"""Review write-side: create, edit, flag and moderate."""

import logging
import uuid
from datetime import datetime, timedelta

from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.html import strip_tags
from django.utils.translation import gettext_lazy as _

from apps.bookings import selectors as bookings
from apps.businesses import services as business_services
from apps.core import audit
from apps.core.errors import AppError, conflict, not_found
from apps.core.images import process_image

from . import events, selectors
from .models import FlagReason, Review, ReviewFlag, ReviewOutbox, ReviewStatus

logger = logging.getLogger(__name__)

EDIT_WINDOW = timedelta(hours=48)
MAX_PHOTOS = 3
MAX_COMMENT = 1000


def clean_comment(text: str) -> str:
    """Policy: reviews are plain text. HTML tags are stripped and control characters dropped on
    the way in, so a stored review can never carry markup (clients must still render as text)."""
    stripped = strip_tags(text)
    printable = "".join(ch for ch in stripped if ch in "\n\t" or ch.isprintable())
    return printable.strip()


def recompute_business_rating(business_id: int) -> selectors.RatingSummary:
    with transaction.atomic():
        summary = selectors.rating_summary(business_id)
        business_services.set_rating(business_id, summary.avg, summary.count)
    return summary


def _store_photos(business_id: int, files: list[bytes]) -> list[str]:
    if len(files) > MAX_PHOTOS:
        raise AppError(
            "too_many_photos",
            _("You can attach up to %(n)s photos.") % {"n": MAX_PHOTOS},
            422,
            {"photos": [str(_("Too many photos."))]},
        )
    processed = [process_image(data, max_dimension=1280) for data in files]  # validate all first
    paths = []
    for image in processed:
        name = f"reviews/{business_id}/{uuid.uuid4().hex}.{image.ext}"
        paths.append(default_storage.save(name, ContentFile(image.content)))
    return paths


def _delete_files(paths: list[str]) -> None:
    for path in paths:
        try:
            default_storage.delete(path)
        except Exception:
            logger.warning("Could not delete orphaned review photo", extra={"path": path})


def create_review(
    *,
    user_id: int,
    appointment_public_id: str,
    rating: int,
    comment: str = "",
    photos: list[bytes] | None = None,
) -> Review:
    ref = bookings.customer_appointment(user_id, appointment_public_id)
    if ref is None:
        raise not_found()
    if ref.status != "completed":
        raise conflict(
            "appointment_not_completed", _("You can review an appointment once it is completed.")
        )
    if Review.objects.filter(appointment_id=ref.id).exists():
        raise conflict("already_reviewed", _("You have already reviewed this appointment."))
    text = clean_comment(comment)
    if len(text) > MAX_COMMENT:
        raise AppError(
            "validation_error", _("Invalid input."), 422, {"comment": [str(_("Too long."))]}
        )
    paths = _store_photos(ref.business_id, photos or [])
    try:
        with transaction.atomic():
            review = Review.objects.create(
                appointment_id=ref.id,
                customer_id=user_id,
                business_id=ref.business_id,
                rating=rating,
                comment=text,
                photos=paths,
            )
            recompute_business_rating(ref.business_id)
            events.emit(
                events.review_created,
                review_id=review.pk,
                customer_id=user_id,
                business_id=ref.business_id,
                appointment_id=ref.id,
                has_photos=bool(paths),
            )
    except IntegrityError as exc:
        _delete_files(paths)
        raise conflict(
            "already_reviewed", _("You have already reviewed this appointment.")
        ) from exc
    except Exception:
        _delete_files(paths)
        raise
    return review


def _own_review(user_id: int, public_id: str) -> Review:
    review = Review.objects.filter(public_id=public_id, customer_id=user_id).first()
    if review is None:
        raise not_found()
    return review


def edit_review(
    *,
    user_id: int,
    review_public_id: str,
    rating: int | None = None,
    comment: str | None = None,
    now: datetime | None = None,
) -> Review:
    """Edit within 48 hours of posting (inclusive of the 48 h mark)."""
    now = now or timezone.now()
    with transaction.atomic():
        review = (
            Review.objects.select_for_update()
            .filter(public_id=review_public_id, customer_id=user_id)
            .first()
        )
        if review is None:
            raise not_found()
        if review.status != ReviewStatus.VISIBLE:
            raise conflict("review_not_editable", _("This review can no longer be edited."))
        if now - review.created_at > EDIT_WINDOW:
            raise conflict("edit_window_closed", _("Reviews can only be edited within 48 hours."))
        if rating is not None:
            review.rating = rating
        if comment is not None:
            text = clean_comment(comment)
            if len(text) > MAX_COMMENT:
                raise AppError(
                    "validation_error", _("Invalid input."), 422, {"comment": [str(_("Too long."))]}
                )
            review.comment = text
        review.edited_at = now
        review.save(update_fields=["rating", "comment", "edited_at", "updated_at"])
        recompute_business_rating(review.business_id)
    return review


def flag_review(*, user_id: int, review_public_id: str, reason: str) -> ReviewFlag:
    if reason not in FlagReason.values:
        raise AppError(
            "validation_error", _("Invalid input."), 422, {"reason": [str(_("Unknown reason."))]}
        )
    review = Review.objects.filter(public_id=review_public_id, status=ReviewStatus.VISIBLE).first()
    if review is None:
        raise not_found()
    if review.customer_id == user_id:
        raise conflict("cannot_flag_own_review", _("You cannot report your own review."))
    try:
        with transaction.atomic():
            flag = ReviewFlag.objects.create(review=review, reporter_id=user_id, reason=reason)
            Review.objects.filter(pk=review.pk).update(is_flagged=True)
            ReviewOutbox.objects.create(
                event="review_flagged",
                review=review,
                payload={"reporter_id": user_id, "reason": reason, "flag_id": flag.pk},
            )
            events.emit(
                events.review_flagged, review_id=review.pk, reporter_id=user_id, reason=reason
            )
    except IntegrityError as exc:
        raise conflict("already_flagged", _("You have already reported this review.")) from exc
    return flag


def moderate_review(review_id: int, status: str, actor_id: int, reason: str = "") -> Review:
    """Hide/remove/restore a review (exposed by Plan 6.3). Keeps the aggregate rating exact."""
    if status not in ReviewStatus.values:
        raise AppError(
            "validation_error", _("Invalid input."), 422, {"status": [str(_("Unknown status."))]}
        )
    with transaction.atomic():
        review = Review.objects.select_for_update().filter(pk=review_id).first()
        if review is None:
            raise not_found()
        before = review.status
        review.status = status
        review.save(update_fields=["status", "updated_at"])
        recompute_business_rating(review.business_id)
        audit.record(
            "review.moderated",
            actor_id=actor_id,
            target_type="review",
            target_id=review.pk,
            before={"status": before},
            after={"status": status, "reason": reason},
        )
    return review

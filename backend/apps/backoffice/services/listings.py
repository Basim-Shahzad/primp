"""Business approval queue: claim, decide, notes and revocation (Plan 6.3, Step 3)."""

from typing import Any

from django.db import transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.businesses import adminops as businesses
from apps.core import audit
from apps.core.errors import AppError, conflict, not_found, validation_error

from ..models import (
    QueueStatus,
    ReviewDecision,
    ReviewKind,
    ReviewNote,
    ReviewQueueItem,
)
from ..permissions import AdminActor

CHECKLIST_KEYS = (
    "identity_matches",
    "address_plausible",
    "photos_genuine",
    "category_correct",
    "no_duplicate",
)
SORTS = {"submitted_at": "submitted_at", "-submitted_at": "-submitted_at"}


def queue(
    *,
    city_id: int | None,
    category_id: int | None,
    kind: str | None,
    status: str | None,
    sort: str,
) -> QuerySet[ReviewQueueItem]:
    qs = ReviewQueueItem.objects.select_related(
        "business__city", "business__category", "assigned_to"
    )
    if city_id is not None:
        qs = qs.filter(business__city_id=city_id)
    if category_id is not None:
        qs = qs.filter(business__category_id=category_id)
    if kind:
        qs = qs.filter(kind=kind)
    qs = qs.filter(status=status) if status else qs.exclude(status=QueueStatus.CLOSED)
    return qs.order_by(SORTS[sort], "id")


def get_item(ref: str, *, lock: bool = False) -> ReviewQueueItem:
    import uuid

    try:
        public_id = uuid.UUID(str(ref))
    except ValueError:
        raise not_found() from None
    qs = ReviewQueueItem.objects.select_related("business")
    if lock:
        qs = qs.select_for_update(of=("self",))
    item = qs.filter(public_id=public_id).first()
    if item is None:
        raise not_found()
    return item


def _open_for_decision(item: ReviewQueueItem, version: int, actor: AdminActor) -> None:
    if item.status == QueueStatus.CLOSED:
        raise conflict("item_closed", _("This item was already decided."))
    if item.version != version:
        raise conflict("stale_version", _("The item changed; reload it and try again."))
    if item.assigned_to_id not in (None, actor.id):
        raise conflict("already_claimed", _("Another admin is reviewing this item."))


def claim(actor: AdminActor, ref: str, version: int) -> ReviewQueueItem:
    actor.require("businesses.review")
    with transaction.atomic():
        item = get_item(ref, lock=True)
        if item.status == QueueStatus.CLOSED:
            raise conflict("item_closed", _("This item was already decided."))
        if item.version != version:
            raise conflict("stale_version", _("The item changed; reload it and try again."))
        if item.assigned_to_id not in (None, actor.id):
            raise conflict("already_claimed", _("Another admin is reviewing this item."))
        item.assigned_to_id = actor.id
        item.status = QueueStatus.IN_REVIEW
        item.version += 1
        item.save(update_fields=["assigned_to", "status", "version", "updated_at"])
    return item


def _close(
    item: ReviewQueueItem, actor: AdminActor, outcome: str, checklist: Any, message: str
) -> None:
    ReviewDecision.objects.create(
        item=item, admin_id=actor.id, action=outcome, checklist=checklist, message=message
    )
    item.status = QueueStatus.CLOSED
    item.outcome = outcome
    item.closed_at = timezone.now()
    item.assigned_to_id = actor.id
    item.version += 1
    item.save(
        update_fields=["status", "outcome", "closed_at", "assigned_to", "version", "updated_at"]
    )


def _check_kind(item: ReviewQueueItem, kind: str) -> None:
    if item.kind != kind:
        raise conflict("wrong_kind", _("This action does not apply to this kind of item."))


def _context(item: ReviewQueueItem, checklist: Any) -> dict[str, Any]:
    ctx: dict[str, Any] = {"queue_item": str(item.public_id)}
    if checklist is not None:
        ctx["checklist"] = checklist
    return ctx


def approve(
    actor: AdminActor, ref: str, version: int, checklist: dict[str, bool]
) -> ReviewQueueItem:
    actor.require("businesses.review")
    failed = [k for k in CHECKLIST_KEYS if not checklist.get(k)]
    if failed:
        raise AppError(
            "checklist_incomplete",
            _("Every checklist item must be confirmed before approving."),
            422,
            {"checklist": failed},
        )
    with transaction.atomic():
        item = get_item(ref, lock=True)
        _open_for_decision(item, version, actor)
        _check_kind(item, ReviewKind.NEW_LISTING)
        business = businesses.get_business_by_id(item.business_id)
        businesses.mark_verified(
            business, actor.user, ip=actor.ip, context=_context(item, checklist)
        )
        _close(item, actor, "approved", checklist, "")
    return item


def reject(
    actor: AdminActor, ref: str, version: int, reason: str, checklist: dict[str, bool] | None
) -> ReviewQueueItem:
    actor.require("businesses.review")
    with transaction.atomic():
        item = get_item(ref, lock=True)
        _open_for_decision(item, version, actor)
        _check_kind(item, ReviewKind.NEW_LISTING)
        business = businesses.get_business_by_id(item.business_id)
        businesses.reject(
            business, actor.user, reason, ip=actor.ip, context=_context(item, checklist)
        )
        _close(item, actor, "rejected", checklist, reason.strip())
    return item


def request_info(
    actor: AdminActor, ref: str, version: int, message: str, checklist: dict[str, bool] | None
) -> ReviewQueueItem:
    actor.require("businesses.review")
    with transaction.atomic():
        item = get_item(ref, lock=True)
        _open_for_decision(item, version, actor)
        _check_kind(item, ReviewKind.NEW_LISTING)
        business = businesses.get_business_by_id(item.business_id)
        businesses.request_more_info(
            business, actor.user, message, ip=actor.ip, context=_context(item, checklist)
        )
        _close(item, actor, "info_requested", checklist, message.strip())
    return item


def approve_change(actor: AdminActor, ref: str, version: int) -> ReviewQueueItem:
    actor.require("businesses.review")
    with transaction.atomic():
        item = get_item(ref, lock=True)
        _open_for_decision(item, version, actor)
        _check_kind(item, ReviewKind.CHANGE_REQUEST)
        business = businesses.get_business_by_id(item.business_id)
        businesses.approve_pending_changes(
            business, actor.user, ip=actor.ip, context=_context(item, None)
        )
        _close(item, actor, "change_approved", None, "")
    return item


def reject_change(actor: AdminActor, ref: str, version: int, reason: str) -> ReviewQueueItem:
    actor.require("businesses.review")
    with transaction.atomic():
        item = get_item(ref, lock=True)
        _open_for_decision(item, version, actor)
        _check_kind(item, ReviewKind.CHANGE_REQUEST)
        business = businesses.get_business_by_id(item.business_id)
        businesses.reject_pending_changes(
            business, actor.user, reason, ip=actor.ip, context=_context(item, None)
        )
        _close(item, actor, "change_rejected", None, reason.strip())
    return item


def add_note(actor: AdminActor, ref: str, body: str) -> ReviewNote:
    actor.require("businesses.review")
    text = body.strip()
    if not text:
        raise validation_error({"body": [str(_("Write a note."))]})
    with transaction.atomic():
        item = get_item(ref, lock=True)
        return ReviewNote.objects.create(item=item, author_id=actor.id, body=text)


def revoke(actor: AdminActor, business_ref: str, reason: str) -> Any:
    actor.require("businesses.revoke")
    with transaction.atomic():
        business = businesses.get_business(business_ref)
        return businesses.revoke_verification(business, actor.user, reason, ip=actor.ip)


def detail(actor: AdminActor, ref: str) -> dict[str, Any]:
    """Everything the reviewer needs. Documents need `documents.view` and every view is audited."""
    actor.require("businesses.review")
    item = get_item(ref)
    business = businesses.get_business_by_id(item.business_id)
    documents: list[dict[str, Any]] | None = None
    if actor.can("documents.view"):
        documents = businesses.documents_with_urls(business)
        if documents:
            audit.record(
                "admin.documents_viewed",
                actor_id=actor.id,
                target_type="business",
                target_id=business.public_id,
                after={
                    "document_ids": [d["id"] for d in documents],
                    "queue_item": str(item.public_id),
                },
                ip=actor.ip,
            )
    return {
        "item": item,
        "business": business,
        "preview": businesses.preview(business),
        "private": businesses.private_fields(business),
        "documents": documents,
        "duplicates": businesses.find_duplicates(business),
        "decisions": list(item.decisions.select_related("admin")),
        "notes": list(item.notes.select_related("author")),
        "previous_items": list(
            ReviewQueueItem.objects.filter(business_id=item.business_id)
            .exclude(pk=item.pk)
            .order_by("-submitted_at")[:10]
        ),
    }

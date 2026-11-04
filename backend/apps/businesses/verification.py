"""Verification state machine used by the admin endpoints (wired in Plan 6.3).

Statuses: draft -> pending -> verified | needs_info | rejected; verified -> revoked.
Each function locks the business, checks the actor is an admin, validates the transition (409
otherwise), writes an audit entry with before/after, invalidates the public caches, emits
`business_verification_changed` and notifies the owner.
"""

from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts.roles import Role
from apps.core import audit
from apps.core.errors import conflict, forbidden, not_found, validation_error

from . import events
from .invalidation import touch
from .models import Business, VerificationStatus
from .onboarding import notify_status_changed

V = VerificationStatus
ADMIN_ROLES = (Role.ADMIN, Role.SUPER_ADMIN)
MAX_MESSAGE = 500


def _snapshot(b: Business) -> dict[str, Any]:
    return {
        "status": b.verification_status,
        "is_listed": b.is_listed,
        "status_message": b.status_message,
        "revoked_reason": b.revoked_reason,
        "verified_by": b.verified_by_id,
    }


def _message(text: str, field: str) -> str:
    cleaned = text.strip()
    if not cleaned or len(cleaned) > MAX_MESSAGE:
        raise validation_error({field: [str(_("Enter a message of 1 to 500 characters."))]})
    return cleaned


def _transition(
    business: Business,
    admin: Any,
    allowed_from: tuple[str, ...],
    to_status: str,
    action: str,
    apply: Any,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    if getattr(admin, "role", None) not in ADMIN_ROLES:
        raise forbidden()
    with transaction.atomic():
        locked = Business.objects.select_for_update().filter(pk=business.pk).first()
        if locked is None:
            raise not_found()
        if locked.verification_status not in allowed_from:
            raise conflict(
                "invalid_transition",
                _("A %(from)s business cannot be moved to %(to)s.")
                % {"from": locked.verification_status, "to": to_status},
            )
        before = _snapshot(locked)
        locked.verification_status = to_status
        apply(locked)
        locked.save()
        audit.record(
            action,
            actor_id=admin.pk,
            target_type="business",
            target_id=locked.public_id,
            before=before,
            after={**_snapshot(locked), **(context or {})},
            ip=ip,
        )
        events.emit(events.business_verification_changed, locked.pk, status=to_status)
        notify_status_changed(locked, to_status, locked.status_message)
    touch(locked.pk)
    business.refresh_from_db()
    return business


def mark_verified(
    business: Business,
    admin: Any,
    *,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    """Approve a pending business (or reinstate a revoked one): shows the badge and lists it."""

    def apply(b: Business) -> None:
        b.is_listed = True
        b.verified_at = timezone.now()
        b.verified_by_id = admin.pk
        b.status_message = ""
        b.revoked_reason = ""

    return _transition(
        business, admin, (V.PENDING, V.REVOKED), V.VERIFIED, "business.verified", apply, ip, context
    )


def revoke_verification(
    business: Business,
    admin: Any,
    reason: str,
    *,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    """Remove the badge of a verified business at once (the listing itself is not changed)."""
    reason = _message(reason, "reason")

    def apply(b: Business) -> None:
        b.revoked_reason = reason
        b.status_message = reason

    return _transition(
        business,
        admin,
        (V.VERIFIED,),
        V.REVOKED,
        "business.verification_revoked",
        apply,
        ip,
        context,
    )


def request_more_info(
    business: Business,
    admin: Any,
    message: str,
    *,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    """Ask the owner for more information; they can edit and resubmit."""
    message = _message(message, "message")

    def apply(b: Business) -> None:
        b.status_message = message
        b.is_listed = False

    return _transition(
        business,
        admin,
        (V.PENDING,),
        V.NEEDS_INFO,
        "business.more_info_requested",
        apply,
        ip,
        context,
    )


def reject(
    business: Business,
    admin: Any,
    reason: str,
    *,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    """Reject a submission (the owner may resubmit once, see ADR-012)."""
    reason = _message(reason, "reason")

    def apply(b: Business) -> None:
        b.status_message = reason
        b.is_listed = False

    return _transition(
        business,
        admin,
        (V.PENDING, V.NEEDS_INFO),
        V.REJECTED,
        "business.rejected",
        apply,
        ip,
        context,
    )


# ---------------------------------------------------------------- name / category changes


def approve_pending_changes(
    business: Business,
    admin: Any,
    *,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    """Apply the owner's pending name/category change to the live listing."""
    from apps.catalog import selectors as catalog

    from .onboarding import unique_slug

    if getattr(admin, "role", None) not in ADMIN_ROLES:
        raise forbidden()
    with transaction.atomic():
        locked = Business.objects.select_for_update().filter(pk=business.pk).first()
        if locked is None:
            raise not_found()
        pending = dict(locked.pending_review_changes or {})
        if not pending:
            raise conflict("no_pending_changes", _("There is no change waiting for review."))
        before = {
            "name": locked.name,
            "category_id": locked.category_id,
            "pending": pending,
        }
        if "name" in pending:
            locked.name = pending["name"]
            locked.slug = unique_slug(locked.city_id, locked.name, locked.pk)
        if "category_id" in pending:
            category = catalog.get_active_category(int(pending["category_id"]))
            if category is None:
                raise conflict("category_unavailable", _("The requested category is inactive."))
            locked.category = category
        locked.pending_review_changes = None
        locked.save()
        audit.record(
            "business.change_approved",
            actor_id=admin.pk,
            target_type="business",
            target_id=locked.public_id,
            before=before,
            after={"name": locked.name, "category_id": locked.category_id, **(context or {})},
            ip=ip,
        )
        notify_status_changed(locked, "change approved", "Your requested changes are now live.")
    touch(locked.pk)
    business.refresh_from_db()
    return business


def reject_pending_changes(
    business: Business,
    admin: Any,
    reason: str,
    *,
    ip: str | None = None,
    context: dict[str, Any] | None = None,
) -> Business:
    """Discard the owner's pending name/category change; the live listing is untouched."""
    reason = _message(reason, "reason")
    if getattr(admin, "role", None) not in ADMIN_ROLES:
        raise forbidden()
    with transaction.atomic():
        locked = Business.objects.select_for_update().filter(pk=business.pk).first()
        if locked is None:
            raise not_found()
        pending = dict(locked.pending_review_changes or {})
        if not pending:
            raise conflict("no_pending_changes", _("There is no change waiting for review."))
        locked.pending_review_changes = None
        locked.save(update_fields=["pending_review_changes", "updated_at"])
        audit.record(
            "business.change_rejected",
            actor_id=admin.pk,
            target_type="business",
            target_id=locked.public_id,
            before={"pending": pending},
            after={"pending": None, "reason": reason, **(context or {})},
            ip=ip,
        )
        notify_status_changed(locked, "change declined", reason)
    touch(locked.pk)
    business.refresh_from_db()
    return business

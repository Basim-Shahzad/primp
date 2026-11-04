"""User management for admins: suspend/reactivate, sessions, deletion requests, admin accounts."""

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import adminops as accounts
from apps.accounts.models import User
from apps.accounts.roles import Role
from apps.bookings import adminops as bookings
from apps.businesses import adminops as businesses
from apps.core import audit
from apps.core.errors import AppError, conflict, forbidden
from apps.notifications import adminops as notices
from apps.notifications import services as notifications

from ..models import SuspensionRecord
from ..permissions import AdminActor


def _is_admin(user: User) -> bool:
    return user.role in (Role.ADMIN, Role.SUPER_ADMIN)


def _guard_role(actor: AdminActor, target: User) -> None:
    """Only a super admin may act on an admin account."""
    if _is_admin(target) and actor.user.role != Role.SUPER_ADMIN:
        raise forbidden()


def _guard_admin_target(actor: AdminActor, target: User) -> None:
    """Role guard, then nobody acts on themselves."""
    _guard_role(actor, target)
    if target.pk == actor.id:
        raise conflict("cannot_target_self", _("You cannot do this to your own account."))


def _open_suspension(user_id: int) -> SuspensionRecord | None:
    return SuspensionRecord.objects.filter(user_id=user_id, reactivated_at__isnull=True).first()


def current_suspension(user_id: int) -> SuspensionRecord | None:
    return _open_suspension(user_id)


def suspend(actor: AdminActor, user_ref: str, reason: str) -> User:
    actor.require("users.suspend")
    with transaction.atomic():
        user = accounts.get_user(user_ref, lock=True)
        _guard_role(actor, user)
        if (
            user.role == Role.SUPER_ADMIN
            and user.status == "active"
            and accounts.count_active_super_admins() <= 1
        ):
            raise conflict("last_super_admin", _("The last super admin cannot be suspended."))
        _guard_admin_target(actor, user)
        if user.status != "active":
            raise conflict("invalid_state", _("Only active accounts can be suspended."))
        before = {"status": user.status}
        accounts.suspend(user)
        hidden = businesses.hide_owner_listings(user.pk)
        SuspensionRecord.objects.create(
            user=user, suspended_by_id=actor.id, reason=reason, previous_listings=hidden
        )
        audit.record(
            "admin.user_suspended",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            before=before,
            after={
                "status": "suspended",
                "reason": reason,
                "unlisted_businesses": sorted(int(k) for k, was in hidden.items() if was),
            },
            ip=actor.ip,
        )
        _notify_suspension(user)
    return user


def _notify_suspension(user: User) -> None:
    """Customers keep their bookings; the other side is told (ADR-021)."""
    for brief in bookings.future_active_for_customer(user.pk):
        notifications.notify(
            brief.owner_id, "customer_suspended_notice", brief.payload(), include_sms=False
        )
    for brief in bookings.future_active_for_owner(user.pk):
        if brief.customer_id is not None:
            notifications.notify(brief.customer_id, "business_suspended_notice", brief.payload())


def reactivate(actor: AdminActor, user_ref: str, reason: str) -> User:
    actor.require("users.suspend")
    with transaction.atomic():
        user = accounts.get_user(user_ref, lock=True)
        _guard_admin_target(actor, user)
        record = _open_suspension(user.pk)
        if user.status != "suspended" or record is None:
            raise conflict("invalid_state", _("This account is not suspended."))
        restored = businesses.restore_owner_listings(user.pk, record.previous_listings)
        accounts.reactivate(user)
        record.reactivated_at = timezone.now()
        record.reactivated_by_id = actor.id
        record.reactivation_reason = reason
        record.save(update_fields=["reactivated_at", "reactivated_by", "reactivation_reason"])
        audit.record(
            "admin.user_reactivated",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            before={"status": "suspended"},
            after={"status": "active", "reason": reason, "relisted_businesses": sorted(restored)},
            ip=actor.ip,
        )
    return user


def force_logout(actor: AdminActor, user_ref: str) -> int:
    actor.require("users.force_logout")
    with transaction.atomic():
        user = accounts.get_user(user_ref)
        _guard_admin_target(actor, user)
        revoked = accounts.force_logout(user)
        audit.record(
            "admin.user_force_logout",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            before={"active_sessions": revoked},
            after={"active_sessions": 0},
            ip=actor.ip,
        )
    return revoked


def send_password_reset(actor: AdminActor, user_ref: str) -> None:
    actor.require("users.reset_password")
    with transaction.atomic():
        user = accounts.get_user(user_ref)
        _guard_admin_target(actor, user)
        if user.status != "active":
            raise conflict("invalid_state", _("Only active accounts can receive a reset link."))
        accounts.send_password_reset(user)
        audit.record(
            "admin.user_password_reset_sent",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            ip=actor.ip,
        )


# ---------------------------------------------------------------- deletion requests


def process_deletion(actor: AdminActor, request_id: int) -> None:
    """Anonymise the requester's personal data; bookings and ledger rows stay valid."""
    actor.require("users.suspend")  # destructive account change: same level as suspending
    with transaction.atomic():
        request = accounts.get_deletion_request(request_id, lock=True)
        if request.status != "pending":
            raise conflict("invalid_state", _("This request was already handled."))
        user = accounts.get_user(request.user.public_id, lock=True)
        _guard_admin_target(actor, user)
        if businesses.owns_businesses(user.pk):
            raise AppError(
                "owner_has_businesses",
                _("This account owns businesses; handle them before deleting the account."),
                409,
            )
        before = {
            "status": user.status,
            "had_phone": bool(user.phone),
            "had_email": bool(user.email),
        }
        photo = accounts.anonymise(user)
        accounts.delete_photo_later(photo)
        bookings.scrub_customer_contact(user.pk)
        notices.scrub_recipient(user.pk)
        accounts.mark_deletion_processed(request)
        audit.record(
            "admin.deletion_processed",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            before=before,
            after={"status": "deleted", "request_id": request.pk},
            ip=actor.ip,
        )


# ---------------------------------------------------------------- admin accounts


def create_admin(actor: AdminActor, *, email: str, name: str, group: str) -> User:
    actor.require("admins.manage")
    with transaction.atomic():
        user = accounts.create_admin(email=email, name=name, group=group)
        accounts.send_password_reset(user)  # the new admin sets their own password
        audit.record(
            "admin.admin_created",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            before=None,
            after={"group": group, "role": user.role},
            ip=actor.ip,
        )
    return user


def set_admin_group(actor: AdminActor, user_ref: str, group: str) -> User:
    actor.require("admins.manage")
    with transaction.atomic():
        user = accounts.get_user(user_ref, lock=True)
        if (
            user.role == Role.SUPER_ADMIN
            and user.status == "active"
            and group != "super_admin"
            and accounts.count_active_super_admins() <= 1
        ):
            raise conflict("last_super_admin", _("The last super admin cannot be demoted."))
        if user.pk == actor.id:
            raise conflict("cannot_target_self", _("You cannot change your own group."))
        before = accounts.admin_summary(user)
        accounts.set_admin_group(user, group)
        audit.record(
            "admin.admin_group_changed",
            actor_id=actor.id,
            target_type="user",
            target_id=user.public_id,
            before=before,
            after=accounts.admin_summary(user),
            ip=actor.ip,
        )
    return user

"""Account operations for the backoffice (which may not import this app's models)."""

import contextlib
import re
import uuid
from datetime import datetime
from typing import Any

from django.contrib.auth.models import Group
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.db.models import Q, QuerySet
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError, conflict, not_found

from . import onetime, tokens
from .models import (
    AccountDeletionRequest,
    CustomerProfile,
    DeletionStatus,
    OneTimeToken,
    RefreshToken,
    TokenPurpose,
    User,
    UserStatus,
    Wishlist,
)
from .roles import Role
from .services import PASSWORD_RESET_TTL, _frontend_link

ADMIN_GROUPS = ("support_agent", "ops_admin", "super_admin")
USER_SORTS = {
    "created_at": "created_at",
    "-created_at": "-created_at",
    "email": "email",
    "-email": "-email",
    "name": "profile__name",
    "-name": "-profile__name",
}


def search_users(
    *,
    q: str | None = None,
    role: str | None = None,
    status: str | None = None,
    phone_verified: bool | None = None,
    email_verified: bool | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    sort: str = "-created_at",
) -> QuerySet[User]:
    """Users matching the filters. `sort` must be one of USER_SORTS (an allow-list)."""
    qs = User.objects.select_related("profile")
    if q:
        text = q.strip()
        clause = Q(email__icontains=text) | Q(profile__name__icontains=text)
        digits = re.sub(r"\D", "", text)
        if len(digits) >= 3:
            tail = digits[-10:] if len(digits) > 10 else digits.lstrip("0") or digits
            clause |= Q(phone__icontains=tail)
        if digits and digits == text and len(digits) <= 9:
            clause |= Q(pk=int(digits))
        with contextlib.suppress(ValueError):
            clause |= Q(public_id=uuid.UUID(text))
        qs = qs.filter(clause)
    if role:
        qs = qs.filter(role=role)
    if status:
        qs = qs.filter(status=status)
    if phone_verified is not None:
        qs = qs.filter(is_phone_verified=phone_verified)
    if email_verified is not None:
        qs = qs.filter(is_email_verified=email_verified)
    if created_from:
        qs = qs.filter(created_at__gte=created_from)
    if created_to:
        qs = qs.filter(created_at__lte=created_to)
    return qs.order_by(USER_SORTS[sort], "id")


def get_user(ref: str | uuid.UUID, *, lock: bool = False) -> User:
    try:
        public_id = ref if isinstance(ref, uuid.UUID) else uuid.UUID(str(ref))
    except ValueError:
        raise not_found() from None
    qs = User.objects.select_related("profile")
    if lock:
        qs = qs.select_for_update(of=("self",))
    user = qs.filter(public_id=public_id).first()
    if user is None:
        raise not_found()
    return user


def login_history(user_id: int, limit: int = 20) -> list[RefreshToken]:
    """One row per login (the first token of each session family), newest first."""
    rows = (
        RefreshToken.objects.filter(user_id=user_id)
        .order_by("family_id", "created_at")
        .distinct("family_id")
    )
    return sorted(rows, key=lambda r: r.created_at, reverse=True)[:limit]


def deletion_requests(status: str | None = None) -> QuerySet[AccountDeletionRequest]:
    qs = AccountDeletionRequest.objects.select_related("user", "user__profile")
    if status:
        qs = qs.filter(status=status)
    return qs.order_by("created_at", "id")


def user_deletion_requests(user_id: int) -> list[AccountDeletionRequest]:
    return list(AccountDeletionRequest.objects.filter(user_id=user_id).order_by("-created_at"))


def get_deletion_request(request_id: int, *, lock: bool = False) -> AccountDeletionRequest:
    qs = AccountDeletionRequest.objects.select_related("user")
    if lock:
        qs = qs.select_for_update(of=("self",))
    row = qs.filter(pk=request_id).first()
    if row is None:
        raise not_found()
    return row


# ---------------------------------------------------------------- state changes


def count_active_super_admins() -> int:
    return User.objects.filter(role=Role.SUPER_ADMIN, status=UserStatus.ACTIVE).count()


def suspend(user: User) -> None:
    user.status = UserStatus.SUSPENDED
    user.save(update_fields=["status", "updated_at"])
    tokens.revoke_all_sessions(user)


def reactivate(user: User) -> None:
    user.status = UserStatus.ACTIVE
    user.failed_login_count = 0
    user.locked_until = None
    user.save(update_fields=["status", "failed_login_count", "locked_until", "updated_at"])


def force_logout(user: User) -> int:
    return tokens.revoke_all_sessions(user)


def send_password_reset(user: User) -> None:
    """Email a reset link (the admin never sets a password)."""
    from apps.notifications.services import queue_email

    if not user.email:
        raise AppError("no_email", _("This account has no email address."), 422)
    raw = onetime.create_token(user, TokenPurpose.PASSWORD_RESET, PASSWORD_RESET_TTL)
    queue_email(
        user.email,
        str(_("Reset your Primp password")),
        str(_("Reset your password (valid for 1 hour): %(link)s"))
        % {"link": _frontend_link("reset-password", raw)},
    )


def anonymise(user: User) -> str:
    """Remove personal data but keep the row (bookings and the ledger still point to it).

    Returns the old photo path so the caller can delete the file after commit.
    """
    old_photo = ""
    user.email = f"deleted-{user.public_id}@deleted.invalid"
    user.phone = None
    user.is_email_verified = False
    user.is_phone_verified = False
    user.status = UserStatus.DELETED
    user.set_unusable_password()
    user.save()
    profile = CustomerProfile.objects.filter(user=user).first()
    if profile is not None:
        old_photo = profile.photo
        profile.name = "Deleted user"
        profile.photo = ""
        profile.default_city = None
        profile.default_area = None
        profile.referred_by = None
        profile.save()
    Wishlist.objects.filter(user=user).delete()
    OneTimeToken.objects.filter(user=user).delete()
    tokens.revoke_all_sessions(user)
    return old_photo


def delete_photo_later(path: str) -> None:
    if path:
        transaction.on_commit(lambda: default_storage.delete(path))


def mark_deletion_processed(request: AccountDeletionRequest) -> None:
    request.status = DeletionStatus.PROCESSED
    request.save(update_fields=["status", "updated_at"])


# ---------------------------------------------------------------- admin accounts


def create_admin(*, email: str, name: str, group: str) -> User:
    if group not in ADMIN_GROUPS:
        raise AppError("validation_error", _("Invalid input."), 422, {"group": ["Unknown group."]})
    role = Role.SUPER_ADMIN if group == "super_admin" else Role.ADMIN
    try:
        with transaction.atomic():
            user = User.objects.create_user(email=email.strip().lower(), role=role)
            user.is_email_verified = True
            user.save(update_fields=["is_email_verified"])
            CustomerProfile.objects.create(user=user, name=name.strip())
            user.groups.set([Group.objects.get(name=group)])
    except IntegrityError as exc:
        raise conflict("email_taken", _("An account with this email already exists.")) from exc
    return user


def set_admin_group(user: User, group: str) -> None:
    if group not in ADMIN_GROUPS:
        raise AppError("validation_error", _("Invalid input."), 422, {"group": ["Unknown group."]})
    if user.role not in (Role.ADMIN, Role.SUPER_ADMIN):
        raise conflict("not_an_admin", _("This account is not an admin."))
    user.role = Role.SUPER_ADMIN if group == "super_admin" else Role.ADMIN
    user.save(update_fields=["role", "updated_at"])
    user.groups.set([Group.objects.get(name=group)])


def list_admins() -> QuerySet[User]:
    return (
        User.objects.filter(role__in=(Role.ADMIN, Role.SUPER_ADMIN))
        .select_related("profile")
        .order_by("id")
    )


def group_names(user: User) -> list[str]:
    return sorted(user.groups.values_list("name", flat=True))


def admin_summary(user: User) -> dict[str, Any]:
    return {"role": user.role, "groups": group_names(user)}

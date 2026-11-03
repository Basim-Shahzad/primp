"""Profile management: edit, photo, phone/email change, deletion request."""

import uuid
from datetime import timedelta

from django.conf import settings
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.db import IntegrityError, transaction
from django.utils.translation import gettext as _t
from django.utils.translation import gettext_lazy as _

from apps.core import audit
from apps.core.errors import AppError
from apps.core.images import process_image
from apps.geo import selectors as geo
from apps.notifications.services import queue_email

from . import events, onetime, otp
from .models import (
    AccountDeletionRequest,
    CustomerProfile,
    DeletionStatus,
    TokenPurpose,
    User,
)
from .tokens import revoke_all_sessions

EMAIL_CHANGE_TTL = timedelta(hours=24)


def get_or_create_profile(user: User) -> CustomerProfile:
    profile, _created = CustomerProfile.objects.get_or_create(user=user)
    return profile


def update_profile(
    user: User,
    *,
    name: str | None = None,
    default_city_id: int | None = None,
    default_area_id: int | None = None,
    fields_set: set[str],
) -> CustomerProfile:
    profile = get_or_create_profile(user)
    if "name" in fields_set and name is not None:
        profile.name = name.strip()
    city_set = "default_city_id" in fields_set
    area_set = "default_area_id" in fields_set
    if area_set and default_area_id is not None:
        area = geo.get_active_area(default_area_id)
        if area is None:
            raise _bad_geo("default_area_id")
        if city_set and default_city_id is not None and area.city_id != default_city_id:
            raise _bad_geo("default_area_id", _("Area does not belong to the city."))
        profile.default_area = area
        profile.default_city = area.city
    else:
        if area_set:
            profile.default_area = None
        if city_set:
            if default_city_id is None:
                profile.default_city = None
                profile.default_area = None
            else:
                city = geo.get_active_city(default_city_id)
                if city is None:
                    raise _bad_geo("default_city_id")
                if profile.default_area and profile.default_area.city_id != city.pk:
                    profile.default_area = None
                profile.default_city = city
    profile.save()
    events.emit_profile_updated(user.pk)
    return profile


def _bad_geo(field: str, message: object = None) -> AppError:
    return AppError(
        "validation_error",
        _("Invalid input."),
        422,
        {field: [str(message or _t("Unknown location."))]},
    )


def set_photo(user: User, data: bytes) -> CustomerProfile:
    image = process_image(data, max_dimension=512)
    profile = get_or_create_profile(user)
    old = profile.photo
    path = default_storage.save(
        f"profiles/{user.public_id}/{uuid.uuid4().hex}.{image.ext}", ContentFile(image.content)
    )
    profile.photo = path
    profile.save(update_fields=["photo", "updated_at"])
    events.emit_profile_updated(user.pk)
    if old:
        transaction.on_commit(lambda: default_storage.delete(old))
    return profile


def photo_url(profile: CustomerProfile | None) -> str | None:
    if profile is None or not profile.photo:
        return None
    return str(default_storage.url(profile.photo))


# ---- phone change ----


def request_phone_change(user: User, phone: str, ip: str | None) -> None:
    if phone == user.phone:
        raise AppError("phone_unchanged", _("This is already your number."), 409)
    if User.objects.filter(phone=phone).exclude(pk=user.pk).exists():
        raise AppError(
            "phone_taken", _("This number is already in use."), 409, {"phone": [_t("In use.")]}
        )
    otp.request_otp(phone, otp.PHONE_CHANGE, ip)


def confirm_phone_change(user: User, phone: str, code: str) -> User:
    otp.verify_otp(phone, otp.PHONE_CHANGE, code)
    try:
        with transaction.atomic():
            user.phone = phone
            user.is_phone_verified = True
            user.save(update_fields=["phone", "is_phone_verified", "updated_at"])
    except IntegrityError as exc:
        raise AppError("phone_taken", _("This number is already in use."), 409) from exc
    audit.record("user.phone_changed", actor_id=user.pk, target_type="user", target_id=user.pk)
    events.emit_profile_updated(user.pk)
    return user


# ---- email change ----


def request_email_change(user: User, email: str) -> None:
    email = email.strip().lower()
    if email == user.email:
        raise AppError("email_unchanged", _("This is already your email."), 409)
    if User.objects.filter(email=email).exclude(pk=user.pk).exists():
        raise AppError(
            "email_taken", _("This email is already in use."), 409, {"email": [_t("In use.")]}
        )
    raw = onetime.create_token(user, TokenPurpose.EMAIL_CHANGE, EMAIL_CHANGE_TTL, {"email": email})
    link = f"{settings.FRONTEND_URL.rstrip('/')}/confirm-email?token={raw}"
    queue_email(
        email,
        _t("Confirm your new Primp email"),
        _t("Confirm this email address for your Primp account: %(link)s") % {"link": link},
    )


def confirm_email_change(raw: str) -> User:
    with transaction.atomic():
        token = onetime.consume_token(raw, TokenPurpose.EMAIL_CHANGE)
        user = token.user
        user.email = str(token.payload["email"])
        user.is_email_verified = True
        try:
            with transaction.atomic():
                user.save(update_fields=["email", "is_email_verified", "updated_at"])
        except IntegrityError as exc:
            raise AppError("email_taken", _("This email is already in use."), 409) from exc
    audit.record("user.email_changed", actor_id=user.pk, target_type="user", target_id=user.pk)
    events.emit_profile_updated(user.pk)
    return user


# ---- deletion request ----


def request_deletion(user: User, reason: str = "") -> AccountDeletionRequest:
    with transaction.atomic():
        existing = AccountDeletionRequest.objects.filter(
            user=user, status=DeletionStatus.PENDING
        ).first()
        if existing is None:
            existing = AccountDeletionRequest.objects.create(user=user, reason=reason.strip())
            audit.record(
                "user.deletion_requested", actor_id=user.pk, target_type="user", target_id=user.pk
            )
        revoke_all_sessions(user)
    return existing

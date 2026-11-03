"""Write-side business logic for authentication and registration."""

import logging
from datetime import timedelta
from typing import Any

from django.conf import settings
from django.contrib.auth import password_validation
from django.core.exceptions import ValidationError as DjangoValidationError
from django.db import IntegrityError, transaction
from django.db.models import F
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext as _t
from django.utils.translation import gettext_lazy as _

from apps.core import audit, ratelimit
from apps.core.errors import AppError
from apps.core.http import get_client_ip
from apps.notifications.services import queue_email

from . import onetime, otp
from .google import verify_google_id_token
from .models import (
    AuthProvider,
    CustomerProfile,
    TokenPurpose,
    User,
    UserStatus,
)
from .roles import Role
from .tokens import SessionTokens, issue_session, revoke_all_sessions

logger = logging.getLogger(__name__)

MAX_FAILED_LOGINS = 5
LOCKOUT = timedelta(minutes=15)
EMAIL_VERIFY_TTL = timedelta(hours=24)
PASSWORD_RESET_TTL = timedelta(hours=1)
LOGIN_IP_LIMIT = (30, 60)
REGISTER_IP_LIMIT = (20, 3600)
RESET_EMAIL_LIMIT = (3, 3600)

# Hash computed once so unknown-email logins cost the same as real ones.
_DUMMY_HASH: str | None = None


def _dummy_check(password: str) -> None:
    global _DUMMY_HASH
    from django.contrib.auth.hashers import check_password, make_password

    if _DUMMY_HASH is None:
        _DUMMY_HASH = make_password("dummy-password-for-timing")
    check_password(password, _DUMMY_HASH)


def _session_meta(request: HttpRequest) -> dict[str, Any]:
    return {"user_agent": request.headers.get("User-Agent", ""), "ip": get_client_ip(request)}


def start_session(user: User, request: HttpRequest) -> SessionTokens:
    return issue_session(user, **_session_meta(request))


def validate_password(password: str, user: User | None = None) -> None:
    try:
        password_validation.validate_password(password, user)
    except DjangoValidationError as exc:
        raise AppError(
            "validation_error", _("Invalid input."), 422, {"password": list(exc.messages)}
        ) from exc


def _frontend_link(path: str, token: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}/{path}?token={token}"


def send_email_verification(user: User) -> None:
    if not user.email:
        return
    raw = onetime.create_token(user, TokenPurpose.EMAIL_VERIFY, EMAIL_VERIFY_TTL)
    link = _frontend_link("verify-email", raw)
    queue_email(
        user.email,
        _t("Verify your Primp email"),
        _t("Welcome to Primp! Confirm your email address: %(link)s") % {"link": link},
    )


def _create_customer(
    *,
    email: str | None = None,
    phone: str | None = None,
    password: str | None = None,
    name: str = "",
    provider: str = AuthProvider.PASSWORD,
    email_verified: bool = False,
    phone_verified: bool = False,
) -> User:
    user = User.objects.create_user(
        email=email,
        phone=phone,
        password=password,
        role=Role.CUSTOMER,
        auth_provider=provider,
        is_email_verified=email_verified,
        is_phone_verified=phone_verified,
    )
    CustomerProfile.objects.create(user=user, name=name)
    return user


def register_with_email(
    *, name: str, email: str, password: str, request: HttpRequest
) -> SessionTokens:
    ip = get_client_ip(request)
    if ip:
        ratelimit.enforce(
            f"register-ip:{ip}", limit=REGISTER_IP_LIMIT[0], window_seconds=REGISTER_IP_LIMIT[1]
        )
    email = email.strip().lower()
    validate_password(password, User(email=email))
    try:
        with transaction.atomic():
            user = _create_customer(email=email, password=password, name=name.strip())
            send_email_verification(user)
    except IntegrityError as exc:
        raise AppError(
            "email_taken",
            _("An account with this email already exists."),
            409,
            {"email": [_t("Already registered.")]},
        ) from exc
    return start_session(user, request)


def verify_email(raw_token: str) -> User:
    with transaction.atomic():
        token = onetime.consume_token(raw_token, TokenPurpose.EMAIL_VERIFY)
        user = token.user
        user.is_email_verified = True
        user.save(update_fields=["is_email_verified", "updated_at"])
    return user


def login_with_password(*, email: str, password: str, request: HttpRequest) -> SessionTokens:
    ip = get_client_ip(request)
    if ip:
        ratelimit.enforce(
            f"login-ip:{ip}", limit=LOGIN_IP_LIMIT[0], window_seconds=LOGIN_IP_LIMIT[1]
        )
    invalid = AppError("invalid_credentials", _("Incorrect email or password."), 401)
    user = User.objects.filter(email=email.strip().lower()).first()
    if user is None or not user.has_usable_password():
        _dummy_check(password)
        raise invalid
    now = timezone.now()
    if user.locked_until and user.locked_until <= now:
        User.objects.filter(pk=user.pk).update(failed_login_count=0, locked_until=None)
        user.refresh_from_db()
    if user.is_locked:
        _dummy_check(password)
        raise invalid
    if not user.check_password(password):
        _register_failure(user)
        raise invalid
    if user.status != UserStatus.ACTIVE:
        raise AppError("account_suspended", _("This account is not active."), 403)
    User.objects.filter(pk=user.pk).update(failed_login_count=0, locked_until=None)
    return start_session(user, request)


def _register_failure(user: User) -> None:
    User.objects.filter(pk=user.pk).update(failed_login_count=F("failed_login_count") + 1)
    user.refresh_from_db(fields=["failed_login_count"])
    if user.failed_login_count >= MAX_FAILED_LOGINS:
        User.objects.filter(pk=user.pk).update(locked_until=timezone.now() + LOCKOUT)
        audit.record("auth.account_locked", target_type="user", target_id=user.pk)


def request_phone_otp(phone: str, request: HttpRequest) -> None:
    otp.request_otp(phone, otp.LOGIN, get_client_ip(request))


def login_with_phone_otp(
    *, phone: str, code: str, name: str, request: HttpRequest
) -> tuple[SessionTokens, bool]:
    otp.verify_otp(phone, otp.LOGIN, code)
    created = False
    user = User.objects.filter(phone=phone).first()
    if user is None:
        try:
            with transaction.atomic():
                user = _create_customer(
                    phone=phone, name=name.strip(), provider=AuthProvider.OTP, phone_verified=True
                )
            created = True
        except IntegrityError:  # lost a registration race: the winner's account is ours too
            user = User.objects.get(phone=phone)
    if user.status != UserStatus.ACTIVE:
        raise AppError("account_suspended", _("This account is not active."), 403)
    if not user.is_phone_verified:
        user.is_phone_verified = True
        user.save(update_fields=["is_phone_verified", "updated_at"])
    return start_session(user, request), created


def login_with_google(*, id_token: str, request: HttpRequest) -> tuple[SessionTokens, bool]:
    identity = verify_google_id_token(id_token)
    created = False
    user = User.objects.filter(email=identity.email).first()
    if user is None:
        try:
            with transaction.atomic():
                user = _create_customer(
                    email=identity.email,
                    name=identity.name,
                    provider=AuthProvider.GOOGLE,
                    email_verified=True,
                )
            created = True
        except IntegrityError:
            user = User.objects.get(email=identity.email)
    elif not user.is_email_verified:
        # Linking to an unverified password account: whoever registered it never proved they
        # own the address, so drop their credentials and sessions before trusting Google.
        user.set_unusable_password()
        user.is_email_verified = True
        user.save(update_fields=["password", "is_email_verified", "updated_at"])
        revoke_all_sessions(user)
        audit.record("auth.google_link_reset", target_type="user", target_id=user.pk)
    if user.status != UserStatus.ACTIVE:
        raise AppError("account_suspended", _("This account is not active."), 403)
    return start_session(user, request), created


def request_password_reset(email: str) -> None:
    """Runs inside a Celery task so the HTTP response is identical for any address."""
    email = email.strip().lower()
    if not ratelimit.hit(
        f"reset-email:{email}", limit=RESET_EMAIL_LIMIT[0], window_seconds=RESET_EMAIL_LIMIT[1]
    ):
        return
    user = User.objects.filter(email=email, status=UserStatus.ACTIVE).first()
    if user is None:
        return
    raw = onetime.create_token(user, TokenPurpose.PASSWORD_RESET, PASSWORD_RESET_TTL)
    queue_email(
        email,
        _t("Reset your Primp password"),
        _t("Reset your password (valid for 1 hour): %(link)s")
        % {"link": _frontend_link("reset-password", raw)},
    )


def confirm_password_reset(*, token: str, new_password: str) -> None:
    with transaction.atomic():
        record = onetime.consume_token(token, TokenPurpose.PASSWORD_RESET)
        user = record.user
        validate_password(new_password, user)
        user.set_password(new_password)
        user.failed_login_count = 0
        user.locked_until = None
        user.save(update_fields=["password", "failed_login_count", "locked_until", "updated_at"])
        revoke_all_sessions(user)
        audit.record("auth.password_reset", actor_id=user.pk, target_type="user", target_id=user.pk)

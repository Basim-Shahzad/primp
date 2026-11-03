"""JWT access tokens and rotating server-stored refresh tokens."""

import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import jwt
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core import audit
from apps.core.errors import AppError

from .models import RefreshToken, User, UserStatus


@dataclass
class SessionTokens:
    access_token: str
    expires_in: int
    refresh_token: str
    family_id: uuid.UUID


def hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode()).hexdigest()


def access_lifetime(user: User) -> timedelta:
    if user.is_admin_role:
        return settings.ADMIN_ACCESS_TOKEN_LIFETIME
    return settings.ACCESS_TOKEN_LIFETIME


def refresh_lifetime(user: User) -> timedelta:
    if user.is_admin_role:
        return settings.ADMIN_REFRESH_TOKEN_LIFETIME
    return settings.REFRESH_TOKEN_LIFETIME


def create_access_token(user: User, family_id: uuid.UUID) -> tuple[str, int]:
    now = timezone.now()
    lifetime = access_lifetime(user)
    claims = {
        "sub": str(user.public_id),
        "role": user.role,
        "sid": str(family_id),
        "jti": uuid.uuid4().hex,
        "iat": now,
        "exp": now + lifetime,
    }
    token = jwt.encode(claims, settings.JWT_SIGNING_KEY, algorithm=settings.JWT_ALGORITHM)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str) -> dict[str, Any] | None:
    try:
        claims: dict[str, Any] = jwt.decode(
            token,
            settings.JWT_SIGNING_KEY,
            algorithms=[settings.JWT_ALGORITHM],
            options={"require": ["exp", "iat", "sub", "jti", "sid"]},
        )
    except jwt.PyJWTError:
        return None
    return claims


def _new_refresh(
    user: User, family_id: uuid.UUID, ua: str, ip: str | None
) -> tuple[str, RefreshToken]:
    raw = secrets.token_urlsafe(48)
    row = RefreshToken.objects.create(
        user=user,
        family_id=family_id,
        token_hash=hash_token(raw),
        expires_at=timezone.now() + refresh_lifetime(user),
        user_agent=ua[:255],
        ip_address=ip,
    )
    return raw, row


def issue_session(user: User, *, user_agent: str = "", ip: str | None = None) -> SessionTokens:
    family_id = uuid.uuid4()
    refresh, _row = _new_refresh(user, family_id, user_agent, ip)
    access, expires_in = create_access_token(user, family_id)
    return SessionTokens(access, expires_in, refresh, family_id)


def revoke_family(family_id: uuid.UUID) -> int:
    return RefreshToken.objects.filter(family_id=family_id, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


def revoke_all_sessions(user: User) -> int:
    return RefreshToken.objects.filter(user=user, revoked_at__isnull=True).update(
        revoked_at=timezone.now()
    )


def session_is_active(family_id: str) -> bool:
    try:
        fid = uuid.UUID(family_id)
    except ValueError:
        return False
    return RefreshToken.objects.filter(
        family_id=fid, revoked_at__isnull=True, expires_at__gt=timezone.now()
    ).exists()


def _invalid() -> AppError:
    return AppError("invalid_refresh_token", _("Invalid or expired refresh token."), 401)


def rotate_refresh_token(raw: str, *, user_agent: str = "", ip: str | None = None) -> SessionTokens:
    reuse_detected: uuid.UUID | None = None
    dead_family: uuid.UUID | None = None
    with transaction.atomic():
        row = (
            RefreshToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=hash_token(raw))
            .first()
        )
        if row is None:
            raise _invalid()
        user = row.user
        if row.revoked_at is not None or row.replaced_by_id is not None:
            reuse_detected = row.family_id
        elif row.expires_at <= timezone.now() or user.status != UserStatus.ACTIVE:
            dead_family = row.family_id
        else:
            new_raw, new_row = _new_refresh(user, row.family_id, user_agent, ip)
            row.replaced_by = new_row
            row.revoked_at = timezone.now()
            row.save(update_fields=["replaced_by", "revoked_at"])
            access, expires_in = create_access_token(user, row.family_id)
            return SessionTokens(access, expires_in, new_raw, row.family_id)
    # Revocations happen outside the atomic block so raising does not roll them back.
    if dead_family is not None:
        revoke_family(dead_family)
        raise _invalid()
    assert reuse_detected is not None
    revoke_family(reuse_detected)
    audit.record("auth.refresh_reuse", target_type="session", target_id=reuse_detected, ip=ip)
    raise AppError("refresh_token_reused", _("Refresh token was already used."), 401)


def logout(raw: str) -> None:
    row = RefreshToken.objects.filter(token_hash=hash_token(raw)).first()
    if row is not None:
        revoke_family(row.family_id)

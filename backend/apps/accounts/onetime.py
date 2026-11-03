import secrets
from datetime import timedelta
from typing import Any

from django.db import transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError

from .models import OneTimeToken, User
from .tokens import hash_token


def create_token(
    user: User, purpose: str, ttl: timedelta, payload: dict[str, Any] | None = None
) -> str:
    raw = secrets.token_urlsafe(32)
    OneTimeToken.objects.create(
        user=user,
        purpose=purpose,
        token_hash=hash_token(raw),
        payload=payload or {},
        expires_at=timezone.now() + ttl,
    )
    return raw


def consume_token(raw: str, purpose: str) -> OneTimeToken:
    """Mark the token used and return it; raises 400 invalid_token if unknown/used/expired."""
    with transaction.atomic():
        token = (
            OneTimeToken.objects.select_for_update()
            .select_related("user")
            .filter(token_hash=hash_token(raw), purpose=purpose)
            .first()
        )
        if token is None or token.used_at is not None or token.expires_at <= timezone.now():
            raise AppError("invalid_token", _("This link is invalid or has expired."), 400)
        token.used_at = timezone.now()
        token.save(update_fields=["used_at"])
        return token

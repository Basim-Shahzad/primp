"""Google ID token verification (external boundary, mocked in tests)."""

from dataclasses import dataclass

from django.conf import settings
from django.utils.translation import gettext_lazy as _
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from apps.core.errors import AppError


@dataclass
class GoogleIdentity:
    email: str
    name: str
    email_verified: bool


def _verify(token: str, audiences: list[str]) -> dict[str, object]:
    return id_token.verify_oauth2_token(  # type: ignore[no-any-return,no-untyped-call]
        token, google_requests.Request(), audience=audiences
    )


def verify_google_id_token(token: str) -> GoogleIdentity:
    audiences = list(settings.GOOGLE_CLIENT_IDS)
    if not audiences:
        raise AppError("google_not_configured", _("Google sign-in is not available."), 503)
    try:
        claims = _verify(token, audiences)
    except (ValueError, Exception) as exc:
        raise AppError("invalid_google_token", _("Google sign-in failed."), 401) from exc
    email = str(claims.get("email", "")).lower()
    if not email or claims.get("email_verified") is not True:
        raise AppError("google_email_unverified", _("Your Google email is not verified."), 401)
    return GoogleIdentity(email=email, name=str(claims.get("name", ""))[:100], email_verified=True)

import hmac
import secrets

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _

from apps.core.errors import AppError

CSRF_HEADER = "X-CSRF-Token"


def set_refresh_cookies(response: HttpResponse, refresh: str) -> None:
    max_age = int(settings.REFRESH_TOKEN_LIFETIME.total_seconds())
    secure = settings.REFRESH_COOKIE_SECURE
    response.set_cookie(
        settings.REFRESH_COOKIE_NAME,
        refresh,
        max_age=max_age,
        httponly=True,
        secure=secure,
        samesite="Lax",
        path=settings.REFRESH_COOKIE_PATH,
    )
    # double-submit CSRF cookie: readable by JS, echoed in the X-CSRF-Token header
    response.set_cookie(
        settings.REFRESH_CSRF_COOKIE_NAME,
        secrets.token_urlsafe(32),
        max_age=max_age,
        httponly=False,
        secure=secure,
        samesite="Lax",
        path=settings.REFRESH_COOKIE_PATH,
    )


def clear_refresh_cookies(response: HttpResponse) -> None:
    for name in (settings.REFRESH_COOKIE_NAME, settings.REFRESH_CSRF_COOKIE_NAME):
        response.delete_cookie(name, path=settings.REFRESH_COOKIE_PATH, samesite="Lax")


def read_refresh_token(request: HttpRequest, body_token: str | None) -> tuple[str | None, bool]:
    """Return (token, from_cookie). A body token is only honoured when enabled."""
    if body_token and settings.REFRESH_TOKEN_IN_BODY:
        return body_token, False
    cookie = request.COOKIES.get(settings.REFRESH_COOKIE_NAME)
    return (cookie, True) if cookie else (None, False)


def check_csrf(request: HttpRequest) -> None:
    cookie = request.COOKIES.get(settings.REFRESH_CSRF_COOKIE_NAME, "")
    header = request.headers.get(CSRF_HEADER, "")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise AppError("csrf_failed", _("CSRF token missing or incorrect."), 403)

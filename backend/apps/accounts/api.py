from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import Router

from apps.core.errors import AppError
from apps.core.http import get_client_ip

from . import tokens
from .auth import jwt_auth
from .cookies import check_csrf, clear_refresh_cookies, read_refresh_token, set_refresh_cookies
from .schemas import RefreshIn, TokenOut, UserOut

auth_router = Router(tags=["auth"])
users_router = Router(tags=["users"])


def token_response(request: HttpRequest, session: tokens.SessionTokens) -> HttpResponse:
    """Serialise a session: access token in body, refresh in cookie (and body if enabled)."""
    body = TokenOut(
        access_token=session.access_token,
        expires_in=session.expires_in,
        refresh_token=session.refresh_token if settings.REFRESH_TOKEN_IN_BODY else None,
    )
    response = HttpResponse(body.model_dump_json(), content_type="application/json")
    set_refresh_cookies(response, session.refresh_token)
    return response


@auth_router.post("/refresh", response=TokenOut, auth=None)
def refresh(request: HttpRequest, payload: RefreshIn | None = None) -> HttpResponse:
    raw, from_cookie = read_refresh_token(request, payload.refresh_token if payload else None)
    if raw is None:
        raise AppError("invalid_refresh_token", "Refresh token required.", 401)
    if from_cookie:
        check_csrf(request)
    session = tokens.rotate_refresh_token(
        raw, user_agent=request.headers.get("User-Agent", ""), ip=get_client_ip(request)
    )
    return token_response(request, session)


@auth_router.post("/logout", auth=None, response={204: None})
def logout(request: HttpRequest, payload: RefreshIn | None = None) -> HttpResponse:
    raw, from_cookie = read_refresh_token(request, payload.refresh_token if payload else None)
    if from_cookie:
        check_csrf(request)
    if raw:
        tokens.logout(raw)
    response = HttpResponse(status=204)
    clear_refresh_cookies(response)
    return response


@users_router.get("/me", response=UserOut, auth=jwt_auth)
def me(request: HttpRequest) -> UserOut:
    return UserOut.from_user(request.auth)  # type: ignore[attr-defined]

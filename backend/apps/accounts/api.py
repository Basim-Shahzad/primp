from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import Router, Status

from apps.core.errors import AppError
from apps.core.http import get_client_ip

from . import otp, services, tokens
from .auth import jwt_auth
from .cookies import check_csrf, clear_refresh_cookies, read_refresh_token, set_refresh_cookies
from .schemas import (
    GoogleIn,
    LoginIn,
    MessageOut,
    OTPRequestIn,
    OTPRequestOut,
    OTPVerifyIn,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    RefreshIn,
    RegisterIn,
    TokenIn,
    TokenOut,
    UserOut,
)
from .tasks import password_reset_task

auth_router = Router(tags=["auth"])
users_router = Router(tags=["users"])


def token_response(
    request: HttpRequest,
    session: tokens.SessionTokens,
    *,
    status: int = 200,
    is_new_user: bool | None = None,
) -> HttpResponse:
    """Serialise a session: access token in body, refresh in cookie (and body if enabled)."""
    body = TokenOut(
        access_token=session.access_token,
        expires_in=session.expires_in,
        refresh_token=session.refresh_token if settings.REFRESH_TOKEN_IN_BODY else None,
        is_new_user=is_new_user,
    )
    response = HttpResponse(body.model_dump_json(), content_type="application/json", status=status)
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


@auth_router.post("/register", response={201: TokenOut}, auth=None)
def register(request: HttpRequest, payload: RegisterIn) -> HttpResponse:
    session = services.register_with_email(
        name=payload.name, email=payload.email, password=payload.password, request=request
    )
    return token_response(request, session, status=201)


@auth_router.post("/login", response=TokenOut, auth=None)
def login(request: HttpRequest, payload: LoginIn) -> HttpResponse:
    session = services.login_with_password(
        email=payload.email, password=payload.password, request=request
    )
    return token_response(request, session)


@auth_router.post("/email/verify", response=MessageOut, auth=None)
def verify_email(request: HttpRequest, payload: TokenIn) -> MessageOut:
    services.verify_email(payload.token)
    return MessageOut(detail="Email verified.")


@auth_router.post("/otp/request", response={202: OTPRequestOut}, auth=None)
def otp_request(request: HttpRequest, payload: OTPRequestIn) -> Status[OTPRequestOut]:
    services.request_phone_otp(payload.phone, request)
    return Status(202, OTPRequestOut(expires_in=otp.OTP_TTL, resend_in=otp.RESEND_COOLDOWN))


@auth_router.post("/otp/verify", response=TokenOut, auth=None)
def otp_verify(request: HttpRequest, payload: OTPVerifyIn) -> HttpResponse:
    session, created = services.login_with_phone_otp(
        phone=payload.phone, code=payload.code, name=payload.name, request=request
    )
    return token_response(request, session, is_new_user=created)


@auth_router.post("/google", response=TokenOut, auth=None)
def google(request: HttpRequest, payload: GoogleIn) -> HttpResponse:
    session, created = services.login_with_google(id_token=payload.id_token, request=request)
    return token_response(request, session, is_new_user=created)


@auth_router.post("/password/reset/request", response=MessageOut, auth=None)
def password_reset_request(request: HttpRequest, payload: PasswordResetRequestIn) -> MessageOut:
    password_reset_task.delay(payload.email)
    return MessageOut(detail="If the address is registered, a reset link has been sent.")


@auth_router.post("/password/reset/confirm", response=MessageOut, auth=None)
def password_reset_confirm(request: HttpRequest, payload: PasswordResetConfirmIn) -> MessageOut:
    services.confirm_password_reset(token=payload.token, new_password=payload.new_password)
    return MessageOut(detail="Password updated.")


@users_router.get("/me", response=UserOut, auth=jwt_auth)
def me(request: HttpRequest) -> UserOut:
    return UserOut.from_user(request.auth)  # type: ignore[attr-defined]

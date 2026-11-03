import uuid
from typing import Any

from django.conf import settings
from django.http import HttpRequest, HttpResponse
from ninja import File, Query, Router, Status, UploadedFile

from apps.core.errors import AppError
from apps.core.http import get_client_ip
from apps.core.pagination import CursorParams
from apps.core.permissions import require_role

from . import otp, profile, ratelimits, services, tokens, wishlist
from .auth import jwt_auth
from .cookies import check_csrf, clear_refresh_cookies, read_refresh_token, set_refresh_cookies
from .roles import Role
from .schemas import (
    DeletionIn,
    DeletionOut,
    EmailChangeIn,
    GoogleIn,
    LoginIn,
    MessageOut,
    OTPRequestIn,
    OTPRequestOut,
    OTPVerifyIn,
    PasswordResetConfirmIn,
    PasswordResetRequestIn,
    PhoneChangeIn,
    PhoneConfirmIn,
    ProfilePatchIn,
    RefreshIn,
    RegisterIn,
    TokenIn,
    TokenOut,
    UserOut,
    WishlistItemOut,
    WishlistPageOut,
    WishlistStateOut,
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
    ratelimits.check(request, "refresh")
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
    ratelimits.check(request, "logout")
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
    ratelimits.check(request, "email_verify")
    services.verify_email(payload.token)
    return MessageOut(detail="Email verified.")


@auth_router.post("/otp/request", response={202: OTPRequestOut}, auth=None)
def otp_request(request: HttpRequest, payload: OTPRequestIn) -> Status[OTPRequestOut]:
    services.request_phone_otp(payload.phone, request)
    return Status(202, OTPRequestOut(expires_in=otp.OTP_TTL, resend_in=otp.RESEND_COOLDOWN))


@auth_router.post("/otp/verify", response=TokenOut, auth=None)
def otp_verify(request: HttpRequest, payload: OTPVerifyIn) -> HttpResponse:
    ratelimits.check(request, "otp_verify")
    session, created = services.login_with_phone_otp(
        phone=payload.phone, code=payload.code, name=payload.name, request=request
    )
    return token_response(request, session, is_new_user=created)


@auth_router.post("/google", response=TokenOut, auth=None)
def google(request: HttpRequest, payload: GoogleIn) -> HttpResponse:
    ratelimits.check(request, "google")
    session, created = services.login_with_google(id_token=payload.id_token, request=request)
    return token_response(request, session, is_new_user=created)


@auth_router.post("/password/reset/request", response=MessageOut, auth=None)
def password_reset_request(request: HttpRequest, payload: PasswordResetRequestIn) -> MessageOut:
    ratelimits.check(request, "password_reset_request")
    password_reset_task.delay(payload.email)
    return MessageOut(detail="If the address is registered, a reset link has been sent.")


@auth_router.post("/password/reset/confirm", response=MessageOut, auth=None)
def password_reset_confirm(request: HttpRequest, payload: PasswordResetConfirmIn) -> MessageOut:
    ratelimits.check(request, "password_reset_confirm")
    services.confirm_password_reset(token=payload.token, new_password=payload.new_password)
    return MessageOut(detail="Password updated.")


@users_router.get("/me", response=UserOut, auth=jwt_auth)
def me(request: HttpRequest) -> UserOut:
    return UserOut.from_user(request.auth)  # type: ignore[attr-defined]


@users_router.patch("/me", response=UserOut, auth=jwt_auth)
def update_me(request: HttpRequest, payload: ProfilePatchIn) -> UserOut:
    user = request.auth  # type: ignore[attr-defined]
    profile.update_profile(
        user,
        name=payload.name,
        default_city_id=payload.default_city_id,
        default_area_id=payload.default_area_id,
        fields_set=payload.model_fields_set,
    )
    return UserOut.from_user(user)


@users_router.post("/me/photo", response=UserOut, auth=jwt_auth)
def upload_photo(request: HttpRequest, file: File[UploadedFile]) -> UserOut:
    user = request.auth  # type: ignore[attr-defined]
    profile.set_photo(user, file.read())
    return UserOut.from_user(user)


@users_router.post("/me/phone/change", response={202: OTPRequestOut}, auth=jwt_auth)
def phone_change(request: HttpRequest, payload: PhoneChangeIn) -> Status[OTPRequestOut]:
    profile.request_phone_change(request.auth, payload.phone, get_client_ip(request))  # type: ignore[attr-defined]
    return Status(202, OTPRequestOut(expires_in=otp.OTP_TTL, resend_in=otp.RESEND_COOLDOWN))


@users_router.post("/me/phone/confirm", response=UserOut, auth=jwt_auth)
def phone_confirm(request: HttpRequest, payload: PhoneConfirmIn) -> UserOut:
    user = profile.confirm_phone_change(request.auth, payload.phone, payload.code)  # type: ignore[attr-defined]
    return UserOut.from_user(user)


@users_router.post("/me/email/change", response={202: MessageOut}, auth=jwt_auth)
def email_change(request: HttpRequest, payload: EmailChangeIn) -> Status[MessageOut]:
    profile.request_email_change(request.auth, payload.email)  # type: ignore[attr-defined]
    return Status(202, MessageOut(detail="Confirmation link sent to the new address."))


@users_router.post("/me/email/confirm", response=UserOut, auth=None)
def email_confirm(request: HttpRequest, payload: TokenIn) -> UserOut:
    return UserOut.from_user(profile.confirm_email_change(payload.token))


@users_router.post("/me/deletion-request", response={202: DeletionOut}, auth=jwt_auth)
def deletion_request(
    request: HttpRequest, payload: DeletionIn | None = None
) -> Status[DeletionOut]:
    record = profile.request_deletion(request.auth, payload.reason if payload else "")  # type: ignore[attr-defined]
    return Status(
        202, DeletionOut(status=record.status, requested_at=record.created_at.isoformat())
    )


def _customer(request: HttpRequest) -> Any:
    return require_role(Role.CUSTOMER)(request)


@users_router.get("/me/wishlist", auth=jwt_auth, response=WishlistPageOut)
def my_wishlist(request: HttpRequest, params: Query[CursorParams]) -> WishlistPageOut:
    user = _customer(request)
    cards, result = wishlist.page(user.pk, params.cursor, params.limit)
    added = {w.business_id: w.created_at for w in result.items}
    return WishlistPageOut(
        items=[
            WishlistItemOut(
                id=c.business.public_id,
                slug=c.business.slug,
                name=c.business.name,
                area=c.business.area.name if c.business.area else None,
                rating_avg=float(c.business.rating_avg),
                rating_count=c.business.rating_count,
                starting_price=c.starting_price,
                is_verified=c.business.is_verified,
                cover_photo_url=c.cover_url,
                next_available_slot=c.next_slot,
                added_at=added[c.business.pk],
            )
            for c in cards
        ],
        next_cursor=result.next_cursor,
    )


@users_router.post(
    "/me/wishlist/{business_id}",
    auth=jwt_auth,
    response={200: WishlistStateOut, 201: WishlistStateOut},
)
def add_to_wishlist(request: HttpRequest, business_id: uuid.UUID) -> Status[WishlistStateOut]:
    user = _customer(request)
    created = wishlist.add(user.pk, business_id)
    return Status(
        201 if created else 200, WishlistStateOut(business_id=business_id, wishlisted=True)
    )


@users_router.delete("/me/wishlist/{business_id}", auth=jwt_auth, response={204: None})
def remove_from_wishlist(request: HttpRequest, business_id: uuid.UUID) -> Status[None]:
    user = _customer(request)
    wishlist.remove(user.pk, business_id)
    return Status(204, None)

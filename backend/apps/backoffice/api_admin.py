"""Admin session endpoints: login, whoami, re-authentication."""

from django.http import HttpRequest, HttpResponse
from ninja import Router

from apps.accounts import selectors as accounts
from apps.accounts import services as account_services
from apps.accounts import tokens
from apps.accounts.api import token_response
from apps.accounts.auth import user_from_token
from apps.core import audit, ratelimit
from apps.core.errors import AppError, forbidden
from apps.core.http import get_client_ip

from . import permissions
from .permissions import admin_auth
from .schemas import AdminLoginIn, AdminMeOut, ReauthIn, ReauthOut

router = Router(tags=["admin"], auth=admin_auth)
LOGIN_LIMIT = (10, 600)  # attempts per IP per 10 minutes, on top of the shared login limits
REAUTH_LIMIT = (5, 900)


def admin_user(request: HttpRequest):  # type: ignore[no-untyped-def]
    """Any admin account (no specific permission needed)."""
    user = getattr(request, "auth", None)
    if user is None:
        raise AppError("unauthenticated", "Authentication required.", 401)
    if user.role not in permissions.ADMIN_ROLES:
        raise forbidden()
    return user


@router.post("/auth/login", auth=None, response=dict[str, object])
def admin_login(request: HttpRequest, payload: AdminLoginIn) -> HttpResponse:
    """Admin-only password login (stricter throttle; other roles look like a bad login)."""
    ip = get_client_ip(request) or "unknown"
    ratelimit.enforce(f"admin-login:{ip}", limit=LOGIN_LIMIT[0], window_seconds=LOGIN_LIMIT[1])
    session = account_services.login_with_password(
        email=payload.email, password=payload.password, request=request
    )
    user = user_from_token(session.access_token)
    if user is None or user.role not in permissions.ADMIN_ROLES:
        tokens.revoke_family(session.family_id)
        raise AppError("invalid_credentials", "Incorrect email or password.", 401)
    audit.record("admin.login", actor_id=user.pk, target_type="user", target_id=user.pk, ip=ip)
    permissions.mark_reauthenticated(str(session.family_id))  # a fresh password login counts
    return token_response(request, session)


@router.get("/me", response=AdminMeOut)
def me(request: HttpRequest) -> AdminMeOut:
    user = admin_user(request)
    profile = accounts.profile_of(user)
    sid = getattr(request, "admin_session_id", "")
    return AdminMeOut(
        id=user.public_id,
        email=user.email,
        name=profile.name if profile else "",
        role=user.role,
        groups=sorted(user.groups.values_list("name", flat=True)),
        permissions=sorted(permissions.permissions_of(user)),
        reauth_valid=permissions.recently_reauthenticated(sid),
    )


@router.post("/reauth", response=ReauthOut)
def reauth(request: HttpRequest, payload: ReauthIn) -> ReauthOut:
    user = admin_user(request)
    ratelimit.enforce(
        f"admin-reauth:{user.pk}", limit=REAUTH_LIMIT[0], window_seconds=REAUTH_LIMIT[1]
    )
    if not user.check_password(payload.password):
        audit.record(
            "admin.reauth_failed",
            actor_id=user.pk,
            target_type="user",
            target_id=user.pk,
            ip=get_client_ip(request),
        )
        raise AppError("invalid_credentials", "Incorrect password.", 401)
    permissions.mark_reauthenticated(getattr(request, "admin_session_id", ""))
    return ReauthOut(valid_for_seconds=permissions.REAUTH_WINDOW_SECONDS)

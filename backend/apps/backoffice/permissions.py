"""Admin permissions, the three admin groups and request-level checks (Plan 6.3, Step 1)."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from django.core.cache import cache
from django.http import HttpRequest
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from ninja.security import HttpBearer

from apps.accounts.auth import user_from_token
from apps.accounts.roles import Role
from apps.accounts.tokens import decode_access_token
from apps.core.errors import AppError, forbidden

APP_LABEL = "backoffice"
ANCHOR_MODEL = "adminpermission"
REAUTH_WINDOW_SECONDS = 15 * 60
ADMIN_ROLES = (Role.ADMIN, Role.SUPER_ADMIN)

SUPPORT_AGENT = "support_agent"
OPS_ADMIN = "ops_admin"
SUPER_ADMIN = "super_admin"

_SUPPORT = [
    "users.view",
    "users.force_logout",
    "users.reset_password",
    "tickets.view",
    "tickets.handle",
    "reviews.moderate",
]
_OPS = [
    *_SUPPORT,
    "users.suspend",
    "businesses.review",
    "businesses.revoke",
    "documents.view",
    "taxonomy.manage",
    "geo.manage",
    "banners.manage",
    "points.adjust",
    "audit.view",
]
_SUPER = [*_OPS, "admins.manage", "config.manage"]

GROUP_PERMISSIONS: dict[str, list[str]] = {
    SUPPORT_AGENT: _SUPPORT,
    OPS_ADMIN: _OPS,
    SUPER_ADMIN: _SUPER,
}
ALL_PERMISSIONS = sorted(set(_SUPER))
PERMISSION_CHOICES: tuple[tuple[str, str], ...] = tuple(
    (code, code.replace(".", " ").replace("_", " ").capitalize()) for code in ALL_PERMISSIONS
)
HIGH_RISK = {
    "users.suspend",
    "businesses.revoke",
    "points.adjust",
    "config.manage",
    "admins.manage",
}


def sync_groups(apps: Any) -> None:
    """Create the anchor content type, all permissions and the three groups (idempotent).

    Works with the real app registry (post_migrate) and a historical one (data migration).
    """
    content_type_model = apps.get_model("contenttypes", "ContentType")
    permission_model = apps.get_model("auth", "Permission")
    group_model = apps.get_model("auth", "Group")
    ct, _created = content_type_model.objects.get_or_create(app_label=APP_LABEL, model=ANCHOR_MODEL)
    permissions = {}
    for code, name in PERMISSION_CHOICES:
        perm, _c = permission_model.objects.get_or_create(
            content_type=ct, codename=code, defaults={"name": name[:255]}
        )
        permissions[code] = perm
    for group_name, codes in GROUP_PERMISSIONS.items():
        group, _c = group_model.objects.get_or_create(name=group_name)
        group.permissions.set([permissions[c] for c in codes])


# ---------------------------------------------------------------- actor


@dataclass(frozen=True)
class AdminActor:
    """The authenticated admin plus what they may do. Services call `require`."""

    user: Any
    permissions: frozenset[str]
    session_id: str
    ip: str | None

    @property
    def id(self) -> int:
        return int(self.user.pk)

    def can(self, permission: str) -> bool:
        return permission in self.permissions

    def require(self, permission: str, *, reauth: bool | None = None) -> None:
        """Raise 403 unless allowed; high-risk permissions also need a recent re-authentication."""
        if not self.can(permission):
            raise forbidden()
        needs_reauth = permission in HIGH_RISK if reauth is None else reauth
        if needs_reauth and not recently_reauthenticated(self.session_id):
            raise reauth_required()


def reauth_required() -> AppError:
    return AppError("reauth_required", _("Please confirm your password to continue."), 403)


def _reauth_key(session_id: str) -> str:
    return f"admin-reauth:{session_id}"


def mark_reauthenticated(session_id: str) -> None:
    cache.set(_reauth_key(session_id), timezone.now().timestamp(), timeout=24 * 3600)


def recently_reauthenticated(session_id: str) -> bool:
    stamp = cache.get(_reauth_key(session_id))
    return (
        stamp is not None and (timezone.now().timestamp() - float(stamp)) <= REAUTH_WINDOW_SECONDS
    )


def permissions_of(user: Any) -> frozenset[str]:
    """Permissions an admin account holds. Non-admin roles hold none."""
    if getattr(user, "role", None) not in ADMIN_ROLES:
        return frozenset()
    if user.role == Role.SUPER_ADMIN:
        return frozenset(ALL_PERMISSIONS)
    from django.contrib.auth.models import Permission

    return frozenset(
        Permission.objects.filter(
            group__user=user, content_type__app_label=APP_LABEL, content_type__model=ANCHOR_MODEL
        ).values_list("codename", flat=True)
    )


# ---------------------------------------------------------------- Ninja glue


class AdminAuth(HttpBearer):
    """Any valid access token authenticates (anonymous -> 401); authorisation is per endpoint."""

    def authenticate(self, request: HttpRequest, token: str) -> Any:
        user = user_from_token(token)
        if user is None:
            return None
        claims = decode_access_token(token)
        request.admin_session_id = str(claims["sid"]) if claims else ""  # type: ignore[attr-defined]
        return user


admin_auth = AdminAuth()


def require_permission(
    permission: str, *, reauth: bool | None = None
) -> Callable[[HttpRequest], AdminActor]:
    """Call at the top of an endpoint: `actor = require_permission("users.view")(request)`."""

    def check(request: HttpRequest) -> AdminActor:
        from apps.core.http import get_client_ip

        user = getattr(request, "auth", None)
        if user is None:
            raise AppError("unauthenticated", "Authentication required.", 401)
        actor = AdminActor(
            user=user,
            permissions=permissions_of(user),
            session_id=getattr(request, "admin_session_id", ""),
            ip=get_client_ip(request),
        )
        actor.require(permission, reauth=reauth)
        return actor

    return check

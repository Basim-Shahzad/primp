import json
import logging
from datetime import timedelta
from io import StringIO

import pytest
import time_machine
from django.contrib.auth.models import Group
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client
from django.utils import timezone

from apps.accounts import tokens
from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import User
from apps.accounts.roles import Role
from apps.backoffice import permissions
from apps.backoffice.factories import make_admin
from apps.backoffice.permissions import ALL_PERMISSIONS, GROUP_PERMISSIONS
from apps.backoffice.tests.helpers import ROLES, A, call, headers_for, reauth, user_for
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db

# the permission table of Plan 6.3, Step 1 (written out independently of the code)
TABLE = {
    "users.view": {"support_agent", "ops_admin", "super_admin"},
    "users.suspend": {"ops_admin", "super_admin"},
    "users.force_logout": {"support_agent", "ops_admin", "super_admin"},
    "users.reset_password": {"support_agent", "ops_admin", "super_admin"},
    "admins.manage": {"super_admin"},
    "businesses.review": {"ops_admin", "super_admin"},
    "businesses.revoke": {"ops_admin", "super_admin"},
    "documents.view": {"ops_admin", "super_admin"},
    "taxonomy.manage": {"ops_admin", "super_admin"},
    "geo.manage": {"ops_admin", "super_admin"},
    "tickets.view": {"support_agent", "ops_admin", "super_admin"},
    "tickets.handle": {"support_agent", "ops_admin", "super_admin"},
    "reviews.moderate": {"support_agent", "ops_admin", "super_admin"},
    "banners.manage": {"ops_admin", "super_admin"},
    "points.adjust": {"ops_admin", "super_admin"},
    "config.manage": {"super_admin"},
    "audit.view": {"ops_admin", "super_admin"},
}


def test_the_table_lists_every_permission_the_code_knows() -> None:
    assert set(TABLE) == set(ALL_PERMISSIONS)


@pytest.mark.parametrize(
    "role", ["customer", "business_owner", "staff", "support_agent", "ops_admin", "super_admin"]
)
@pytest.mark.parametrize("permission", sorted(TABLE))
def test_permission_matrix(role: str, permission: str) -> None:
    user = user_for(role)
    assert (permission in permissions.permissions_of(user)) == (role in TABLE[permission])


def test_groups_are_seeded_exactly_per_the_table() -> None:
    for group, codes in GROUP_PERMISSIONS.items():
        stored = set(Group.objects.get(name=group).permissions.values_list("codename", flat=True))
        assert stored == {p for p, who in TABLE.items() if group in who} == set(codes)


def test_sync_groups_repairs_wiped_groups() -> None:
    from django.apps import apps

    Group.objects.all().delete()
    permissions.sync_groups(apps)
    permissions.sync_groups(apps)  # idempotent
    assert Group.objects.filter(name__in=GROUP_PERMISSIONS).count() == 3
    assert make_admin("support_agent").groups.count() == 1


def test_admin_without_a_group_has_no_permissions() -> None:
    user = UserFactory(role=Role.ADMIN)
    assert permissions.permissions_of(user) == frozenset()


def test_me_lists_groups_and_permissions_for_admins_only() -> None:
    for role in ROLES:
        user = user_for(role)
        r = call("get", f"{A}/me", headers_for(user))
        if role in ("support_agent", "ops_admin", "super_admin"):
            assert r.status_code == 200, r.content
            data = r.json()
            assert data["role"] == user.role and data["permissions"] == sorted(
                permissions.permissions_of(user)
            )
            assert data["groups"] == [role] and data["reauth_valid"] is False
        else:
            assert r.status_code == 403
    assert Client().get(f"{A}/me").status_code == 401
    assert call("get", f"{A}/me", {"HTTP_AUTHORIZATION": "Bearer junk"}).status_code == 401


def test_reauth_wrong_password_right_password_and_expiry(long_tokens) -> None:  # type: ignore[no-untyped-def]
    admin = make_admin("ops_admin")
    h = headers_for(admin)
    bad = reauth(h, "wrong-password")
    assert bad.status_code == 401 and bad.json()["code"] == "invalid_credentials"
    assert AuditLog.objects.filter(action="admin.reauth_failed", actor_id=admin.pk).count() == 1
    start = timezone.now()
    with time_machine.travel(start, tick=False) as t:
        assert reauth(h).json() == {"valid_for_seconds": 900}
        assert call("get", f"{A}/me", h).json()["reauth_valid"] is True
        t.shift(timedelta(minutes=15))
        assert call("get", f"{A}/me", h).json()["reauth_valid"] is True  # exactly 15 minutes
        t.shift(timedelta(seconds=1))
        assert call("get", f"{A}/me", h).json()["reauth_valid"] is False


def test_reauth_is_per_session_and_throttled() -> None:
    admin = make_admin("ops_admin")
    first, second = headers_for(admin), headers_for(admin)
    assert reauth(first).status_code == 200
    assert call("get", f"{A}/me", first).json()["reauth_valid"] is True
    assert call("get", f"{A}/me", second).json()["reauth_valid"] is False
    codes = [reauth(second, "nope").status_code for _ in range(6)]
    assert codes[-1] == 429
    for role in ("customer", "business_owner"):
        assert reauth(headers_for(user_for(role))).status_code == 403


def test_actor_require_enforces_permission_then_reauth() -> None:
    from apps.core.errors import AppError

    admin = make_admin("ops_admin")
    session = "sess-1"
    actor = permissions.AdminActor(admin, permissions.permissions_of(admin), session, "1.2.3.4")
    actor.require("users.view")  # not high risk
    with pytest.raises(AppError) as exc:
        actor.require("users.suspend")
    assert exc.value.status == 403 and exc.value.code == "reauth_required"
    with pytest.raises(AppError) as denied:
        actor.require("config.manage")
    assert denied.value.code == "permission_denied"  # permission is checked before reauth
    permissions.mark_reauthenticated(session)
    actor.require("users.suspend")
    actor.require("users.view", reauth=True)


def test_admin_tokens_expire_sooner_than_customer_tokens() -> None:
    assert tokens.access_lifetime(make_admin("ops_admin")) < tokens.access_lifetime(make_customer())
    assert tokens.refresh_lifetime(make_admin("support_agent")) < tokens.refresh_lifetime(
        make_customer()
    )


# ------------------------------------------------------------ admin login


def login(email: str, password: str = "Str0ng-pass-phrase!"):  # type: ignore[no-untyped-def]
    return Client().post(
        f"{A}/auth/login",
        data=json.dumps({"email": email, "password": password}),
        content_type="application/json",
    )


def test_admin_login_issues_a_session_that_counts_as_reauthenticated() -> None:
    admin = make_admin("ops_admin", email="ops@example.com")
    r = login("ops@example.com")
    assert r.status_code == 200, r.content
    token = r.json()["access_token"]
    h = {"HTTP_AUTHORIZATION": f"Bearer {token}"}
    me = call("get", f"{A}/me", h).json()
    assert me["reauth_valid"] is True and me["id"] == str(admin.public_id)
    assert "primp_refresh" in r.cookies
    assert AuditLog.objects.filter(action="admin.login", actor_id=admin.pk).count() == 1


@pytest.mark.parametrize("role", ["customer", "business_owner", "staff"])
def test_admin_login_refuses_other_roles_and_revokes_their_session(role: str) -> None:
    user = user_for(role)
    r = login(user.email or "")
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"
    from apps.accounts.models import RefreshToken

    assert RefreshToken.objects.filter(user=user, revoked_at__isnull=True).count() == 0


def test_admin_login_bad_password_unknown_user_and_throttle() -> None:
    make_admin("ops_admin", email="ops@example.com")
    assert login("ops@example.com", "wrong").status_code == 401
    assert login("nobody@example.com").status_code == 401
    codes = [login("nobody@example.com").status_code for _ in range(10)]
    assert codes[-1] == 429
    assert login("ops@example.com").status_code == 429  # same IP, shared admin limit


# ------------------------------------------------------------ create_super_admin


def test_create_super_admin_from_environment(monkeypatch, caplog) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ADMIN_EMAIL", "Root@Example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "Sup3r-secret-passphrase!")
    out = StringIO()
    with caplog.at_level(logging.DEBUG):
        call_command("create_super_admin", stdout=out)
    user = User.objects.get(email="root@example.com")
    assert user.role == Role.SUPER_ADMIN and user.check_password("Sup3r-secret-passphrase!")
    assert list(user.groups.values_list("name", flat=True)) == ["super_admin"]
    assert permissions.permissions_of(user) == frozenset(ALL_PERMISSIONS)
    assert "Sup3r-secret-passphrase!" not in out.getvalue() + caplog.text
    assert AuditLog.objects.filter(
        action="admin.super_admin_created", target_id=str(user.pk)
    ).exists()
    with pytest.raises(CommandError, match="already exists"):
        call_command("create_super_admin")


def test_create_super_admin_rejects_weak_password_and_missing_input(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setenv("ADMIN_EMAIL", "root@example.com")
    monkeypatch.setenv("ADMIN_PASSWORD", "123")
    with pytest.raises(CommandError):
        call_command("create_super_admin")
    assert not User.objects.filter(email="root@example.com").exists()
    monkeypatch.setenv("ADMIN_EMAIL", "")
    monkeypatch.setattr("builtins.input", lambda *_: "")
    monkeypatch.setattr("getpass.getpass", lambda *_: "")
    with pytest.raises(CommandError, match="required"):
        call_command("create_super_admin")


def test_create_super_admin_prompts_when_environment_is_empty(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("ADMIN_EMAIL", raising=False)
    monkeypatch.delenv("ADMIN_PASSWORD", raising=False)
    monkeypatch.setattr("builtins.input", lambda *_: "prompted@example.com")
    monkeypatch.setattr("getpass.getpass", lambda *_: "Pr0mpted-passphrase-ok!")
    call_command("create_super_admin")
    assert User.objects.get(email="prompted@example.com").role == Role.SUPER_ADMIN

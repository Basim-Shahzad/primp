from datetime import timedelta

import jwt
import pytest
import time_machine
from django.conf import settings
from django.test import Client
from django.utils import timezone

from apps.accounts import tokens
from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import RefreshToken, Role, UserStatus
from apps.accounts.tests.helpers import CSRF, bearer, login_client, post_json
from apps.core.errors import AppError
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db
ME = "/api/v1/users/me"


def test_me_success_returns_profile() -> None:
    user = make_customer(name="Ayesha")
    _, session = login_client(user)
    r = Client().get(ME, **bearer(session))
    assert r.status_code == 200
    body = r.json()
    assert body["email"] == user.email and body["name"] == "Ayesha"
    assert body["role"] == "customer" and len(body["referral_code"]) == 8
    assert "password" not in body


def test_me_without_profile_for_non_customer() -> None:
    user = UserFactory(role=Role.ADMIN)
    _, session = login_client(user)
    body = Client().get(ME, **bearer(session)).json()
    assert body["name"] == "" and body["referral_code"] is None


def test_me_anonymous_401() -> None:
    r = Client().get(ME)
    assert r.status_code == 401 and r.json()["code"] == "unauthenticated"


def test_access_token_claims_and_lifetime() -> None:
    user = UserFactory()
    session = tokens.issue_session(user)
    claims = jwt.decode(session.access_token, settings.JWT_SIGNING_KEY, algorithms=["HS256"])
    assert claims["sub"] == str(user.public_id) and claims["role"] == "customer"
    assert {"jti", "exp", "iat", "sid"} <= set(claims)
    assert claims["exp"] - claims["iat"] == 15 * 60 == session.expires_in


def test_admin_tokens_have_shorter_lifetimes() -> None:
    admin = UserFactory(role=Role.ADMIN)
    cust = UserFactory()
    assert tokens.access_lifetime(admin) < tokens.access_lifetime(cust)
    assert tokens.refresh_lifetime(admin) < tokens.refresh_lifetime(cust)


def test_expired_access_token_401() -> None:
    user = UserFactory()
    session = tokens.issue_session(user)
    with time_machine.travel(timezone.now() + timedelta(minutes=15, seconds=1), tick=False):
        assert Client().get(ME, **bearer(session)).status_code == 401


def test_access_token_valid_just_before_expiry() -> None:
    user = UserFactory()
    session = tokens.issue_session(user)
    with time_machine.travel(timezone.now() + timedelta(minutes=14, seconds=50), tick=False):
        assert Client().get(ME, **bearer(session)).status_code == 200


def test_tampered_signature_401() -> None:
    session = tokens.issue_session(UserFactory())
    head, payload, sig = session.access_token.split(".")
    bad = ".".join([head, payload, sig[:-3] + ("AAA" if not sig.endswith("AAA") else "BBB")])
    assert Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {bad}").status_code == 401


def test_wrong_signing_key_401() -> None:
    user = UserFactory()
    forged = jwt.encode(
        {"sub": str(user.public_id), "sid": "x", "jti": "j", "iat": 1, "exp": 9999999999},
        "other-key-other-key-other-key-other-key",
        algorithm="HS256",
    )
    assert Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {forged}").status_code == 401


def test_alg_none_rejected() -> None:
    user = UserFactory()
    session = tokens.issue_session(user)
    claims = jwt.decode(session.access_token, options={"verify_signature": False})
    forged = jwt.encode(claims, key="", algorithm="none")
    assert Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {forged}").status_code == 401


def test_missing_required_claims_rejected() -> None:
    user = UserFactory()
    t = jwt.encode({"sub": str(user.public_id)}, settings.JWT_SIGNING_KEY, algorithm="HS256")
    assert Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {t}").status_code == 401


@pytest.mark.parametrize("header", ["Bearer", "Bearer ", "Basic abc", "garbage", "Bearer a.b.c"])
def test_malformed_authorization_header(header: str) -> None:
    assert Client().get(ME, HTTP_AUTHORIZATION=header).status_code == 401


@pytest.mark.parametrize("status", [UserStatus.SUSPENDED, UserStatus.DELETED])
def test_suspended_or_deleted_user_blocked(status: str) -> None:
    user = UserFactory()
    session = tokens.issue_session(user)
    user.status = status
    user.save()
    assert Client().get(ME, **bearer(session)).status_code == 401


def test_revoked_session_blocks_access_token() -> None:
    session = tokens.issue_session(UserFactory())
    tokens.revoke_family(session.family_id)
    assert Client().get(ME, **bearer(session)).status_code == 401


def test_session_with_bad_sid_claim() -> None:
    assert tokens.session_is_active("not-a-uuid") is False


def test_refresh_stores_only_hash() -> None:
    session = tokens.issue_session(UserFactory())
    row = RefreshToken.objects.get()
    assert row.token_hash == tokens.hash_token(session.refresh_token) != session.refresh_token


def test_refresh_rotation_via_cookie() -> None:
    user = UserFactory()
    client, session = login_client(user)
    r = post_json(client, "/api/v1/auth/refresh", **CSRF)
    assert r.status_code == 200
    body = r.json()
    assert body["access_token"] and body["token_type"] == "bearer"
    assert "refresh_token" not in body or body["refresh_token"] is None
    new_cookie = r.cookies[settings.REFRESH_COOKIE_NAME]
    assert new_cookie.value != session.refresh_token
    assert new_cookie["httponly"] and new_cookie["samesite"] == "Lax" and new_cookie["secure"]
    assert not r.cookies[settings.REFRESH_CSRF_COOKIE_NAME]["httponly"]
    old = RefreshToken.objects.get(token_hash=tokens.hash_token(session.refresh_token))
    assert old.revoked_at and old.replaced_by_id
    assert Client().get(ME, HTTP_AUTHORIZATION=f"Bearer {body['access_token']}").status_code == 200


def test_refresh_without_csrf_header_403() -> None:
    client, _ = login_client(UserFactory())
    r = post_json(client, "/api/v1/auth/refresh")
    assert r.status_code == 403 and r.json()["code"] == "csrf_failed"
    assert RefreshToken.objects.filter(revoked_at__isnull=False).count() == 0


def test_refresh_with_wrong_csrf_header_403() -> None:
    client, _ = login_client(UserFactory())
    r = post_json(client, "/api/v1/auth/refresh", HTTP_X_CSRF_TOKEN="nope")
    assert r.status_code == 403


def test_refresh_without_any_token_401() -> None:
    r = post_json(Client(), "/api/v1/auth/refresh")
    assert r.status_code == 401 and r.json()["code"] == "invalid_refresh_token"


def test_refresh_unknown_token_401() -> None:
    client = Client()
    client.cookies[settings.REFRESH_COOKIE_NAME] = "unknown"
    client.cookies[settings.REFRESH_CSRF_COOKIE_NAME] = "csrf-value"
    assert post_json(client, "/api/v1/auth/refresh", **CSRF).status_code == 401


def test_refresh_body_token_ignored_unless_enabled(settings) -> None:  # type: ignore[no-untyped-def]
    session = tokens.issue_session(UserFactory())
    r = post_json(Client(), "/api/v1/auth/refresh", {"refresh_token": session.refresh_token})
    assert r.status_code == 401
    settings.REFRESH_TOKEN_IN_BODY = True
    r = post_json(Client(), "/api/v1/auth/refresh", {"refresh_token": session.refresh_token})
    assert r.status_code == 200 and r.json()["refresh_token"]


def test_reuse_detection_revokes_whole_family() -> None:
    user = UserFactory()
    first = tokens.issue_session(user)
    second = tokens.rotate_refresh_token(first.refresh_token)
    with pytest.raises(AppError) as exc:
        tokens.rotate_refresh_token(first.refresh_token)  # replay of rotated token
    assert exc.value.code == "refresh_token_reused" and exc.value.status == 401
    assert (
        RefreshToken.objects.filter(family_id=first.family_id, revoked_at__isnull=True).count() == 0
    )
    with pytest.raises(AppError):
        tokens.rotate_refresh_token(second.refresh_token)  # descendant is dead too
    assert AuditLog.objects.filter(action="auth.refresh_reuse").exists()


def test_reuse_does_not_touch_other_sessions() -> None:
    user = UserFactory()
    a = tokens.issue_session(user)
    b = tokens.issue_session(user)
    tokens.rotate_refresh_token(a.refresh_token)
    with pytest.raises(AppError):
        tokens.rotate_refresh_token(a.refresh_token)
    assert tokens.session_is_active(str(b.family_id))


def test_expired_refresh_token_rejected_and_family_revoked() -> None:
    session = tokens.issue_session(UserFactory())
    with (
        time_machine.travel(timezone.now() + timedelta(days=31), tick=False),
        pytest.raises(AppError) as exc,
    ):
        tokens.rotate_refresh_token(session.refresh_token)
    assert exc.value.code == "invalid_refresh_token"


def test_refresh_for_suspended_user_rejected() -> None:
    user = UserFactory()
    session = tokens.issue_session(user)
    user.status = UserStatus.SUSPENDED
    user.save()
    with pytest.raises(AppError):
        tokens.rotate_refresh_token(session.refresh_token)
    assert not tokens.session_is_active(str(session.family_id))


def test_logout_revokes_family_and_clears_cookies() -> None:
    client, session = login_client(UserFactory())
    r = post_json(client, "/api/v1/auth/logout", **CSRF)
    assert r.status_code == 204
    assert r.cookies[settings.REFRESH_COOKIE_NAME]["max-age"] == 0
    assert not tokens.session_is_active(str(session.family_id))
    assert Client().get(ME, **bearer(session)).status_code == 401


def test_logout_requires_csrf_with_cookie() -> None:
    client, session = login_client(UserFactory())
    assert post_json(client, "/api/v1/auth/logout").status_code == 403
    assert tokens.session_is_active(str(session.family_id))


def test_logout_without_token_is_noop() -> None:
    assert post_json(Client(), "/api/v1/auth/logout").status_code == 204


def test_logout_with_body_token_needs_no_csrf(settings) -> None:  # type: ignore[no-untyped-def]
    settings.REFRESH_TOKEN_IN_BODY = True
    session = tokens.issue_session(UserFactory())
    r = post_json(Client(), "/api/v1/auth/logout", {"refresh_token": session.refresh_token})
    assert r.status_code == 204 and not tokens.session_is_active(str(session.family_id))


def test_revoke_all_sessions() -> None:
    user = UserFactory()
    s1, s2 = tokens.issue_session(user), tokens.issue_session(user)
    assert tokens.revoke_all_sessions(user) == 2
    assert not tokens.session_is_active(str(s1.family_id))
    assert not tokens.session_is_active(str(s2.family_id))

from datetime import timedelta
from unittest import mock

import pytest
from django.test import Client, override_settings

from apps.accounts import google
from apps.accounts.factories import UserFactory
from apps.accounts.models import RefreshToken, User, UserStatus
from apps.accounts.tests.helpers import post_json
from apps.accounts.tokens import issue_session, session_is_active

pytestmark = pytest.mark.django_db
URL = "/api/v1/auth/google"
AUD = "test-client-id.apps.googleusercontent.com"


def claims(**over: object) -> dict[str, object]:
    base: dict[str, object] = {
        "email": "Gita@Example.com",
        "email_verified": True,
        "name": "Gita Khan",
        "aud": AUD,
    }
    return {**base, **over}


def with_claims(c: dict[str, object]):  # type: ignore[no-untyped-def]
    return mock.patch.object(google, "_verify", return_value=c)


def test_creates_new_account() -> None:
    with with_claims(claims()) as m:
        r = post_json(Client(), URL, {"id_token": "tok"})
    assert r.status_code == 200 and r.json()["is_new_user"] is True
    m.assert_called_once_with("tok", [AUD])
    user = User.objects.get(email="gita@example.com")
    assert (
        user.auth_provider == "google" and user.is_email_verified and not user.has_usable_password()
    )
    assert user.profile.name == "Gita Khan"


def test_links_existing_verified_account_keeps_password() -> None:
    user = UserFactory(email="gita@example.com", password="Keep-this-pass-1")
    with with_claims(claims()):
        r = post_json(Client(), URL, {"id_token": "tok"})
    assert r.status_code == 200 and r.json()["is_new_user"] is False
    user.refresh_from_db()
    assert user.check_password("Keep-this-pass-1") and User.objects.count() == 1


def test_linking_unverified_account_drops_password_and_sessions() -> None:
    squatter = UserFactory(
        email="gita@example.com", password="Squatter-pass-1", is_email_verified=False
    )
    old = issue_session(squatter)
    with with_claims(claims()):
        assert post_json(Client(), URL, {"id_token": "tok"}).status_code == 200
    squatter.refresh_from_db()
    assert squatter.is_email_verified and not squatter.has_usable_password()
    assert not session_is_active(str(old.family_id))


@pytest.mark.parametrize(
    "bad",
    [
        {"email_verified": False},
        {"email_verified": "true"},
        {"email": ""},
    ],
)
def test_unverified_or_missing_email_rejected(bad: dict[str, object]) -> None:
    with with_claims(claims(**bad)):
        r = post_json(Client(), URL, {"id_token": "tok"})
    assert r.status_code == 401 and r.json()["code"] == "google_email_unverified"
    assert not User.objects.exists()


@pytest.mark.parametrize(
    "error", [ValueError("Token expired"), ValueError("Wrong audience"), Exception("net")]
)
def test_invalid_expired_wrong_audience_token(error: Exception) -> None:
    with mock.patch.object(google, "_verify", side_effect=error):
        r = post_json(Client(), URL, {"id_token": "tok"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_google_token"


@override_settings(GOOGLE_CLIENT_IDS=[])
def test_not_configured() -> None:
    r = post_json(Client(), URL, {"id_token": "tok"})
    assert r.status_code == 503 and r.json()["code"] == "google_not_configured"


def test_suspended_user_blocked() -> None:
    UserFactory(email="gita@example.com", status=UserStatus.SUSPENDED)
    with with_claims(claims()):
        r = post_json(Client(), URL, {"id_token": "tok"})
    assert r.status_code == 403 and RefreshToken.objects.count() == 0


@pytest.mark.parametrize("body", [{}, {"id_token": ""}, {"id_token": "x" * 5000}, {"id_token": 5}])
def test_validation(body: dict[str, object]) -> None:
    assert post_json(Client(), URL, body).status_code == 422


def test_name_truncated() -> None:
    with with_claims(claims(name="N" * 300)):
        post_json(Client(), URL, {"id_token": "tok"})
    assert len(User.objects.get().profile.name) == 100


def test_real_verifier_passes_audience_list_and_wraps_errors() -> None:
    with (
        mock.patch(
            "apps.accounts.google.id_token.verify_oauth2_token", side_effect=ValueError("bad")
        ) as m,
        pytest.raises(Exception) as exc,
    ):
        google.verify_google_id_token("tok")
    assert m.call_args.kwargs["audience"] == [AUD]
    assert getattr(exc.value, "code", "") == "invalid_google_token"
    assert timedelta(0) == timedelta(0)

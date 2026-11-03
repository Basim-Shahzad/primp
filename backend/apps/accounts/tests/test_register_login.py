import threading
from datetime import timedelta
from unittest import mock

import pytest
import time_machine
from django.core import mail
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts import services, tokens
from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import CustomerProfile, RefreshToken, User, UserStatus
from apps.accounts.tests.helpers import post_json
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db
REGISTER = "/api/v1/auth/register"
LOGIN = "/api/v1/auth/login"
GOOD = {"name": "Sana Malik", "email": "sana@example.com", "password": "Tr0ub4dor&3-horse"}


def test_register_success(run_commit) -> None:  # type: ignore[no-untyped-def]
    client = Client()
    with run_commit():
        r = post_json(client, REGISTER, GOOD)
    assert r.status_code == 201
    body = r.json()
    assert body["access_token"] and body["expires_in"] == 900
    user = User.objects.get(email="sana@example.com")
    assert user.role == "customer" and user.auth_provider == "password"
    assert not user.is_email_verified and user.check_password(GOOD["password"])
    assert CustomerProfile.objects.get(user=user).name == "Sana Malik"
    assert RefreshToken.objects.filter(user=user).count() == 1
    assert len(mail.outbox) == 1 and "verify-email?token=" in mail.outbox[0].body
    assert mail.outbox[0].to == ["sana@example.com"]


def test_register_normalises_email_case_and_name() -> None:
    post_json(Client(), REGISTER, {**GOOD, "email": "Sana@Example.COM", "name": "  Sana  "})
    user = User.objects.get(email="sana@example.com")
    assert user.profile.name == "Sana"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", ""),
        ("name", "   "),
        ("name", "x" * 101),
        ("name", 5),
        ("email", ""),
        ("email", "not-an-email"),
        ("email", "a@b"),
        ("email", "x" * 250 + "@example.com"),
        ("password", ""),
        ("password", "x" * 129),
    ],
)
def test_register_validation(field: str, value: object) -> None:
    r = post_json(Client(), REGISTER, {**GOOD, field: value})
    assert r.status_code == 422
    assert r.json()["code"] == "validation_error" and field in r.json()["fields"]
    assert User.objects.count() == 0


@pytest.mark.parametrize("missing", ["name", "email", "password"])
def test_register_missing_fields(missing: str) -> None:
    data = {k: v for k, v in GOOD.items() if k != missing}
    r = post_json(Client(), REGISTER, data)
    assert r.status_code == 422 and missing in r.json()["fields"]


@pytest.mark.parametrize(
    "password",
    ["short1!", "12345678", "password", "sana@example.com", "qwertyuiop"],
)
def test_register_weak_password_rejected(password: str) -> None:
    r = post_json(Client(), REGISTER, {**GOOD, "password": password})
    assert r.status_code == 422 and "password" in r.json()["fields"]


def test_register_password_boundaries() -> None:
    assert (
        post_json(Client(), REGISTER, {**GOOD, "password": "Zq9!Zq9!"}).status_code == 201
    )  # 8 chars
    long_pw = "Zq9!" * 32  # exactly 128
    r = post_json(Client(), REGISTER, {**GOOD, "email": "b@example.com", "password": long_pw})
    assert r.status_code == 201


def test_register_duplicate_email_409() -> None:
    post_json(Client(), REGISTER, GOOD)
    r = post_json(Client(), REGISTER, {**GOOD, "email": "SANA@example.com"})
    assert r.status_code == 409 and r.json()["code"] == "email_taken"
    assert User.objects.count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_registration_same_email_only_one_wins() -> None:
    results: list[int] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(post_json(Client(), REGISTER, GOOD).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(results) == [201] + [409] * 5
    assert User.objects.filter(email="sana@example.com").count() == 1


def test_register_ip_rate_limit(settings) -> None:  # type: ignore[no-untyped-def]
    with mock.patch.object(services, "REGISTER_IP_LIMIT", (2, 3600)):
        codes = [
            post_json(Client(), REGISTER, {**GOOD, "email": f"u{i}@example.com"}).status_code
            for i in range(3)
        ]
    assert codes == [201, 201, 429]


# ---- login ----


def test_login_success_resets_failures() -> None:
    user = UserFactory(email="a@example.com", password="Correct-horse-9", failed_login_count=3)
    r = post_json(Client(), LOGIN, {"email": "A@example.com", "password": "Correct-horse-9"})
    assert r.status_code == 200 and r.json()["access_token"]
    user.refresh_from_db()
    assert user.failed_login_count == 0
    assert RefreshToken.objects.filter(user=user).exists()


def test_login_generic_errors_identical() -> None:
    UserFactory(email="a@example.com", password="Correct-horse-9")
    wrong_pw = post_json(Client(), LOGIN, {"email": "a@example.com", "password": "nope-nope-nope"})
    unknown = post_json(
        Client(), LOGIN, {"email": "ghost@example.com", "password": "nope-nope-nope"}
    )
    assert wrong_pw.status_code == unknown.status_code == 401
    assert wrong_pw.json() == unknown.json()
    assert wrong_pw.json()["code"] == "invalid_credentials"


def test_login_oauth_only_account_cannot_use_password() -> None:
    User.objects.create_user(email="g@example.com", auth_provider="google")
    r = post_json(Client(), LOGIN, {"email": "g@example.com", "password": "anything-goes-1"})
    assert r.status_code == 401 and r.json()["code"] == "invalid_credentials"


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"email": "a@example.com"},
        {"email": "bad", "password": "x"},
        {"email": "a@example.com", "password": ""},
    ],
)
def test_login_validation(body: dict[str, str]) -> None:
    assert post_json(Client(), LOGIN, body).status_code == 422


def test_lockout_after_five_failures_and_unlock_after_15_min() -> None:
    user = UserFactory(email="a@example.com", password="Correct-horse-9")
    bad = {"email": "a@example.com", "password": "wrong-wrong-1"}
    good = {"email": "a@example.com", "password": "Correct-horse-9"}
    for _ in range(5):
        assert post_json(Client(), LOGIN, bad).status_code == 401
    user.refresh_from_db()
    assert user.locked_until is not None and user.failed_login_count == 5
    assert AuditLog.objects.filter(action="auth.account_locked").exists()
    # correct password is refused while locked, with the same generic error
    locked = post_json(Client(), LOGIN, good)
    assert locked.status_code == 401 and locked.json()["code"] == "invalid_credentials"
    with time_machine.travel(timezone.now() + timedelta(minutes=14, seconds=50), tick=False):
        assert post_json(Client(), LOGIN, good).status_code == 401
    with time_machine.travel(timezone.now() + timedelta(minutes=15, seconds=5), tick=False):
        assert post_json(Client(), LOGIN, good).status_code == 200
    user.refresh_from_db()
    assert user.failed_login_count == 0 and user.locked_until is None


def test_failures_after_unlock_start_counting_again() -> None:
    UserFactory(
        email="a@example.com",
        password="Correct-horse-9",
        failed_login_count=5,
        locked_until=timezone.now() - timedelta(minutes=1),
    )
    post_json(Client(), LOGIN, {"email": "a@example.com", "password": "wrong-wrong-1"})
    assert User.objects.get(email="a@example.com").failed_login_count == 1


def test_four_failures_do_not_lock() -> None:
    user = UserFactory(email="a@example.com", password="Correct-horse-9")
    for _ in range(4):
        post_json(Client(), LOGIN, {"email": "a@example.com", "password": "wrong-wrong-1"})
    user.refresh_from_db()
    assert user.locked_until is None
    assert (
        post_json(
            Client(), LOGIN, {"email": "a@example.com", "password": "Correct-horse-9"}
        ).status_code
        == 200
    )


def test_suspended_user_cannot_login() -> None:
    UserFactory(email="a@example.com", password="Correct-horse-9", status=UserStatus.SUSPENDED)
    r = post_json(Client(), LOGIN, {"email": "a@example.com", "password": "Correct-horse-9"})
    assert r.status_code == 403 and r.json()["code"] == "account_suspended"
    assert RefreshToken.objects.count() == 0


def test_login_ip_rate_limit() -> None:
    with mock.patch.object(services, "LOGIN_IP_LIMIT", (3, 60)):
        codes = [
            post_json(Client(), LOGIN, {"email": "x@example.com", "password": "y"}).status_code
            for _ in range(4)
        ]
    assert codes == [401, 401, 401, 429]


# ---- email verification ----


def test_email_verification_flow_and_token_reuse(run_commit) -> None:  # type: ignore[no-untyped-def]
    with run_commit():
        post_json(Client(), REGISTER, GOOD)
    token = mail.outbox[0].body.split("token=")[1].strip()
    r = post_json(Client(), "/api/v1/auth/email/verify", {"token": token})
    assert r.status_code == 200
    assert User.objects.get(email="sana@example.com").is_email_verified
    again = post_json(Client(), "/api/v1/auth/email/verify", {"token": token})
    assert again.status_code == 400 and again.json()["code"] == "invalid_token"


def test_email_verification_token_expires(run_commit) -> None:  # type: ignore[no-untyped-def]
    with run_commit():
        post_json(Client(), REGISTER, GOOD)
    token = mail.outbox[0].body.split("token=")[1].strip()
    with time_machine.travel(timezone.now() + timedelta(hours=24, seconds=5), tick=False):
        assert post_json(Client(), "/api/v1/auth/email/verify", {"token": token}).status_code == 400
    assert not User.objects.get(email="sana@example.com").is_email_verified


def test_email_verification_wrong_purpose_or_garbage() -> None:
    assert post_json(Client(), "/api/v1/auth/email/verify", {"token": "garbage"}).status_code == 400
    assert post_json(Client(), "/api/v1/auth/email/verify", {"token": ""}).status_code == 422


# ---- password reset ----

_RC: list = []


@pytest.fixture(autouse=True)
def _capture_commit(run_commit):  # type: ignore[no-untyped-def]
    _RC[:] = [run_commit]


def reset_request(email: str):  # type: ignore[no-untyped-def]
    with _RC[0]():
        return post_json(Client(), "/api/v1/auth/password/reset/request", {"email": email})


def _reset_link_token() -> str:
    return mail.outbox[-1].body.split("token=")[1].strip()


def test_reset_request_identical_for_known_and_unknown() -> None:
    UserFactory(email="a@example.com")
    known = reset_request("a@example.com")
    unknown = reset_request("zzz@example.com")
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json()
    assert [m.to for m in mail.outbox] == [["a@example.com"]]


def test_reset_flow_end_to_end_revokes_sessions_and_unlocks() -> None:
    user = UserFactory(
        email="a@example.com",
        password="Old-password-1!",
        failed_login_count=5,
        locked_until=timezone.now() + timedelta(minutes=10),
    )
    session = tokens.issue_session(user)
    reset_request("a@example.com")
    token = _reset_link_token()
    r = post_json(
        Client(),
        "/api/v1/auth/password/reset/confirm",
        {"token": token, "new_password": "Brand-new-pass-77"},
    )
    assert r.status_code == 200
    user.refresh_from_db()
    assert user.check_password("Brand-new-pass-77") and user.locked_until is None
    assert not tokens.session_is_active(str(session.family_id))
    assert (
        post_json(
            Client(), LOGIN, {"email": "a@example.com", "password": "Brand-new-pass-77"}
        ).status_code
        == 200
    )
    assert AuditLog.objects.filter(action="auth.password_reset").exists()


def test_reset_token_single_use() -> None:
    UserFactory(email="a@example.com")
    reset_request("a@example.com")
    token = _reset_link_token()
    url = "/api/v1/auth/password/reset/confirm"
    assert (
        post_json(Client(), url, {"token": token, "new_password": "Brand-new-pass-77"}).status_code
        == 200
    )
    again = post_json(Client(), url, {"token": token, "new_password": "Another-pass-88!"})
    assert again.status_code == 400 and again.json()["code"] == "invalid_token"


def test_reset_token_expiry_boundary() -> None:
    UserFactory(email="a@example.com")
    reset_request("a@example.com")
    token = _reset_link_token()
    url = "/api/v1/auth/password/reset/confirm"
    with time_machine.travel(timezone.now() + timedelta(hours=1, seconds=5), tick=False):
        assert (
            post_json(
                Client(), url, {"token": token, "new_password": "Brand-new-pass-77"}
            ).status_code
            == 400
        )
    with time_machine.travel(timezone.now() + timedelta(minutes=59), tick=False):
        assert (
            post_json(
                Client(), url, {"token": token, "new_password": "Brand-new-pass-77"}
            ).status_code
            == 200
        )


def test_reset_weak_password_does_not_burn_token() -> None:
    UserFactory(email="a@example.com")
    reset_request("a@example.com")
    token = _reset_link_token()
    url = "/api/v1/auth/password/reset/confirm"
    r = post_json(Client(), url, {"token": token, "new_password": "12345678"})
    assert r.status_code == 422 and "password" in r.json()["fields"]
    assert (
        post_json(Client(), url, {"token": token, "new_password": "Brand-new-pass-77"}).status_code
        == 200
    )


def test_reset_request_rate_limited_silently_per_email() -> None:
    UserFactory(email="a@example.com")
    for _ in range(5):
        r = reset_request("a@example.com")
        assert r.status_code == 200
    assert len(mail.outbox) == 3


def test_reset_for_suspended_user_sends_nothing() -> None:
    UserFactory(email="a@example.com", status=UserStatus.SUSPENDED)
    reset_request("a@example.com")
    assert mail.outbox == []


def test_reset_request_validation() -> None:
    assert reset_request("nope").status_code == 422
    assert post_json(Client(), "/api/v1/auth/password/reset/request", {}).status_code == 422


def test_make_customer_helper_has_profile() -> None:
    assert make_customer(name="Zed").profile.name == "Zed"

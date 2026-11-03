import threading
from datetime import timedelta
from unittest import mock

import pytest
import time_machine
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts import otp
from apps.accounts.factories import UserFactory
from apps.accounts.models import CustomerProfile, User, UserStatus
from apps.accounts.tests.helpers import post_json
from apps.notifications.adapters import FakeSMS

pytestmark = pytest.mark.django_db
REQ = "/api/v1/auth/otp/request"
VER = "/api/v1/auth/otp/verify"
PHONE = "+923001234567"


def request_code(run_commit, phone: str = "03001234567") -> str:  # type: ignore[no-untyped-def]
    with run_commit():
        r = post_json(Client(), REQ, {"phone": phone})
    assert r.status_code == 202, r.content
    return FakeSMS.outbox[-1].body.split("code is ")[1][:6]


def test_request_success_normalises_and_sends_sms(run_commit) -> None:  # type: ignore[no-untyped-def]
    with run_commit():
        r = post_json(Client(), REQ, {"phone": "0300 123 4567"})
    assert r.status_code == 202 and r.json() == {"expires_in": 300, "resend_in": 60}
    assert len(FakeSMS.outbox) == 1 and FakeSMS.outbox[0].to == PHONE
    assert "expires in 5 minutes" in FakeSMS.outbox[0].body


def test_code_is_hashed_in_cache(run_commit) -> None:  # type: ignore[no-untyped-def]
    from django.core.cache import cache

    code = request_code(run_commit)
    record = cache.get(f"otp:code:login:{PHONE}")
    assert code not in str(record) and len(record["h"]) == 64


@pytest.mark.parametrize(
    "phone", ["", "123", "+14155552671", "abcdefghijk", "03001234", None, 3001234567]
)
def test_request_phone_validation(phone: object) -> None:
    r = post_json(Client(), REQ, {"phone": phone})
    assert r.status_code == 422 and "phone" in r.json()["fields"]


def test_request_missing_phone() -> None:
    assert post_json(Client(), REQ, {}).status_code == 422


def test_verify_registers_new_user(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    r = post_json(Client(), VER, {"phone": "03001234567", "code": code, "name": "Hira"})
    assert r.status_code == 200
    body = r.json()
    assert body["is_new_user"] is True and body["access_token"]
    user = User.objects.get(phone=PHONE)
    assert user.is_phone_verified and user.auth_provider == "otp" and user.email is None
    assert CustomerProfile.objects.get(user=user).name == "Hira"


def test_verify_logs_in_existing_user(run_commit) -> None:  # type: ignore[no-untyped-def]
    existing = UserFactory(phone=PHONE, is_phone_verified=False)
    code = request_code(run_commit)
    body = post_json(Client(), VER, {"phone": PHONE, "code": code}).json()
    assert body["is_new_user"] is False
    existing.refresh_from_db()
    assert existing.is_phone_verified and User.objects.count() == 1


def test_code_single_use(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    assert post_json(Client(), VER, {"phone": PHONE, "code": code}).status_code == 200
    again = post_json(Client(), VER, {"phone": PHONE, "code": code})
    assert again.status_code == 400 and again.json()["code"] == "invalid_otp"


def test_wrong_code(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    wrong = "000000" if code != "000000" else "111111"
    r = post_json(Client(), VER, {"phone": PHONE, "code": wrong})
    assert r.status_code == 400 and r.json()["code"] == "invalid_otp"
    assert not User.objects.exists()
    assert post_json(Client(), VER, {"phone": PHONE, "code": code}).status_code == 200


def test_unrequested_phone_rejected() -> None:
    r = post_json(Client(), VER, {"phone": PHONE, "code": "123456"})
    assert r.status_code == 400


@pytest.mark.parametrize("code", ["", "12345", "1234567", "abcdef", "12 456", None])
def test_code_format_validation(code: object) -> None:
    r = post_json(Client(), VER, {"phone": PHONE, "code": code})
    assert r.status_code == 422 and "code" in r.json()["fields"]


def test_expired_at_5_01(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    start = timezone.now()
    with time_machine.travel(start + timedelta(minutes=5, seconds=1), tick=False):
        r = post_json(Client(), VER, {"phone": PHONE, "code": code})
        assert r.status_code == 400 and r.json()["code"] == "invalid_otp"


def test_code_valid_at_4_59(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    with time_machine.travel(timezone.now() + timedelta(minutes=4, seconds=59), tick=False):
        assert post_json(Client(), VER, {"phone": PHONE, "code": code}).status_code == 200


def test_attempts_exhausted_burns_code(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(5):
        assert post_json(Client(), VER, {"phone": PHONE, "code": wrong}).status_code == 400
    # code is now burned even when correct
    r = post_json(Client(), VER, {"phone": PHONE, "code": code})
    assert r.status_code == 400 and r.json()["code"] == "invalid_otp"
    assert not User.objects.exists()


def test_fourth_wrong_then_correct_still_ok(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(4):
        post_json(Client(), VER, {"phone": PHONE, "code": wrong})
    assert post_json(Client(), VER, {"phone": PHONE, "code": code}).status_code == 200


def test_resend_cooldown_60s(run_commit) -> None:  # type: ignore[no-untyped-def]
    request_code(run_commit)
    start = timezone.now()
    with time_machine.travel(start + timedelta(seconds=30), tick=False):
        r = post_json(Client(), REQ, {"phone": PHONE})
        assert r.status_code == 429 and r.json()["code"] == "otp_cooldown"
        assert int(r.json()["fields"]["retry_after"][0]) > 0
    with time_machine.travel(start + timedelta(seconds=61), tick=False), run_commit():
        assert post_json(Client(), REQ, {"phone": PHONE}).status_code == 202
    assert len(FakeSMS.outbox) == 2


def test_new_code_invalidates_previous(run_commit) -> None:  # type: ignore[no-untyped-def]
    first = request_code(run_commit)
    with time_machine.travel(timezone.now() + timedelta(seconds=61), tick=False):
        second = request_code(run_commit)
        if first != second:
            assert post_json(Client(), VER, {"phone": PHONE, "code": first}).status_code == 400
        assert post_json(Client(), VER, {"phone": PHONE, "code": second}).status_code == 200


def test_hourly_cap_5_per_phone(run_commit) -> None:  # type: ignore[no-untyped-def]
    start = timezone.now()
    for i in range(5):
        with time_machine.travel(start + timedelta(seconds=61 * i), tick=False), run_commit():
            assert post_json(Client(), REQ, {"phone": PHONE}).status_code == 202
    with time_machine.travel(start + timedelta(seconds=61 * 5), tick=False):
        r = post_json(Client(), REQ, {"phone": PHONE})
        assert r.status_code == 429 and r.json()["code"] == "otp_rate_limited"
    # window slides: first request ages out after an hour
    with time_machine.travel(start + timedelta(hours=1, seconds=1), tick=False), run_commit():
        assert post_json(Client(), REQ, {"phone": PHONE}).status_code == 202
    assert len(FakeSMS.outbox) == 6


def test_per_ip_limit() -> None:
    with mock.patch.object(otp, "MAX_REQUESTS_PER_IP_HOUR", 2):
        codes = [
            post_json(Client(), REQ, {"phone": f"030012345{i:02d}"}).status_code for i in range(3)
        ]
    assert codes == [202, 202, 429]


def test_suspended_user_cannot_login_by_otp(run_commit) -> None:  # type: ignore[no-untyped-def]
    UserFactory(phone=PHONE, status=UserStatus.SUSPENDED)
    code = request_code(run_commit)
    r = post_json(Client(), VER, {"phone": PHONE, "code": code})
    assert r.status_code == 403 and r.json()["code"] == "account_suspended"


def test_phone_change_otp_cannot_be_used_for_login(run_commit) -> None:  # type: ignore[no-untyped-def]
    with run_commit():
        otp.request_otp(PHONE, otp.PHONE_CHANGE, None)
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    r = post_json(Client(), VER, {"phone": PHONE, "code": code})
    assert r.status_code == 400 and r.json()["code"] == "invalid_otp"


@pytest.mark.django_db(transaction=True)
def test_concurrent_verify_same_phone_creates_one_account(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.accounts import services

    results: list[bool] = []
    barrier = threading.Barrier(6)
    rf = __import__("django.test", fromlist=["RequestFactory"]).RequestFactory()

    def worker() -> None:
        try:
            barrier.wait()
            _, created = services.login_with_phone_otp(
                phone=PHONE, code="123456", name="X", request=rf.post("/")
            )
            results.append(created)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    # patch once, outside the threads: mock.patch is not thread-safe
    with mock.patch.object(services.otp, "verify_otp"):
        [t.start() for t in threads]
        [t.join() for t in threads]
    assert User.objects.filter(phone=PHONE).count() == 1
    assert results.count(True) == 1 and len(results) == 6


@pytest.mark.django_db(transaction=True)
def test_concurrent_code_use_only_one_winner(run_commit) -> None:  # type: ignore[no-untyped-def]
    code = request_code(run_commit)
    codes: list[int] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(post_json(Client(), VER, {"phone": PHONE, "code": code}).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(200) == 1 and codes.count(400) == 4

import json
from datetime import timedelta
from unittest import mock

import pytest
import time_machine
from django.test import Client
from redis.exceptions import ConnectionError as RedisConnectionError

from apps.accounts.factories import UserFactory
from apps.accounts.models import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings import holds, services
from apps.bookings.factories import AppointmentFactory
from apps.bookings.tests.api_helpers import (
    HOLD,
    capacity_business,
    customer_session,
    hold_body,
    place_hold,
    post,
    staff_business,
)
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses.factories import ServiceFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("frozen")]


def test_hold_success_shape_and_redis_state() -> None:
    b, svc = capacity_business()
    user, h = customer_session()
    r = place_hold(h, b, [svc])
    assert r.status_code == 201
    body = r.json()
    assert (
        body["start"] == "2026-11-03T12:00:00+05:00" and body["end"] == "2026-11-03T13:00:00+05:00"
    )
    assert body["business_id"] == str(b.public_id) and body["staff_id"] is None
    assert body["expires_at"] == (NOW + timedelta(seconds=300)).isoformat().replace("+00:00", "Z")
    stored = holds.get_hold(body["token"])
    assert stored and stored.user_id == user.pk and stored.service_ids == (svc.pk,)


def test_hold_key_format_and_ttl() -> None:
    from django_redis import get_redis_connection

    b, svc = capacity_business()
    _, h = customer_session()
    place_hold(h, b, [svc])
    key = f"hold:{b.pk}:any:{int(local(TUESDAY, 12).timestamp())}"
    conn = get_redis_connection("default")
    assert conn.exists(key) == 1 and 0 < conn.pttl(key) <= 300_000


def test_held_slot_blocks_other_customers() -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    assert place_hold(h1, b, [svc]).status_code == 201
    r = place_hold(h2, b, [svc])
    assert r.status_code == 409 and r.json()["code"] == "slot_unavailable"
    assert place_hold(h2, b, [svc], hour=14).status_code == 201


def test_overlapping_start_is_blocked_too() -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    place_hold(h1, b, [svc], hour=12)
    r = post(HOLD, hold_body(b, [svc], 12, 30), h2)
    assert r.status_code == 409


def test_capacity_two_allows_two_holds_not_three() -> None:
    b, svc = capacity_business(parallel_capacity=2)
    sessions = [customer_session()[1] for _ in range(3)]
    codes = [place_hold(h, b, [svc]).status_code for h in sessions]
    assert codes == [201, 201, 409]


def test_hold_expires_and_slot_is_free_again() -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    place_hold(h1, b, [svc])
    with time_machine.travel(NOW + timedelta(seconds=301), tick=False):
        assert place_hold(h2, b, [svc]).status_code in (
            201,
            409,
        )  # key may still be in Redis for ms
    from django_redis import get_redis_connection

    get_redis_connection("default").flushdb()  # TTL is real time; emulate Redis expiring the key
    with time_machine.travel(NOW + timedelta(seconds=301), tick=False):
        assert place_hold(h2, b, [svc]).status_code == 201


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.pop("business_id"),
        lambda d: d.pop("service_ids"),
        lambda d: d.pop("start"),
        lambda d: d.update(service_ids=[]),
        lambda d: d.update(service_ids=list(range(1, 12))),
        lambda d: d.update(service_ids=["x"]),
        lambda d: d.update(start="2026-11-03T12:00:00"),  # naive datetime
        lambda d: d.update(start="not-a-date"),
        lambda d: d.update(business_id="nope"),
        lambda d: d.update(staff_id="x"),
    ],
)
def test_hold_validation_422(mutate) -> None:  # type: ignore[no-untyped-def]
    b, svc = capacity_business()
    _, h = customer_session()
    data = hold_body(b, [svc])
    mutate(data)
    assert post(HOLD, data, h).status_code == 422


def test_hold_unavailable_times_409() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    for hour, minute in [
        (12, 15),
        (9, 0),
        (19, 30),
        (22, 0),
    ]:  # misaligned, before open, overrun, closed
        r = post(HOLD, hold_body(b, [svc], hour, minute), h)
        assert r.status_code == 409 and r.json()["code"] == "slot_unavailable", (hour, minute)


def test_hold_in_past_and_inside_lead_time() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    past = hold_body(b, [svc])
    past["start"] = "2026-11-01T12:00:00+05:00"
    assert post(HOLD, past, h).status_code == 422  # date_in_past from the availability engine
    b2, svc2 = capacity_business(lead_time_minutes=24 * 60)
    # earliest bookable is Tuesday 09:00; 10:00 Monday is not
    soon = hold_body(b2, [svc2])
    soon["start"] = "2026-11-02T10:00:00+05:00"
    assert post(HOLD, soon, h).status_code == 409


def test_hold_service_validation() -> None:
    b, svc = capacity_business()
    other_business_service = ServiceFactory()
    inactive = ServiceFactory(business=b, is_active=False)
    _, h = customer_session()
    for bad in ([other_business_service], [inactive], [svc, other_business_service]):
        r = place_hold(h, b, bad)
        assert r.status_code == 422 and r.json()["code"] == "invalid_services"


def test_hold_staff_who_cannot_perform_422() -> None:
    b, svc, staff = staff_business()
    stranger = ServiceFactory(business=b)
    from apps.businesses.factories import StaffFactory

    lonely = StaffFactory(business=b)
    _, h = customer_session()
    r = place_hold(h, b, [svc], staff_id=lonely.pk)
    assert r.status_code == 422 and r.json()["code"] == "staff_cannot_perform"
    assert stranger.pk and staff


def test_hold_with_specific_staff_and_any_staff_assignment() -> None:
    b, svc, (alice, bob) = staff_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    r = place_hold(h1, b, [svc], staff_id=bob.pk)
    assert r.status_code == 201 and r.json()["staff_id"] == bob.pk
    # "any staff" now falls to the only free member
    r = place_hold(h2, b, [svc])
    assert r.status_code == 201 and r.json()["staff_id"] == alice.pk
    _, h3 = customer_session()
    assert place_hold(h3, b, [svc]).status_code == 409


def test_any_staff_picks_least_busy_then_lowest_id() -> None:
    b, svc, (alice, bob) = staff_business()
    AppointmentFactory(
        business=b,
        staff=alice,
        scheduled_start=local(TUESDAY, 10),
        scheduled_end=local(TUESDAY, 11),
    )
    _, h = customer_session()
    r = place_hold(h, b, [svc], hour=15)
    assert r.json()["staff_id"] == bob.pk  # alice already has one booking that day
    _, h2 = customer_session()
    r = place_hold(h2, b, [svc], hour=17)
    assert r.json()["staff_id"] == bob.pk  # holds are not bookings, so bob is still least busy


def test_hold_requires_auth_and_customer_role() -> None:
    b, svc = capacity_business()
    assert post(HOLD, hold_body(b, [svc]), {}).status_code == 401
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    _, session = login_client(owner)
    r = post(HOLD, hold_body(b, [svc]), bearer(session))
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"
    assert post(HOLD, hold_body(b, [svc]), {"HTTP_AUTHORIZATION": "Bearer junk"}).status_code == 401


def test_hold_unknown_or_unlisted_business_404() -> None:
    b, svc = capacity_business()
    b.is_listed = False
    b.save()
    _, h = customer_session()
    r = place_hold(h, b, [svc])
    assert r.status_code == 404


def test_hold_rate_limited() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    with mock.patch.object(services, "HOLD_RATE", (2, 60)):
        codes = [place_hold(h, b, [svc], hour=10 + i).status_code for i in range(3)]
    assert codes == [201, 201, 429]


# ---------------- release ----------------


def release(token: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    return Client().delete(f"/api/v1/appointments/hold/{token}", **headers)


def test_release_own_hold_reopens_slot() -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    token = place_hold(h1, b, [svc]).json()["token"]
    assert release(token, h1).status_code == 204
    assert holds.get_hold(token) is None
    assert place_hold(h2, b, [svc]).status_code == 201


def test_release_other_users_hold_404_and_hold_survives() -> None:
    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    token = place_hold(h1, b, [svc]).json()["token"]
    assert release(token, h2).status_code == 404
    assert holds.get_hold(token) is not None


def test_release_unknown_and_repeat_is_idempotent_204() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    assert release("does-not-exist", h).status_code == 204
    token = place_hold(h, b, [svc]).json()["token"]
    assert release(token, h).status_code == 204
    assert release(token, h).status_code == 204


def test_release_requires_auth() -> None:
    assert Client().delete("/api/v1/appointments/hold/x").status_code == 401


def test_compare_and_delete_never_removes_someone_elses_hold() -> None:
    """A's hold expires, B takes the same key, A's late release must not delete B's hold."""
    from django_redis import get_redis_connection

    b, svc = capacity_business()
    _, h1 = customer_session()
    _, h2 = customer_session()
    a_token = place_hold(h1, b, [svc]).json()["token"]
    key = holds.hold_key(b.pk, None, local(TUESDAY, 12))
    get_redis_connection("default").delete(key)  # A's hold expired in Redis
    b_token = place_hold(h2, b, [svc]).json()["token"]
    # A's token index still points at the (now B-owned) key
    from django_redis import get_redis_connection as conn

    conn("default").set(holds.token_key(a_token), key)
    assert holds.release_hold(a_token) is False
    assert holds.get_hold(b_token) is not None


def test_hold_release_helper_lua_returns_true_for_owner() -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = place_hold(h, b, [svc]).json()["token"]
    assert holds.release_hold(token) is True
    assert holds.release_hold(token) is False


# ---------------- Redis outage ----------------


def test_redis_down_issues_soft_hold(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    b, svc = capacity_business()
    _, h = customer_session()

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    r = place_hold(h, b, [svc])
    assert r.status_code == 201 and r.json()["token"].startswith("soft.")
    assert "soft hold" in caplog.text
    assert json.loads(r.content)["staff_id"] is None
    # releasing a soft hold is a harmless no-op
    assert release(r.json()["token"], h).status_code == 204


def test_release_survives_redis_outage(monkeypatch: pytest.MonkeyPatch) -> None:
    b, svc = capacity_business()
    _, h = customer_session()
    token = place_hold(h, b, [svc]).json()["token"]

    def boom() -> None:
        raise RedisConnectionError("down")

    monkeypatch.setattr(holds, "_conn", boom)
    assert release(token, h).status_code == 204

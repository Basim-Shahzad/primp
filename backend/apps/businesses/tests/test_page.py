import hashlib
import hmac
import json
from datetime import UTC, datetime, time, timedelta
from unittest import mock

import pytest
import requests
import time_machine
from django.core.cache import cache
from django.test import Client, override_settings

from apps.accounts.factories import make_customer
from apps.businesses import pagecache
from apps.businesses.factories import (
    BusinessFactory,
    BusinessPhotoFactory,
    ClosureFactory,
    OpeningHoursFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.businesses.models import Business
from apps.businesses.tasks import revalidate_frontend, sign
from apps.catalog.factories import CategoryFactory
from apps.geo.factories import AreaFactory, CityFactory
from apps.reviews.factories import ReviewFactory
from apps.reviews.models import ReviewStatus
from apps.reviews.services import recompute_business_rating

pytestmark = pytest.mark.django_db

# Monday 2026-11-02 09:00 PKT
NOW = datetime(2026, 11, 2, 4, 0, tzinfo=UTC)


def get(ref, **params):  # type: ignore[no-untyped-def]
    return Client().get(f"/api/v1/businesses/{ref}", params)


# ---------------- lookup / visibility ----------------


def test_get_by_slug_and_uuid_and_city_disambiguation() -> None:
    a, b = AreaFactory(), AreaFactory()
    first = BusinessFactory(slug="glow", area=a, name="First")
    second = BusinessFactory(slug="glow", area=b, name="Second")
    assert get("glow").json()["name"] == "First"
    assert get("glow", city=b.city.slug).json()["name"] == "Second"
    assert get(second.public_id).json()["name"] == "Second"
    assert get(first.public_id).json()["id"] == str(first.public_id)


def test_unlisted_unknown_hidden_city_404() -> None:
    unlisted = BusinessFactory(is_listed=False)
    hidden = BusinessFactory(area=AreaFactory(city=CityFactory(visibility="hidden")))
    for ref in (unlisted.slug, hidden.slug, "nope", unlisted.public_id):
        r = get(ref)
        assert r.status_code == 404 and r.json()["code"] == "not_found"


def test_page_is_public_no_auth_needed() -> None:
    b = BusinessFactory()
    assert Client().get(f"/api/v1/businesses/{b.slug}").status_code == 200


# ---------------- content ----------------


def test_basic_fields_location_and_booking_rules() -> None:
    top = CategoryFactory(name="Hair")
    b = BusinessFactory(
        name="Glow",
        category=top,
        description="Desc",
        address="Plot 1",
        phone="+923001234567",
        whatsapp="+923001234567",
        gender_type="women",
        confirm_mode="manual",
        cancellation_notice_hours=12,
        lead_time_minutes=45,
        max_advance_days=30,
        slot_interval_minutes=15,
    )
    body = get(b.slug).json()
    assert body["name"] == "Glow" and body["category"] == {"id": top.pk, "name": "Hair"}
    assert body["location"] == {
        "lat": pytest.approx(24.8607, abs=1e-4),
        "lng": pytest.approx(67.0011, abs=1e-4),
    }
    assert body["area"]["name"] == b.area.name and body["city"]["slug"] == b.city.slug
    assert body["is_verified"] is True and body["timezone"] == "Asia/Karachi"
    assert body["booking"] == {
        "confirm_mode": "manual",
        "cancellation_notice_hours": 12,
        "lead_time_minutes": 45,
        "max_advance_days": 30,
        "slot_interval_minutes": 15,
        "staff_level_booking": False,
    }
    assert body["gender_type"] == "women" and body["phone"] == "+923001234567"


def test_unverified_badge() -> None:
    b = BusinessFactory(verification_status="pending")
    assert get(b.slug).json()["is_verified"] is False


def test_photos_cover_first() -> None:
    b = BusinessFactory()
    BusinessPhotoFactory(business=b, image="b/2.jpg", sort_order=2)
    BusinessPhotoFactory(business=b, image="b/1.jpg", sort_order=1)
    BusinessPhotoFactory(business=b, image="b/cover.jpg", sort_order=9, is_cover=True)
    photos = get(b.slug).json()["photos"]
    assert [p["url"].rsplit("/", 1)[-1] for p in photos] == ["cover.jpg", "1.jpg", "2.jpg"]
    assert photos[0]["is_cover"] is True


def test_services_grouped_by_category_inactive_hidden() -> None:
    b = BusinessFactory()
    hair, nails = (
        CategoryFactory(name="Hair", sort_order=1),
        CategoryFactory(name="Nails", sort_order=2),
    )
    ServiceFactory(business=b, name="Cut", category=hair, sort_order=1, price=800)
    ServiceFactory(business=b, name="Colour", category=hair, sort_order=2)
    ServiceFactory(business=b, name="Mani", category=nails)
    ServiceFactory(business=b, name="Misc")
    ServiceFactory(business=b, name="Retired", category=hair, is_active=False)
    groups = get(b.slug).json()["services"]
    assert [g["category"]["name"] if g["category"] else None for g in groups] == [
        "Hair",
        "Nails",
        None,
    ]
    assert [s["name"] for s in groups[0]["services"]] == ["Cut", "Colour"]
    assert groups[0]["services"][0] == {
        "id": groups[0]["services"][0]["id"],
        "name": "Cut",
        "description": "",
        "price": 800,
        "is_from_price": False,
        "duration_minutes": 60,
    }
    assert "Retired" not in json.dumps(groups)


def test_staff_only_when_staff_level_booking_and_inactive_hidden() -> None:
    off = BusinessFactory(staff_level_booking=False)
    StaffFactory(business=off)
    assert get(off.slug).json()["staff"] == []
    on = BusinessFactory(staff_level_booking=True)
    svc, retired = ServiceFactory(business=on), ServiceFactory(business=on, is_active=False)
    alice = StaffFactory(business=on, name="Alice", bio="Senior stylist")
    StaffServiceFactory(staff=alice, service=svc)
    StaffServiceFactory(staff=alice, service=retired)
    StaffFactory(business=on, name="Gone", is_active=False)
    staff = get(on.slug).json()["staff"]
    assert [s["name"] for s in staff] == ["Alice"]
    assert staff[0]["service_ids"] == [svc.pk] and staff[0]["bio"] == "Senior stylist"


def test_weekly_hours_with_split_shift_and_closed_days() -> None:
    b = BusinessFactory()
    OpeningHoursFactory(business=b, weekday=0, opens_at=time(10), closes_at=time(13))
    OpeningHoursFactory(business=b, weekday=0, opens_at=time(15), closes_at=time(20))
    staff = StaffFactory(business=b)
    OpeningHoursFactory(business=b, weekday=1, staff=staff)  # staff override is not business hours
    hours = get(b.slug).json()["hours"]
    assert [d["weekday"] for d in hours] == list(range(7))
    assert hours[0]["shifts"] == [
        {"opens_at": "10:00", "closes_at": "13:00"},
        {"opens_at": "15:00", "closes_at": "20:00"},
    ]
    assert all(d["shifts"] == [] for d in hours[1:])


@time_machine.travel(NOW, tick=False)
def test_only_upcoming_business_wide_closures() -> None:
    b = BusinessFactory()
    staff = StaffFactory(business=b)
    ClosureFactory(
        business=b, starts_at=NOW - timedelta(days=5), ends_at=NOW - timedelta(days=4), reason="old"
    )
    ClosureFactory(
        business=b,
        starts_at=NOW + timedelta(days=2),
        ends_at=NOW + timedelta(days=3),
        reason="Eid",
        kind="holiday",
    )
    ClosureFactory(
        business=b,
        staff=staff,
        starts_at=NOW + timedelta(days=2),
        ends_at=NOW + timedelta(days=3),
        reason="leave",
    )
    ClosureFactory(
        business=b,
        starts_at=NOW + timedelta(days=200),
        ends_at=NOW + timedelta(days=201),
        reason="far",
    )
    closures = get(b.slug).json()["closures"]
    assert [c["reason"] for c in closures] == ["Eid"] and closures[0]["kind"] == "holiday"


def test_closure_expires_from_response_without_cache_refresh() -> None:
    b = BusinessFactory()
    ClosureFactory(
        business=b,
        starts_at=NOW + timedelta(hours=1),
        ends_at=NOW + timedelta(hours=2),
        reason="short",
    )
    with time_machine.travel(NOW, tick=False):
        assert len(get(b.slug).json()["closures"]) == 1
    with time_machine.travel(NOW + timedelta(hours=3), tick=False):
        assert get(b.slug).json()["closures"] == []


# ---------------- is_open_now ----------------


def open_business() -> Business:
    b = BusinessFactory()
    open_all_week(b)
    return b


@pytest.mark.parametrize(
    ("utc", "expected"),
    [
        (datetime(2026, 11, 2, 4, 59, tzinfo=UTC), False),  # 09:59 PKT
        (datetime(2026, 11, 2, 5, 0, tzinfo=UTC), True),  # 10:00 PKT opens inclusive
        (datetime(2026, 11, 2, 14, 59, tzinfo=UTC), True),  # 19:59 PKT
        (datetime(2026, 11, 2, 15, 0, tzinfo=UTC), False),  # 20:00 PKT closes exclusive
    ],
)
def test_is_open_now_boundaries(utc: datetime, expected: bool) -> None:
    b = open_business()
    with time_machine.travel(utc, tick=False):
        assert get(b.slug).json()["is_open_now"] is expected


def test_is_open_now_uses_business_timezone_across_midnight() -> None:
    b = BusinessFactory()
    OpeningHoursFactory(
        business=b, weekday=0, opens_at=time(10), closes_at=time(23, 59)
    )  # Monday only
    # 20:30 UTC Monday == 01:30 PKT Tuesday: UTC date is Monday but the business is closed
    with time_machine.travel(datetime(2026, 11, 2, 20, 30, tzinfo=UTC), tick=False):
        assert get(b.slug).json()["is_open_now"] is False
    # 06:00 UTC Monday == 11:00 PKT Monday -> open
    with time_machine.travel(datetime(2026, 11, 2, 6, 0, tzinfo=UTC), tick=False):
        assert get(b.slug).json()["is_open_now"] is True


def test_is_open_now_false_during_closure_and_split_gap() -> None:
    b = BusinessFactory()
    OpeningHoursFactory(business=b, weekday=0, opens_at=time(10), closes_at=time(13))
    OpeningHoursFactory(business=b, weekday=0, opens_at=time(15), closes_at=time(20))
    with time_machine.travel(datetime(2026, 11, 2, 9, 0, tzinfo=UTC), tick=False):  # 14:00 PKT
        assert get(b.slug).json()["is_open_now"] is False
    with time_machine.travel(datetime(2026, 11, 2, 6, 0, tzinfo=UTC), tick=False):  # 11:00 PKT
        assert get(b.slug).json()["is_open_now"] is True
        ClosureFactory(
            business=b,
            starts_at=datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
            ends_at=datetime(2026, 11, 2, 7, 0, tzinfo=UTC),
        )
        assert get(b.slug).json()["is_open_now"] is False


# ---------------- rating ----------------


def test_rating_distribution_sums_to_count() -> None:
    b = BusinessFactory()
    for rating in (5, 5, 4, 1):
        ReviewFactory(appointment__business=b, rating=rating)
    hidden = ReviewFactory(appointment__business=b, rating=2, status=ReviewStatus.HIDDEN)
    removed = ReviewFactory(appointment__business=b, rating=3, status=ReviewStatus.REMOVED)
    rating = get(b.slug).json()["rating"]
    assert rating["distribution"] == {"1": 1, "2": 0, "3": 0, "4": 1, "5": 2}
    assert sum(rating["distribution"].values()) == rating["count"] == 4
    assert rating["avg"] == 3.75 and hidden.pk and removed.pk


def test_rating_empty() -> None:
    rating = get(BusinessFactory().slug).json()["rating"]
    assert rating == {
        "avg": 0.0,
        "count": 0,
        "distribution": {"1": 0, "2": 0, "3": 0, "4": 0, "5": 0},
    }


def test_recompute_rating_updates_business_columns() -> None:
    b = BusinessFactory()
    for rating in (5, 4):
        ReviewFactory(appointment__business=b, rating=rating)
    recompute_business_rating(b.pk)
    b.refresh_from_db()
    assert float(b.rating_avg) == 4.5 and b.rating_count == 2


# ---------------- cache ----------------


def test_page_cached_and_second_request_cheaper(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b = BusinessFactory()
    ServiceFactory(business=b)
    get(b.slug)
    assert cache.get(pagecache.page_key(b.pk)) is not None
    with django_assert_max_num_queries(2):  # business lookup (+ at most one more)
        assert get(b.slug).status_code == 200


def test_cold_page_query_count_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b = BusinessFactory(staff_level_booking=True)
    for _ in range(5):
        StaffServiceFactory(staff=StaffFactory(business=b), service=ServiceFactory(business=b))
    open_all_week(b)
    cache.clear()
    with django_assert_max_num_queries(12):
        assert get(b.slug).status_code == 200


@pytest.mark.parametrize(
    "change",
    [
        lambda b: setattr(b, "name", "Renamed") or b.save(),
        lambda b: ServiceFactory(business=b, name="New Service"),
        lambda b: b.services.first().save(),
        lambda b: StaffFactory(business=b),
        lambda b: OpeningHoursFactory(business=b, weekday=3),
        lambda b: ClosureFactory(
            business=b, starts_at=NOW + timedelta(days=1), ends_at=NOW + timedelta(days=2)
        ),
        lambda b: BusinessPhotoFactory(business=b),
        lambda b: ReviewFactory(appointment__business=b),
    ],
)
def test_cache_invalidated_on_any_change(change) -> None:  # type: ignore[no-untyped-def]
    b = BusinessFactory()
    ServiceFactory(business=b)
    with time_machine.travel(NOW, tick=False):
        get(b.slug)
        assert cache.get(pagecache.page_key(b.pk)) is not None
        change(b)
        assert cache.get(pagecache.page_key(b.pk)) is None


def test_change_visible_immediately() -> None:
    b = BusinessFactory()
    svc = ServiceFactory(business=b, price=1000)
    assert get(b.slug).json()["services"][0]["services"][0]["price"] == 1000
    svc.price = 1500
    svc.save()
    assert get(b.slug).json()["services"][0]["services"][0]["price"] == 1500
    svc.is_active = False
    svc.save()
    assert get(b.slug).json()["services"] == []
    ReviewFactory(appointment__business=b, rating=4)
    assert get(b.slug).json()["rating"]["count"] == 1


def test_review_status_change_invalidates() -> None:
    b = BusinessFactory()
    review = ReviewFactory(appointment__business=b)
    assert get(b.slug).json()["rating"]["count"] == 1
    review.status = ReviewStatus.HIDDEN
    review.save()
    assert get(b.slug).json()["rating"]["count"] == 0


# ---------------- revalidation webhook ----------------

HOOK = {
    "FRONTEND_REVALIDATE_URL": "https://frontend.test/api/revalidate",
    "FRONTEND_REVALIDATE_SECRET": "s3cret",
}


@override_settings(**HOOK)
def test_webhook_signature_and_payload() -> None:
    b = BusinessFactory(slug="glow")
    response = mock.Mock(status_code=200)
    with mock.patch("apps.businesses.tasks.requests.post", return_value=response) as post:
        assert revalidate_frontend(b.pk) is True
    kwargs = post.call_args.kwargs
    body, headers = kwargs["data"], kwargs["headers"]
    assert json.loads(body) == {
        "type": "business.updated",
        "business_id": str(b.public_id),
        "slug": "glow",
        "city": b.city.slug,
    }
    expected = hmac.new(
        b"s3cret", headers["X-Primp-Timestamp"].encode() + b"." + body, hashlib.sha256
    ).hexdigest()
    assert headers["X-Primp-Signature"] == f"sha256={expected}"
    assert sign("s3cret", headers["X-Primp-Timestamp"], body) == headers["X-Primp-Signature"]
    assert post.call_args.args[0] == HOOK["FRONTEND_REVALIDATE_URL"] and kwargs["timeout"] == 5


def run_with_retries(business_id: int, attempts: int = 4) -> int:
    """Mimic Celery's autoretry: re-run the task body while it raises a retryable error."""
    calls = 0
    for _ in range(attempts):
        calls += 1
        try:
            revalidate_frontend._orig_run(business_id)  # type: ignore[attr-defined]
            break
        except revalidate_frontend.autoretry_for:
            continue
    return calls


@override_settings(**HOOK)
def test_webhook_failures_raise_retryable_errors_then_succeed() -> None:
    b = BusinessFactory()
    ok = mock.Mock()
    with mock.patch(
        "apps.businesses.tasks.requests.post",
        side_effect=[requests.ConnectionError("down"), requests.Timeout("slow"), ok],
    ) as post:
        assert run_with_retries(b.pk) == 3
    assert post.call_count == 3


@override_settings(**HOOK)
def test_webhook_http_error_status_is_retryable() -> None:
    b = BusinessFactory()
    bad = mock.Mock()
    bad.raise_for_status.side_effect = requests.HTTPError("500")
    with mock.patch("apps.businesses.tasks.requests.post", side_effect=[bad, mock.Mock()]) as post:
        assert run_with_retries(b.pk) == 2
    assert post.call_count == 2


@override_settings(**HOOK)
def test_webhook_error_propagates_to_celery_for_retry() -> None:
    b = BusinessFactory()
    with (
        mock.patch(
            "apps.businesses.tasks.requests.post", side_effect=requests.ConnectionError("x")
        ),
        pytest.raises(requests.ConnectionError),
    ):
        revalidate_frontend._orig_run(b.pk)  # type: ignore[attr-defined]


def test_webhook_noop_when_not_configured() -> None:
    b = BusinessFactory()
    with mock.patch("apps.businesses.tasks.requests.post") as post:
        assert revalidate_frontend(b.pk) is False
    post.assert_not_called()


@override_settings(**HOOK)
def test_webhook_missing_business_is_noop() -> None:
    with mock.patch("apps.businesses.tasks.requests.post") as post:
        assert revalidate_frontend(999999) is False
    post.assert_not_called()


@override_settings(**HOOK)
def test_change_enqueues_webhook_on_commit_not_before(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    b = BusinessFactory()
    with (
        mock.patch("apps.businesses.tasks.requests.post", return_value=mock.Mock()) as post,
        django_capture_on_commit_callbacks(execute=False) as callbacks,
    ):
        b.name = "Changed"
        b.save()
        post.assert_not_called()
    assert callbacks
    with mock.patch("apps.businesses.tasks.requests.post", return_value=mock.Mock()) as post:
        for cb in callbacks:
            cb()
    assert post.called


def test_customer_factory_import_used() -> None:
    assert make_customer().pk


def test_webhook_task_retry_policy() -> None:
    assert requests.RequestException in revalidate_frontend.autoretry_for
    assert revalidate_frontend.retry_kwargs["max_retries"] == 3
    assert revalidate_frontend.retry_backoff
    assert revalidate_frontend.queue == "bulk"

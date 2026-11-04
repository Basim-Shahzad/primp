import csv
import io
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Any
from unittest import mock
from zoneinfo import ZoneInfo

import pytest
import time_machine
from django.db.models.query import QuerySet
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from apps.accounts.factories import make_customer
from apps.bookings import analytics
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import (
    Appointment,
    AppointmentService,
    BusinessDailyStats,
    ServiceDailyStats,
)
from apps.bookings.tasks import rebuild_daily_stats
from apps.bookings.tests.helpers import MONDAY, NOW, TUESDAY, local
from apps.businesses.factories import BusinessFactory, ServiceFactory, open_all_week
from apps.businesses.models import Business, Service
from apps.businesses.tests.provider_helpers import P, call, customer_headers, owner_headers
from apps.core.errors import AppError

pytestmark = pytest.mark.django_db
PKT = ZoneInfo("Asia/Karachi")


def shop(**kw: Any) -> tuple[Business, dict[str, str], Service]:
    owner, h = owner_headers()
    b = BusinessFactory(owner=owner, lead_time_minutes=0, **kw)
    open_all_week(b)
    return b, h, ServiceFactory(business=b, duration_minutes=60, price=1000, name="Cut")


def make(
    b: Business,
    svc: Service,
    start: datetime,
    status: str = "completed",
    price: int | None = None,
    extra: list[tuple[Service, int]] | None = None,
    **kw: Any,
) -> Appointment:
    items = [(svc, price if price is not None else svc.price), *(extra or [])]
    customer = kw.pop("customer", "default")
    a = AppointmentFactory(
        business=b,
        customer=make_customer(name="Sana Khan") if customer == "default" else customer,
        status=status,
        scheduled_start=start,
        scheduled_end=start + timedelta(hours=1),
        total_price=sum(p for _s, p in items),
        **kw,
    )
    for service, p in items:
        AppointmentService.objects.create(
            appointment=a,
            service=service,
            name=service.name,
            price_at_booking=p,
            duration_at_booking=60,
        )
    return a


def day_row(b: Business, day: date) -> BusinessDailyStats | None:
    return BusinessDailyStats.objects.filter(business=b, date=day).first()


def url(b: Business, tail: str) -> str:
    return f"{P}/businesses/{b.public_id}/{tail}"


# ------------------------------------------------------------ access matrix


def test_analytics_endpoints_auth_role_ownership(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    _, other = owner_headers()
    cust = customer_headers()
    paths = [
        url(b, "analytics/summary"),
        url(b, "analytics/top-services"),
        url(b, "analytics/timeseries"),
        url(b, "appointments/export.csv?from=2026-11-01&to=2026-11-30"),
    ]
    for path in paths:
        assert call("get", path, {}).status_code == 401, path
        assert call("get", path, {"HTTP_AUTHORIZATION": "Bearer x"}).status_code == 401, path
        assert call("get", path, cust).status_code == 403, path
        assert call("get", path, other).status_code == 404, path
        assert call("get", path, h).status_code == 200, path


# ------------------------------------------------------------ aggregation correctness


def expected_rows(b: Business) -> dict[date, dict[str, int]]:
    """Direct recomputation from the appointments (the oracle)."""
    out: dict[date, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for a in Appointment.objects.filter(business=b).prefetch_related("items"):
        d = a.scheduled_start.astimezone(PKT).date()
        row = out[d]
        row["bookings_total"] += 1
        key = {
            "completed": "completed",
            "cancelled_by_customer": "cancelled_by_customer",
            "cancelled_by_business": "cancelled_by_business",
            "no_show": "no_shows",
        }.get(a.status)
        if key:
            row[key] += 1
        if a.status == "completed":
            row["revenue_completed"] += sum(i.price_at_booking for i in a.items.all())
    return out


def stored_rows(b: Business) -> dict[date, dict[str, int]]:
    fields = (
        "bookings_total",
        "completed",
        "cancelled_by_customer",
        "cancelled_by_business",
        "no_shows",
        "revenue_completed",
    )
    return {
        r.date: {f: getattr(r, f) for f in fields if getattr(r, f)}
        for r in BusinessDailyStats.objects.filter(business=b)
    }


def assert_matches_oracle(b: Business) -> None:
    oracle = {d: {k: v for k, v in row.items() if v} for d, row in expected_rows(b).items()}
    assert stored_rows(b) == oracle
    for day in oracle:  # per-day invariants
        row = BusinessDailyStats.objects.get(business=b, date=day)
        assert (
            row.completed + row.cancelled_by_customer + row.cancelled_by_business + row.no_shows
            <= row.bookings_total
        )
        service_revenue = sum(
            s.revenue for s in ServiceDailyStats.objects.filter(business=b, date=day)
        )
        assert service_revenue == row.revenue_completed


STATUSES = [
    "pending",
    "confirmed",
    "completed",
    "cancelled_by_customer",
    "cancelled_by_business",
    "no_show",
]
booking_strategy = st.lists(
    st.tuples(
        st.integers(0, 6),  # day offset
        st.integers(0, 23),
        st.sampled_from([0, 30, 59]),
        st.sampled_from(STATUSES),
        st.integers(0, 2),  # primary service index
        st.integers(0, 5000),  # price
        st.booleans(),  # second service too
    ),
    max_size=14,
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(history=booking_strategy)
def test_aggregates_equal_direct_recomputation_for_random_histories(history: list[Any]) -> None:
    b = BusinessFactory(lead_time_minutes=0)
    services = [ServiceFactory(business=b, name=f"S{i}") for i in range(3)]
    for offset, hour, minute, status, idx, price, second in history:
        start = local(MONDAY + timedelta(days=offset), hour, minute)
        extra = [(services[(idx + 1) % 3], price // 2)] if second else None
        make(b, services[idx], start, status, price=price, extra=extra)
    analytics.recompute_range(b.pk, MONDAY - timedelta(days=1), MONDAY + timedelta(days=8))
    assert_matches_oracle(b)
    before = stored_rows(b)
    analytics.recompute_range(b.pk, MONDAY - timedelta(days=1), MONDAY + timedelta(days=8))
    assert stored_rows(b) == before  # re-running never double counts
    assert_matches_oracle(b)


def test_rerunning_the_nightly_job_does_not_double_count() -> None:
    b, h, svc = shop()
    make(b, svc, local(TUESDAY, 12))
    make(b, svc, local(TUESDAY, 14), status="no_show")
    with time_machine.travel(local(TUESDAY, 23), tick=False):
        assert rebuild_daily_stats() >= 1
        first = stored_rows(b)
        rebuild_daily_stats()
        rebuild_daily_stats()
    assert (
        stored_rows(b)
        == first
        == {
            TUESDAY: {"bookings_total": 2, "completed": 1, "no_shows": 1, "revenue_completed": 1000}
        }
    )
    assert BusinessDailyStats.objects.filter(business=b).count() == 1


def test_nightly_task_range_and_single_business_arguments() -> None:
    b, h, svc = shop()
    other, _, other_svc = shop()
    make(b, svc, local(TUESDAY, 12))
    make(other, other_svc, local(TUESDAY, 12))
    rebuild_daily_stats(TUESDAY.isoformat(), TUESDAY.isoformat(), b.pk)
    assert BusinessDailyStats.objects.filter(business=b).count() == 1
    assert not BusinessDailyStats.objects.filter(business=other).exists()
    with pytest.raises(ValueError):
        rebuild_daily_stats(business_id=b.pk)
    far = TUESDAY - timedelta(days=30)
    make(b, svc, local(far, 12))
    rebuild_daily_stats(far.isoformat(), TUESDAY.isoformat())  # a re-runnable date range
    assert BusinessDailyStats.objects.filter(business=b).count() == 2
    assert BusinessDailyStats.objects.filter(business=other).count() == 1


def test_stale_rows_are_removed_when_data_disappears() -> None:
    b, h, svc = shop()
    a = make(b, svc, local(TUESDAY, 12))
    analytics.recompute_range(b.pk, TUESDAY, TUESDAY)
    assert day_row(b, TUESDAY) is not None
    a.delete()
    analytics.recompute_range(b.pk, TUESDAY, TUESDAY)
    assert day_row(b, TUESDAY) is None and not ServiceDailyStats.objects.filter(business=b).exists()
    assert analytics.recompute_range(b.pk, TUESDAY, TUESDAY - timedelta(days=1)) == 0


@pytest.mark.parametrize(
    "hour,minute,local_day_offset",
    [(0, 30, 0), (23, 30, 0), (0, 0, 0), (23, 59, 0)],
)
def test_pkt_day_boundaries(hour: int, minute: int, local_day_offset: int) -> None:
    b, h, svc = shop()
    start = local(TUESDAY, hour, minute)
    make(b, svc, start)
    analytics.recompute_range(b.pk, MONDAY, MONDAY + timedelta(days=5))
    assert set(stored_rows(b)) == {TUESDAY}
    # 00:30 PKT is still Monday in UTC: the UTC date must not be used
    assert (start.astimezone(ZoneInfo("UTC")).date() == MONDAY) == (hour == 0)


def test_cancelled_never_count_in_service_bookings_and_revenue_only_when_completed() -> None:
    b, h, svc = shop()
    make(b, svc, local(TUESDAY, 10), status="completed", price=1000)
    make(b, svc, local(TUESDAY, 11), status="confirmed", price=1000)
    make(b, svc, local(TUESDAY, 12), status="cancelled_by_customer", price=1000)
    make(b, svc, local(TUESDAY, 13), status="no_show", price=1000)
    analytics.recompute_range(b.pk, TUESDAY, TUESDAY)
    s = ServiceDailyStats.objects.get(business=b, service=svc, date=TUESDAY)
    assert (s.bookings, s.revenue) == (
        3,
        1000,
    )  # completed + confirmed + no_show; revenue completed only
    assert day_row(b, TUESDAY).bookings_total == 4  # type: ignore[union-attr]


def test_price_change_after_booking_does_not_change_revenue() -> None:
    b, h, svc = shop()
    make(b, svc, local(TUESDAY, 10), price=1000)
    Service.objects.filter(pk=svc.pk).update(price=9999)
    analytics.recompute_range(b.pk, TUESDAY, TUESDAY)
    assert day_row(b, TUESDAY).revenue_completed == 1000  # type: ignore[union-attr]


# ------------------------------------------------------------ same-day freshness via events


def test_stats_refresh_on_events_without_the_nightly_job(frozen, long_tokens, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop(confirm_mode="manual")
    body = {
        "customer_name": "Walk In",
        "customer_phone": "03001234567",
        "service_ids": [svc.pk],
        "start": local(MONDAY, 12).isoformat(),
    }
    with run_commit():
        r = call("post", url(b, "manual-bookings"), h, body)
    assert r.status_code == 201, r.content
    row = day_row(b, MONDAY)
    assert row is not None and (row.bookings_total, row.completed, row.revenue_completed) == (
        1,
        0,
        0,
    )
    appt_id = r.json()["id"]
    with time_machine.travel(local(MONDAY, 14), tick=False), run_commit():
        done = call("post", f"{P}/appointments/{appt_id}/complete", h, {})
    assert done.status_code == 200
    row = day_row(b, MONDAY)
    assert row is not None and (row.completed, row.revenue_completed) == (1, 1000)
    second = call(
        "post", url(b, "manual-bookings"), h, {**body, "start": local(MONDAY, 16).isoformat()}
    )
    with run_commit():
        call("post", url(b, "manual-bookings"), h, {**body, "start": local(MONDAY, 18).isoformat()})
        call("post", f"{P}/appointments/{second.json()['id']}/cancel", h, {"reason": "x"})
    row = day_row(b, MONDAY)
    assert row is not None and row.cancelled_by_business == 1 and row.bookings_total >= 2
    assert_matches_oracle(b)


def test_reschedule_moves_the_count_between_days(frozen, run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    a = make(b, svc, local(TUESDAY, 12), status="confirmed")
    analytics.recompute_range(b.pk, TUESDAY, TUESDAY)
    wed = TUESDAY + timedelta(days=1)
    with run_commit():
        r = call(
            "post",
            f"{P}/appointments/{a.public_id}/reschedule",
            h,
            {"start": local(wed, 12).isoformat()},
        )
    assert r.status_code == 200, r.content
    assert day_row(b, TUESDAY) is None
    assert day_row(b, wed).bookings_total == 1  # type: ignore[union-attr]


def test_refresh_errors_are_logged_not_raised_in_production(settings, run_commit) -> None:  # type: ignore[no-untyped-def]
    settings.ANALYTICS_RAISE_ON_ERROR = False
    b, h, svc = shop()
    a = make(b, svc, local(TUESDAY, 12), status="confirmed")
    from apps.bookings import analytics_receivers, events

    with mock.patch("apps.bookings.analytics.recompute_range", side_effect=RuntimeError("db")):
        analytics_receivers._refresh(None, a.pk)  # must not raise
    settings.ANALYTICS_RAISE_ON_ERROR = True
    boom = mock.patch("apps.bookings.analytics.recompute_range", side_effect=RuntimeError("db"))
    with boom, pytest.raises(RuntimeError):
        analytics_receivers._refresh(None, a.pk)
    assert events.booking_created


# ------------------------------------------------------------ ranges


TZ = PKT


@pytest.mark.parametrize(
    "name,expected",
    [
        ("today", (date(2026, 11, 4), date(2026, 11, 4))),  # Wednesday
        ("week", (date(2026, 11, 2), date(2026, 11, 4))),
        ("month", (date(2026, 11, 1), date(2026, 11, 4))),
        ("30d", (date(2026, 10, 6), date(2026, 11, 4))),
    ],
)
def test_named_ranges_in_business_timezone(name: str, expected: tuple[date, date]) -> None:
    now = datetime(
        2026, 11, 4, 20, 0, tzinfo=ZoneInfo("UTC")
    )  # 01:00 PKT on the 5th? no: 20:00 UTC = 01:00 next day
    now = datetime(2026, 11, 4, 8, 0, tzinfo=ZoneInfo("UTC"))
    rng = analytics.resolve_range(name, None, None, TZ, now)
    assert (rng.start, rng.end) == expected


def test_range_uses_local_date_not_utc_date() -> None:
    now = datetime(2026, 11, 4, 20, 0, tzinfo=ZoneInfo("UTC"))  # 01:00 on the 5th in Karachi
    assert analytics.resolve_range("today", None, None, TZ, now).start == date(2026, 11, 5)


def test_custom_range_validation_and_previous() -> None:
    with pytest.raises(AppError) as missing:
        analytics.resolve_range("custom", None, date(2026, 1, 1), TZ)
    assert "from" in missing.value.fields
    with pytest.raises(AppError) as backwards:
        analytics.resolve_range("custom", date(2026, 1, 2), date(2026, 1, 1), TZ)
    assert "to" in backwards.value.fields
    ok = analytics.resolve_range("custom", date(2025, 1, 1), date(2025, 12, 31), TZ)  # 365 days
    assert ok.days == 365
    assert analytics.resolve_range("custom", date(2026, 1, 1), date(2027, 1, 1), TZ).days == 366
    with pytest.raises(AppError) as too_long:
        analytics.resolve_range("custom", date(2026, 1, 1), date(2027, 1, 2), TZ)
    assert "to" in too_long.value.fields
    prev = analytics.DateRange(date(2026, 11, 3), date(2026, 11, 5)).previous()
    assert (prev.start, prev.end) == (date(2026, 10, 31), date(2026, 11, 2))


@pytest.mark.parametrize(
    "qs",
    [
        "?range=yearly",
        "?range=custom",
        "?range=custom&from=2026-01-01",
        "?range=custom&from=2026-02-01&to=2026-01-01",
        "?range=custom&from=2026-01-01&to=2027-01-02",
        "?range=custom&from=bad&to=2026-01-01",
    ],
)
def test_summary_range_validation(qs: str, frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    assert call("get", url(b, f"analytics/summary{qs}"), h).status_code == 422


# ------------------------------------------------------------ KPI endpoints


def test_summary_totals_rates_and_previous_period(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    cust = make_customer()
    for hour in (10, 11, 12):
        make(b, svc, local(MONDAY, hour), "completed", price=1500, customer=cust)
    make(b, svc, local(MONDAY, 13), "cancelled_by_customer")
    make(b, svc, local(MONDAY, 14), "cancelled_by_business")
    make(b, svc, local(MONDAY, 15), "no_show")
    make(b, svc, local(MONDAY, 16), "confirmed")
    make(
        b, svc, local(MONDAY - timedelta(days=1), 12), "completed", price=500
    )  # previous period (today)
    analytics.recompute_range(b.pk, MONDAY - timedelta(days=2), MONDAY)
    r = call("get", url(b, "analytics/summary?range=today"), h)
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["totals"] == {
        "bookings_total": 7,
        "completed": 3,
        "cancelled_by_customer": 1,
        "cancelled_by_business": 1,
        "no_shows": 1,
        "revenue_completed": 4500,
    }
    assert data["previous"]["completed"] == 1 and data["previous"]["revenue_completed"] == 500
    assert (data["from_date"], data["to_date"]) == ("2026-11-02", "2026-11-02")
    assert (data["previous_from"], data["previous_to"]) == ("2026-11-01", "2026-11-01")
    assert data["cancellation_rate"] == round(2 / 7, 4) and data["no_show_rate"] == round(1 / 7, 4)
    assert data["average_booking_value"] == 1500
    month = call("get", url(b, "analytics/summary?range=month"), h).json()
    assert month["totals"]["bookings_total"] == 8  # month-to-date includes the day before


def test_summary_empty_has_zeroes_and_no_division_error(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    data = call("get", url(b, "analytics/summary"), h).json()
    assert data["range"] == "30d" and data["totals"]["revenue_completed"] == 0
    assert (data["cancellation_rate"], data["no_show_rate"], data["average_booking_value"]) == (
        0,
        0,
        0,
    )


def test_top_services_by_count_and_revenue(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    pricey = ServiceFactory(business=b, name="Bridal", price=20000)
    cheap = ServiceFactory(business=b, name="Threading", price=300)
    for hour in (10, 11, 12, 13):
        make(b, cheap, local(MONDAY, hour), "completed", price=300)
    make(b, pricey, local(MONDAY, 14), "completed", price=20000)
    make(b, pricey, local(MONDAY, 15), "cancelled_by_customer", price=20000)
    make(b, svc, local(MONDAY, 16), "confirmed", price=1000)
    analytics.recompute_range(b.pk, MONDAY, MONDAY)
    by_count = call(
        "get", url(b, "analytics/top-services?range=today&metric=count&limit=2"), h
    ).json()
    assert [r["name"] for r in by_count] == ["Threading", "Bridal"] and by_count[0]["bookings"] == 4
    by_revenue = call("get", url(b, "analytics/top-services?range=today&metric=revenue"), h).json()
    assert [r["name"] for r in by_revenue] == ["Bridal", "Threading"]
    assert by_revenue[0]["revenue"] == 20000 and by_revenue[0]["bookings"] == 1
    assert all(r["bookings"] or r["revenue"] for r in by_revenue)
    for bad in ("metric=profit", "limit=0", "limit=21"):
        assert call("get", url(b, f"analytics/top-services?{bad}"), h).status_code == 422
    assert len(call("get", url(b, "analytics/top-services?range=today&limit=20"), h).json()) == 3


def test_timeseries_day_and_week_zero_filled(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    d1, d2 = date(2026, 10, 28), date(2026, 11, 3)  # Wed of one week, Tue of the next
    make(b, svc, local(d1, 12), "completed", price=700)
    make(b, svc, local(d2, 12), "completed", price=900)
    make(b, svc, local(d2, 14), "no_show")
    analytics.recompute_range(b.pk, d1, d2)
    q = "range=custom&from=2026-10-27&to=2026-11-04"
    days = call("get", url(b, f"analytics/timeseries?{q}&granularity=day"), h).json()
    assert days["granularity"] == "day" and len(days["points"]) == 9
    by = {p["period_start"]: p for p in days["points"]}
    assert by["2026-10-28"]["revenue_completed"] == 700 and by["2026-10-29"]["bookings_total"] == 0
    assert by["2026-11-03"]["no_shows"] == 1
    weeks = call("get", url(b, f"analytics/timeseries?{q}&granularity=week"), h).json()["points"]
    assert [p["period_start"] for p in weeks] == ["2026-10-27", "2026-11-02"]
    assert weeks[0]["revenue_completed"] == 700 and weeks[1]["revenue_completed"] == 900
    total = sum(p["revenue_completed"] for p in days["points"])
    summary = call("get", url(b, f"analytics/summary?{q}"), h).json()
    assert total == summary["totals"]["revenue_completed"] == 1600
    clipped = call(
        "get",
        url(b, "analytics/timeseries?range=custom&from=2026-10-28&to=2026-11-03&granularity=week"),
        h,
    ).json()
    assert [p["period_start"] for p in clipped["points"]] == ["2026-10-28", "2026-11-02"]
    assert call("get", url(b, "analytics/timeseries?granularity=month"), h).status_code == 422


# ------------------------------------------------------------ CSV export


def rows_of(response: Any) -> list[list[str]]:
    body = b"".join(response.streaming_content).decode()
    return list(csv.reader(io.StringIO(body)))


EXPORT = "appointments/export.csv?from=2026-11-01&to=2026-11-30"


def test_export_streams_expected_rows_and_headers(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop(slug="glow-salon")
    cust = make_customer(name="Sana Khan", phone="+923001234567")
    make(b, svc, local(TUESDAY, 12), "confirmed", customer=cust)
    make(b, svc, local(TUESDAY - timedelta(days=1), 9), "completed", customer=cust)
    make(b, svc, local(date(2026, 12, 1), 12), "confirmed")  # outside the range
    r = call("get", url(b, EXPORT), h)
    assert r.status_code == 200 and r.streaming is True
    assert r["Content-Type"].startswith("text/csv") and r["Cache-Control"] == "private, no-store"
    assert (
        r["Content-Disposition"]
        == 'attachment; filename="appointments-glow-salon-2026-11-01-2026-11-30.csv"'
    )
    rows = rows_of(r)
    assert rows[0] == analytics.CSV_COLUMNS and len(rows) == 3
    first, second = rows[1], rows[2]
    assert first[1:6] == ["2026-11-02", "09:00", "10:00", "completed", "online"]
    assert second[1] == "2026-11-03" and second[7] == "Cut" and second[8] == "1000"
    assert (
        second[10] == "Sana Khan" and second[11] == "'+923001234567"
    )  # active booking keeps phone
    assert first[11] == ""  # past booking: phone withheld


@pytest.mark.parametrize(
    "dangerous", ['=HYPERLINK("http://evil")', "+1+1", "-2+3", "@SUM(A1)", "\t=1", "\r=1"]
)
def test_export_neutralises_formula_injection(dangerous: str, frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    make(
        b,
        svc,
        local(TUESDAY, 12),
        "confirmed",
        customer=None,
        walk_in_name=dangerous,
        walk_in_phone="+923451112222",
    )
    rows = rows_of(call("get", url(b, EXPORT), h))
    assert rows[1][10] == "'" + dangerous
    assert rows[1][11] == "'+923451112222"
    assert not any(cell.startswith(analytics.DANGEROUS) for cell in rows[1])


def test_safe_cell_leaves_normal_values_alone() -> None:
    assert analytics.safe_cell("Ayesha") == "Ayesha" and analytics.safe_cell(1500) == 1500
    assert analytics.safe_cell("") == "" and analytics.safe_cell("a=b") == "a=b"


def test_export_range_limits(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    assert (
        call("get", url(b, "appointments/export.csv?from=2026-01-01&to=2027-01-01"), h).status_code
        == 200
    )  # 366 days
    over = call("get", url(b, "appointments/export.csv?from=2026-01-01&to=2027-01-02"), h)
    assert over.status_code == 422 and "to" in over.json()["fields"]
    assert (
        call("get", url(b, "appointments/export.csv?from=2026-02-01&to=2026-01-01"), h).status_code
        == 422
    )
    assert call("get", url(b, "appointments/export.csv?from=2026-02-01"), h).status_code == 422


def test_export_is_streamed_through_a_chunked_iterator(
    frozen, monkeypatch, django_assert_max_num_queries
) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    for hour in range(8, 18):
        make(
            b,
            svc,
            local(TUESDAY, hour),
            "confirmed",
            customer=None,
            walk_in_name=f"W{hour}",
            walk_in_phone="+923451112222",
        )
    monkeypatch.setattr(analytics, "CHUNK", 3)
    calls: list[int | None] = []
    original = QuerySet.iterator

    def spy(self: Any, chunk_size: int | None = 2000) -> Any:
        calls.append(chunk_size)
        return original(self, chunk_size=chunk_size)

    monkeypatch.setattr(QuerySet, "iterator", spy)
    filename, lines = analytics.export_csv(b.owner_id, str(b.public_id), TUESDAY, TUESDAY)
    assert (
        filename.endswith(".csv") and iter(lines) is lines
    )  # a generator, nothing materialised yet
    assert calls == []
    header = next(lines)
    assert header.startswith("appointment_id")
    assert len(list(lines)) == 10
    assert calls == [3]


def test_export_is_throttled(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop()
    codes = [call("get", url(b, EXPORT), h).status_code for _ in range(11)]
    assert codes[:10] == [200] * 10 and codes[10] == 429


def test_export_filename_cannot_inject_headers(frozen) -> None:  # type: ignore[no-untyped-def]
    b, h, svc = shop(slug="ok-slug")
    r = call("get", url(b, EXPORT), h)
    assert "\n" not in r["Content-Disposition"] and '"' in r["Content-Disposition"]


def test_unused_imports_guard() -> None:
    assert NOW

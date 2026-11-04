"""Basic provider analytics: daily aggregates, range queries and the CSV export.

Stats are bucketed by the business-local date of the appointment start. `recompute_range` is the
single writer: it recomputes a date range from the appointments (delete + insert under a
per-business advisory lock), so the nightly job, a manual re-run and the same-day event hooks can
overlap without ever double counting.
"""

import csv
import zlib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Literal
from zoneinfo import ZoneInfo

from django.db import connection, transaction
from django.db.models import Count, Q, Sum
from django.db.models.functions import TruncDate
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import selectors as accounts
from apps.businesses import selectors as biz
from apps.core.errors import validation_error

from .models import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentService,
    AppointmentStatus,
    BusinessDailyStats,
    ServiceDailyStats,
)

S = AppointmentStatus
MAX_RANGE_DAYS = 366
CANCELLED = (S.CANCELLED_BY_CUSTOMER, S.CANCELLED_BY_BUSINESS)
RangeName = Literal["today", "week", "month", "30d", "custom"]

# ---------------------------------------------------------------- aggregation


def _bounds(start: date, end: date, tz: ZoneInfo) -> tuple[datetime, datetime]:
    return (
        datetime.combine(start, time.min, tzinfo=tz),
        datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz),
    )


def _lock(business_id: int) -> None:
    key = zlib.crc32(f"daily-stats:{business_id}".encode())
    with connection.cursor() as cursor:
        cursor.execute("SELECT pg_advisory_xact_lock(%s)", [key])


def recompute_range(business_id: int, start: date, end: date) -> int:
    """Rebuild the stats of one business for the inclusive local date range. Idempotent."""
    if end < start:
        return 0
    tz = ZoneInfo(biz.timezone_of(business_id) or "Asia/Karachi")
    lo, hi = _bounds(start, end, tz)
    with transaction.atomic():
        _lock(business_id)
        appointments = Appointment.objects.filter(
            business_id=business_id, scheduled_start__gte=lo, scheduled_start__lt=hi
        )
        by_day = (
            appointments.annotate(day=TruncDate("scheduled_start", tzinfo=tz))
            .values("day")
            .annotate(
                total=Count("id"),
                completed=Count("id", filter=Q(status=S.COMPLETED)),
                cancelled_by_customer=Count("id", filter=Q(status=S.CANCELLED_BY_CUSTOMER)),
                cancelled_by_business=Count("id", filter=Q(status=S.CANCELLED_BY_BUSINESS)),
                no_shows=Count("id", filter=Q(status=S.NO_SHOW)),
            )
        )
        items = AppointmentService.objects.filter(appointment__in=appointments)
        by_service = (
            items.annotate(day=TruncDate("appointment__scheduled_start", tzinfo=tz))
            .values("day", "service_id")
            .annotate(
                bookings=Count("id", filter=~Q(appointment__status__in=CANCELLED)),
                revenue=Sum("price_at_booking", filter=Q(appointment__status=S.COMPLETED)),
            )
        )
        revenue_by_day: dict[date, int] = {}
        for row in by_service:
            revenue_by_day[row["day"]] = revenue_by_day.get(row["day"], 0) + int(
                row["revenue"] or 0
            )
        BusinessDailyStats.objects.filter(
            business_id=business_id, date__gte=start, date__lte=end
        ).delete()
        ServiceDailyStats.objects.filter(
            business_id=business_id, date__gte=start, date__lte=end
        ).delete()
        BusinessDailyStats.objects.bulk_create(
            [
                BusinessDailyStats(
                    business_id=business_id,
                    date=r["day"],
                    bookings_total=r["total"],
                    completed=r["completed"],
                    cancelled_by_customer=r["cancelled_by_customer"],
                    cancelled_by_business=r["cancelled_by_business"],
                    no_shows=r["no_shows"],
                    revenue_completed=revenue_by_day.get(r["day"], 0),
                )
                for r in by_day
            ]
        )
        ServiceDailyStats.objects.bulk_create(
            [
                ServiceDailyStats(
                    business_id=business_id,
                    service_id=r["service_id"],
                    date=r["day"],
                    bookings=r["bookings"],
                    revenue=int(r["revenue"] or 0),
                )
                for r in by_service
                if r["bookings"] or r["revenue"]
            ]
        )
        return len(by_day)


def recompute_all(start: date | None = None, end: date | None = None) -> int:
    """Rebuild every business. Default: yesterday and today in each business's timezone."""
    done = 0
    for business_id, tz_name in biz.all_business_timezones():
        today = timezone.now().astimezone(ZoneInfo(tz_name)).date()
        recompute_range(business_id, start or today - timedelta(days=1), end or today)
        done += 1
    return done


# ---------------------------------------------------------------- ranges


@dataclass(frozen=True)
class DateRange:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days + 1

    def previous(self) -> "DateRange":
        return DateRange(self.start - timedelta(days=self.days), self.start - timedelta(days=1))


def resolve_range(
    name: str,
    from_date: date | None,
    to_date: date | None,
    tz: ZoneInfo,
    now: datetime | None = None,
) -> DateRange:
    today = (now or timezone.now()).astimezone(tz).date()
    if name == "today":
        return DateRange(today, today)
    if name == "week":
        return DateRange(today - timedelta(days=today.weekday()), today)
    if name == "month":
        return DateRange(today.replace(day=1), today)
    if name == "30d":
        return DateRange(today - timedelta(days=29), today)
    if name == "custom":
        if from_date is None or to_date is None:
            raise validation_error({"from": [str(_("`from` and `to` are required for custom."))]})
        if to_date < from_date:
            raise validation_error({"to": [str(_("The end date must not be before the start."))]})
        if (to_date - from_date).days >= MAX_RANGE_DAYS:
            raise validation_error({"to": [str(_("The range may span at most 366 days."))]})
        return DateRange(from_date, to_date)
    raise validation_error({"range": [str(_("Unknown range."))]})


# ---------------------------------------------------------------- reads


@dataclass(frozen=True)
class Totals:
    bookings_total: int = 0
    completed: int = 0
    cancelled_by_customer: int = 0
    cancelled_by_business: int = 0
    no_shows: int = 0
    revenue_completed: int = 0


def _stats(business_id: int, rng: DateRange) -> Any:
    return BusinessDailyStats.objects.filter(
        business_id=business_id, date__gte=rng.start, date__lte=rng.end
    )


def totals(business_id: int, rng: DateRange) -> Totals:
    agg = _stats(business_id, rng).aggregate(
        bookings_total=Sum("bookings_total"),
        completed=Sum("completed"),
        cancelled_by_customer=Sum("cancelled_by_customer"),
        cancelled_by_business=Sum("cancelled_by_business"),
        no_shows=Sum("no_shows"),
        revenue_completed=Sum("revenue_completed"),
    )
    return Totals(**{k: int(v or 0) for k, v in agg.items()})


def top_services(business_id: int, rng: DateRange, metric: str, limit: int) -> list[dict[str, Any]]:
    order = (
        ("-total_revenue", "-total_bookings")
        if metric == "revenue"
        else (
            "-total_bookings",
            "-total_revenue",
        )
    )
    rows = (
        ServiceDailyStats.objects.filter(
            business_id=business_id, date__gte=rng.start, date__lte=rng.end
        )
        .values("service_id", "service__name")
        .annotate(total_bookings=Sum("bookings"), total_revenue=Sum("revenue"))
        .order_by(*order, "service_id")[:limit]
    )
    return [
        {
            "service_id": r["service_id"],
            "name": r["service__name"],
            "bookings": int(r["total_bookings"] or 0),
            "revenue": int(r["total_revenue"] or 0),
        }
        for r in rows
        if (r["total_revenue"] if metric == "revenue" else r["total_bookings"])
    ]


def timeseries(business_id: int, rng: DateRange, granularity: str) -> list[tuple[date, Totals]]:
    """Zero-filled points; weekly buckets start on Monday (clipped to the range)."""
    by_day = {s.date: s for s in _stats(business_id, rng)}
    buckets: dict[date, dict[str, int]] = {}
    day = rng.start
    while day <= rng.end:
        key = day if granularity == "day" else max(rng.start, day - timedelta(days=day.weekday()))
        bucket = buckets.setdefault(key, dict.fromkeys(Totals.__dataclass_fields__, 0))
        row = by_day.get(day)
        if row:
            for field in bucket:
                bucket[field] += int(getattr(row, field))
        day += timedelta(days=1)
    return [(k, Totals(**v)) for k, v in sorted(buckets.items())]


# ---------------------------------------------------------------- CSV export

CSV_COLUMNS = [
    "appointment_id",
    "date",
    "start",
    "end",
    "status",
    "source",
    "staff",
    "services",
    "total_price",
    "payment_status",
    "customer_name",
    "customer_phone",
    "cancellation_reason",
    "created_at",
]
DANGEROUS = ("=", "+", "-", "@", "\t", "\r")
CHUNK = 500


def safe_cell(value: object) -> object:
    """Neutralise spreadsheet formulas: text starting with = + - @ gets a leading quote."""
    if isinstance(value, str) and value.startswith(DANGEROUS):
        return "'" + value
    return value


class _Echo:
    def write(self, value: str) -> str:
        return value


def _chunks(rows: Iterator[Appointment]) -> Iterator[list[Appointment]]:
    chunk: list[Appointment] = []
    for row in rows:
        chunk.append(row)
        if len(chunk) >= CHUNK:
            yield chunk
            chunk = []
    if chunk:
        yield chunk


def export_csv(
    owner_id: int, business_ref: str, from_date: date, to_date: date, now: datetime | None = None
) -> tuple[str, Iterator[str]]:
    """(filename, line iterator). Ownership and range are validated before streaming starts."""
    if to_date < from_date:
        raise validation_error({"to": [str(_("The end date must not be before the start."))]})
    if (to_date - from_date).days >= MAX_RANGE_DAYS:
        raise validation_error({"to": [str(_("The range may span at most 366 days."))]})
    business = biz.owned_business(owner_id, business_ref)
    tz = ZoneInfo(business.timezone)
    lo, hi = _bounds(from_date, to_date, tz)
    now = now or timezone.now()
    queryset = (
        Appointment.objects.filter(
            business_id=business.pk, scheduled_start__gte=lo, scheduled_start__lt=hi
        )
        .select_related("business", "staff", "customer")
        .prefetch_related("items")
        .order_by("scheduled_start", "id")
    )
    filename = f"appointments-{business.slug}-{from_date}-{to_date}.csv"

    def lines() -> Iterator[str]:
        writer = csv.writer(_Echo())
        yield writer.writerow(CSV_COLUMNS)
        for chunk in _chunks(queryset.iterator(chunk_size=CHUNK)):
            contacts = accounts.contacts_for(
                [a.customer_id for a in chunk if a.customer_id is not None]
            )
            for a in chunk:
                contact = contacts.get(a.customer_id) if a.customer_id is not None else None
                name = (contact.name if contact and contact.name else a.walk_in_name) or ""
                phone = (contact.phone if contact else None) or a.walk_in_phone or ""
                if a.status not in ACTIVE_STATUSES or a.scheduled_end <= now:
                    phone = ""
                local = a.scheduled_start.astimezone(tz)
                yield writer.writerow(
                    [
                        safe_cell(v)
                        for v in (
                            str(a.public_id),
                            local.strftime("%Y-%m-%d"),
                            local.strftime("%H:%M"),
                            a.scheduled_end.astimezone(tz).strftime("%H:%M"),
                            a.status,
                            a.source,
                            a.staff.name if a.staff else "",
                            "; ".join(i.name for i in a.items.all()),
                            a.total_price,
                            a.payment_status,
                            name,
                            phone,
                            a.cancellation_reason,
                            a.created_at.isoformat(),
                        )
                    ]
                )

    return filename, lines()

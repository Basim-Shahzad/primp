"""Weekly opening hours: full-week replace with validation (business-wide or per staff)."""

from dataclasses import dataclass
from datetime import time
from itertools import pairwise

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.errors import validation_error

from .models import OpeningHours

MAX_SHIFTS_PER_DAY = 2


@dataclass(frozen=True)
class DayShifts:
    weekday: int
    shifts: list[tuple[time, time]]


def validate_week(week: list[DayShifts]) -> None:
    errors: dict[str, list[str]] = {}
    seen: set[int] = set()
    for day in week:
        key = f"hours.{day.weekday}"
        if not 0 <= day.weekday <= 6:
            errors.setdefault(key, []).append(str(_("Weekday must be 0 (Monday) to 6 (Sunday).")))
            continue
        if day.weekday in seen:
            errors.setdefault(key, []).append(str(_("Each weekday may appear only once.")))
        seen.add(day.weekday)
        if len(day.shifts) > MAX_SHIFTS_PER_DAY:
            errors.setdefault(key, []).append(
                str(_("At most %(n)d shifts per day.") % {"n": MAX_SHIFTS_PER_DAY})
            )
        ordered = sorted(day.shifts)
        for opens, closes in ordered:
            if closes <= opens:
                errors.setdefault(key, []).append(str(_("Closing time must be after opening.")))
        for (_o1, c1), (o2, _c2) in pairwise(ordered):
            if o2 < c1:
                errors.setdefault(key, []).append(str(_("Shifts must not overlap.")))
    if errors:
        raise validation_error(errors)


def replace_week(business_id: int, staff_id: int | None, week: list[DayShifts]) -> None:
    """Replace every shift of the business (staff None) or one staff member's override."""
    validate_week(week)
    with transaction.atomic():
        OpeningHours.objects.filter(
            business_id=business_id,
            staff_id=staff_id,  # type: ignore[misc]
        ).delete()
        OpeningHours.objects.bulk_create(
            [
                OpeningHours(
                    business_id=business_id,
                    staff_id=staff_id,
                    weekday=day.weekday,
                    opens_at=opens,
                    closes_at=closes,
                )
                for day in week
                for opens, closes in sorted(day.shifts)
            ]
        )
    from .invalidation import touch

    touch(business_id)


def week_of(business_id: int, staff_id: int | None) -> list[DayShifts]:
    rows: dict[int, list[tuple[time, time]]] = {d: [] for d in range(7)}
    for h in OpeningHours.objects.filter(
        business_id=business_id,
        staff_id=staff_id,  # type: ignore[misc]
    ):
        rows[h.weekday].append((h.opens_at, h.closes_at))
    return [DayShifts(d, sorted(rows[d])) for d in range(7)]

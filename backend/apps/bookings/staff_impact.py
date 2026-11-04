"""Staff deactivation and service removal with an explicit impact workflow (Plan 6.2, Step 3).

Every future pending/confirmed appointment affected by the change must be resolved (reassigned
to another capable staff member, or cancelled by the business) in the same transaction.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import selectors as accounts
from apps.businesses import selectors as biz
from apps.businesses import staff as staff_mod
from apps.businesses.invalidation import touch
from apps.core.errors import AppError, conflict, validation_error

from . import availability, events
from .models import ACTIVE_STATUSES, Appointment, AppointmentService
from .services import Actor, _record, cancel_by_business

Action = Literal["deactivate", "remove_services"]
CANCEL_NOTE = "Staff member no longer available"


@dataclass(frozen=True)
class Resolution:
    appointment_id: str  # public id
    action: Literal["reassign", "cancel"]
    new_staff_id: int | None = None


@dataclass(frozen=True)
class ImpactItem:
    appointment_id: str
    status: str
    start: datetime
    end: datetime
    customer_name: str
    services: list[str]


@dataclass(frozen=True)
class ApplyResult:
    reassigned: int
    cancelled: int


def _affected_qs(
    staff_id: int, service_ids: list[int] | None, now: datetime
) -> QuerySet[Appointment]:
    qs = Appointment.objects.filter(
        staff_id=staff_id, status__in=ACTIVE_STATUSES, scheduled_start__gt=now
    )
    if service_ids is not None:
        with_service = AppointmentService.objects.filter(service_id__in=service_ids)
        qs = qs.filter(pk__in=with_service.values("appointment_id"))  # no DISTINCT: rows get locked
    return qs


def impact(
    owner_id: int,
    staff_id: int,
    action: Action,
    service_ids: list[int],
    now: datetime | None = None,
) -> list[ImpactItem]:
    staff = biz.owned_staff(owner_id, staff_id)
    now = now or timezone.now()
    if action == "remove_services":
        if not service_ids:
            raise validation_error({"service_ids": [str(_("Choose the services to remove."))]})
        qs = _affected_qs(staff.pk, service_ids, now)
    else:
        qs = _affected_qs(staff.pk, None, now)
    rows = qs.prefetch_related("items").order_by("scheduled_start", "id")
    names = accounts.full_names([a.customer_id for a in rows if a.customer_id is not None])
    return [
        ImpactItem(
            appointment_id=str(a.public_id),
            status=a.status,
            start=a.scheduled_start,
            end=a.scheduled_end,
            customer_name=names.get(a.customer_id, "") if a.customer_id else a.walk_in_name,
            services=[i.name for i in a.items.all()],
        )
        for a in rows
    ]


def _check_resolutions(affected: list[Appointment], resolutions: list[Resolution]) -> None:
    wanted = {str(a.public_id) for a in affected}
    given = [r.appointment_id for r in resolutions]
    unknown = sorted(set(given) - wanted)
    duplicated = sorted({g for g in given if given.count(g) > 1})
    missing = sorted(wanted - set(given))
    if unknown or duplicated:
        raise validation_error(
            {
                "resolutions": [
                    f"not an affected appointment or repeated: {x}" for x in unknown + duplicated
                ]
            }
        )
    if missing:
        raise AppError(
            "unresolved_appointments",
            _("Every affected appointment needs a resolution."),
            422,
            {"resolutions": missing},
        )
    for r in resolutions:
        if r.action == "reassign" and r.new_staff_id is None:
            raise validation_error(
                {"resolutions": [f"{r.appointment_id}: new_staff_id is required"]}
            )


def _reassign(
    appointment: Appointment,
    config: biz.BookingConfig,
    new_staff_id: int,
    excluded_staff_id: int,
    actor: Actor,
) -> None:
    if new_staff_id == excluded_staff_id:
        raise validation_error({"resolutions": [str(_("Choose a different staff member."))]})
    candidate = biz.staff_in_business(config.id, new_staff_id)
    if candidate is None or not candidate.is_active:
        raise validation_error({"resolutions": [f"{appointment.public_id}: unknown staff member"]})
    service_ids = list(appointment.items.values_list("service_id", flat=True))
    if new_staff_id not in biz.capable_staff_ids(config.id, service_ids):
        raise AppError(
            "staff_cannot_perform",
            _("That staff member cannot perform all the booked services."),
            422,
            {"resolutions": [f"{appointment.public_id}: staff lacks a booked service"]},
        )
    if not biz.lock_staff(new_staff_id):
        raise validation_error({"resolutions": [f"{appointment.public_id}: unknown staff member"]})
    problems = availability.window_problems(
        config,
        appointment.scheduled_start,
        appointment.scheduled_end,
        new_staff_id,
        ignore_appointment_ids=frozenset({appointment.pk}),
    )
    if availability.CONFLICT in problems:
        raise conflict("slot_unavailable", _("That staff member is busy at that time."))
    if problems:
        raise AppError(
            "staff_unavailable",
            _("That staff member is not working at that time."),
            422,
            {"resolutions": [f"{appointment.public_id}: {', '.join(problems)}"]},
        )
    old = appointment.staff_id
    appointment.staff_id = new_staff_id
    try:
        with transaction.atomic():
            appointment.save(update_fields=["staff", "updated_at"])
    except IntegrityError as exc:  # exclusion constraint: lost a race for the slot
        raise conflict("slot_unavailable", _("That staff member is busy at that time.")) from exc
    _record(appointment, appointment.status, appointment.status, actor, "reassigned")
    events.emit(events.booking_reassigned, appointment.pk, old_staff_id=old)


def _apply(
    config: biz.BookingConfig,
    excluded_staff_id: int,
    affected: list[Appointment],
    resolutions: list[Resolution],
    actor: Actor,
) -> ApplyResult:
    _check_resolutions(affected, resolutions)
    by_id = {str(a.public_id): a for a in affected}
    reassigned = cancelled = 0
    for r in sorted(resolutions, key=lambda x: by_id[x.appointment_id].scheduled_start):
        appointment = by_id[r.appointment_id]
        if r.action == "cancel":
            cancel_by_business(appointment.pk, actor, CANCEL_NOTE)
            cancelled += 1
        else:
            assert r.new_staff_id is not None
            _reassign(appointment, config, r.new_staff_id, excluded_staff_id, actor)
            reassigned += 1
    return ApplyResult(reassigned, cancelled)


def _lock_scope(owner_id: int, staff_id: int) -> tuple[biz.BookingConfig, int]:
    staff = biz.owned_staff(owner_id, staff_id)
    config = biz.get_booking_config_by_id(staff.business_id, lock=True)  # same order as booking
    assert config is not None
    biz.lock_staff(staff.pk)
    return config, staff.pk


def deactivate(
    owner_id: int,
    staff_id: int,
    resolutions: list[Resolution],
    now: datetime | None = None,
) -> ApplyResult:
    now = now or timezone.now()
    with transaction.atomic():
        config, pk = _lock_scope(owner_id, staff_id)
        staff = biz.owned_staff(owner_id, pk)
        if not staff.is_active:
            raise conflict("already_inactive", _("This staff member is already inactive."))
        affected = list(
            _affected_qs(pk, None, now).select_for_update().order_by("scheduled_start", "id")
        )
        result = _apply(config, pk, affected, resolutions, Actor(owner_id, "business_owner"))
        staff_mod.mark_inactive(staff)
    touch(config.id)
    return result


def set_services(
    owner_id: int,
    staff_id: int,
    service_ids: list[int],
    resolutions: list[Resolution],
    now: datetime | None = None,
) -> ApplyResult:
    """Replace a staff member's services; removing one with future bookings needs resolutions."""
    now = now or timezone.now()
    with transaction.atomic():
        config, pk = _lock_scope(owner_id, staff_id)
        staff = biz.owned_staff(owner_id, pk)
        valid = staff_mod.valid_service_ids_for(staff, service_ids)
        current = set(biz.staff_service_ids(pk))
        removed = sorted(current - set(valid))
        result = ApplyResult(0, 0)
        if removed:
            affected = list(
                _affected_qs(pk, removed, now).select_for_update().order_by("scheduled_start", "id")
            )
            result = _apply(config, pk, affected, resolutions, Actor(owner_id, "business_owner"))
        elif resolutions:
            raise validation_error({"resolutions": [str(_("Nothing to resolve."))]})
        staff_mod.set_links(staff, valid)
    touch(config.id)
    return result

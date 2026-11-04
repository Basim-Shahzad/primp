"""Owner-side booking actions: accept, decline, reschedule, cancel, complete, no-show, note.

Every action re-checks ownership of the appointment inside the service (never only in the
router), locks the row and reuses the Plan 6.1 status machine.
"""

from datetime import datetime, timedelta

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.businesses import selectors as biz
from apps.core.errors import AppError, conflict, not_found, validation_error

from . import availability, events, services
from .models import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentStatus,
    RescheduleRequest,
    RescheduleStatus,
)
from .services import Actor

S = AppointmentStatus

ACTION_ACCEPT = "accept"
ACTION_DECLINE = "decline"
ACTION_CANCEL = "cancel"
ACTION_RESCHEDULE = "reschedule"
ACTION_COMPLETE = "complete"
ACTION_NO_SHOW = "no_show"


def actor_for(owner_id: int) -> Actor:
    return Actor(owner_id, "business_owner")


def owned_appointment(owner_id: int, public_id: str, *, lock: bool = False) -> Appointment:
    """The appointment if it belongs to one of the owner's businesses, else 404."""
    qs = Appointment.objects.filter(public_id=public_id, business__owner_id=owner_id)
    if lock:
        qs = qs.select_for_update(of=("self",))
    appointment = qs.select_related("business", "staff", "customer").first()
    if appointment is None:
        raise not_found()
    return appointment


def complete_tolerance() -> timedelta:
    return timedelta(minutes=int(settings.PROVIDER_COMPLETE_TOLERANCE_MINUTES))


def no_show_grace() -> timedelta:
    return timedelta(minutes=int(settings.PROVIDER_NO_SHOW_GRACE_MINUTES))


def allowed_actions(appointment: Appointment, now: datetime) -> list[str]:
    """Actions the owner can take right now (drives the dashboard buttons)."""
    actions: list[str] = []
    started = appointment.scheduled_start <= now
    if appointment.status == S.PENDING:
        if not started:
            actions.append(ACTION_ACCEPT)
        actions.append(ACTION_DECLINE)
    if appointment.status in ACTIVE_STATUSES:
        actions.append(ACTION_CANCEL)
        if not started:
            actions.append(ACTION_RESCHEDULE)
    if appointment.status == S.CONFIRMED:
        if now >= appointment.scheduled_start - complete_tolerance():
            actions.append(ACTION_COMPLETE)
        if now >= appointment.scheduled_start + no_show_grace():
            actions.append(ACTION_NO_SHOW)
    return actions


def _invalid() -> AppError:
    return conflict("invalid_transition", _("This action is not possible for this booking."))


# ---------------------------------------------------------------- status actions


def accept(owner_id: int, public_id: str, now: datetime | None = None) -> Appointment:
    now = now or timezone.now()
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        if appointment.status != S.PENDING:
            raise _invalid()
        if appointment.scheduled_start <= now:
            raise conflict("appointment_started", _("The appointment has already started."))
        services.confirm(appointment.pk, actor_for(owner_id))
    return owned_appointment(owner_id, public_id)


def decline(owner_id: int, public_id: str, reason: str) -> Appointment:
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        if appointment.status != S.PENDING:
            raise _invalid()
        services.cancel_by_business(appointment.pk, actor_for(owner_id), reason or "Declined")
    return owned_appointment(owner_id, public_id)


def cancel(owner_id: int, public_id: str, reason: str) -> Appointment:
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        if appointment.status not in ACTIVE_STATUSES:
            raise _invalid()
        services.cancel_by_business(appointment.pk, actor_for(owner_id), reason)
    return owned_appointment(owner_id, public_id)


def complete(owner_id: int, public_id: str, now: datetime | None = None) -> Appointment:
    now = now or timezone.now()
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        if appointment.status != S.CONFIRMED:
            raise _invalid()
        if now < appointment.scheduled_start - complete_tolerance():
            raise conflict("appointment_not_started", _("The appointment has not started yet."))
        services.transition(appointment.pk, S.COMPLETED, actor_for(owner_id), now=now)
        events.emit(events.booking_completed, appointment.pk)
    return owned_appointment(owner_id, public_id)


def no_show(owner_id: int, public_id: str, now: datetime | None = None) -> Appointment:
    now = now or timezone.now()
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        if appointment.status != S.CONFIRMED:
            raise _invalid()
        if now < appointment.scheduled_start + no_show_grace():
            raise conflict(
                "no_show_too_early",
                _("A no-show can only be recorded after the grace period."),
            )
        services.transition(appointment.pk, S.NO_SHOW, actor_for(owner_id), now=now)
        events.emit(events.booking_no_show, appointment.pk)
    return owned_appointment(owner_id, public_id)


def set_note(owner_id: int, public_id: str, note: str) -> Appointment:
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        appointment.business_note = note
        appointment.save(update_fields=["business_note", "updated_at"])
    return owned_appointment(owner_id, public_id)


# ---------------------------------------------------------------- reschedule


def reschedule(
    owner_id: int,
    public_id: str,
    *,
    start: datetime,
    staff_id: int | None,
    now: datetime | None = None,
) -> Appointment:
    """Move an active booking to a new time (and staff). The customer is notified."""
    now = now or timezone.now()
    with transaction.atomic():
        appointment = owned_appointment(owner_id, public_id, lock=True)
        if appointment.status not in ACTIVE_STATUSES:
            raise _invalid()
        if appointment.scheduled_start <= now:
            raise conflict("appointment_started", _("The appointment has already started."))
        if start <= now:
            raise validation_error({"start": [str(_("Choose a time in the future."))]})
        config = biz.get_booking_config_by_id(appointment.business_id, lock=True)
        assert config is not None
        service_ids = list(appointment.items.values_list("service_id", flat=True))
        target_staff: int | None = None
        if config.staff_level_booking:
            target_staff = staff_id if staff_id is not None else appointment.staff_id
            if target_staff is None:
                raise validation_error({"staff_id": [str(_("Choose a staff member."))]})
            if target_staff not in biz.capable_staff_ids(config.id, service_ids):
                raise AppError(
                    "staff_cannot_perform",
                    _("This staff member cannot perform all booked services."),
                    422,
                    {"staff_id": [str(_("Not available for these services."))]},
                )
            if not biz.lock_staff(target_staff):
                raise validation_error({"staff_id": [str(_("Unknown staff member."))]})
        if start == appointment.scheduled_start and target_staff == appointment.staff_id:
            raise AppError("same_time", _("Choose a different time."), 422)
        end = start + (appointment.scheduled_end - appointment.scheduled_start)
        problems = availability.window_problems(
            config,
            start,
            end,
            target_staff,
            ignore_appointment_ids=frozenset({appointment.pk}),
        )
        if availability.OUTSIDE_HOURS in problems:
            raise AppError("outside_working_hours", _("That time is outside working hours."), 422)
        if availability.BLOCKED in problems:
            raise conflict("time_blocked", _("That time is blocked."))
        if availability.CONFLICT in problems:
            raise conflict("slot_unavailable", _("That time is no longer available."))
        RescheduleRequest.objects.filter(
            appointment=appointment, status=RescheduleStatus.PENDING
        ).update(status=RescheduleStatus.CANCELLED, decided_at=now)
        try:
            with transaction.atomic():
                services._apply_move(
                    appointment,
                    start,
                    end,
                    target_staff,
                    actor_for(owner_id),
                    "rescheduled by business",
                    by="business",
                )
        except IntegrityError as exc:
            raise conflict("slot_unavailable", _("That time is no longer available.")) from exc
    return owned_appointment(owner_id, public_id)


# ------------------------------------------------- customer reschedule requests


def _owned_request(owner_id: int, public_id: str) -> RescheduleRequest:
    appointment = owned_appointment(owner_id, public_id)
    request = (
        RescheduleRequest.objects.filter(appointment=appointment, status=RescheduleStatus.PENDING)
        .order_by("-id")
        .first()
    )
    if request is None:
        raise conflict("no_pending_request", _("There is no reschedule request to answer."))
    return request


def accept_reschedule_request(
    owner_id: int, public_id: str, now: datetime | None = None
) -> Appointment:
    request = _owned_request(owner_id, public_id)
    services.accept_reschedule(request.pk, actor_for(owner_id), now)
    return owned_appointment(owner_id, public_id)


def decline_reschedule_request(
    owner_id: int, public_id: str, now: datetime | None = None
) -> Appointment:
    request = _owned_request(owner_id, public_id)
    services.decline_reschedule(request.pk, actor_for(owner_id), now)
    return owned_appointment(owner_id, public_id)

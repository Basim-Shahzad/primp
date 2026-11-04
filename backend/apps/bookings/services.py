"""Booking write-side: holds, appointment creation and the status machine."""

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from django.core import signing
from django.db import IntegrityError, transaction
from django.db.models import Count
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from redis.exceptions import RedisError

from apps.businesses import selectors as biz
from apps.core import ratelimit
from apps.core.errors import AppError, conflict, not_found

from . import availability, events, holds
from .models import (
    ACTIVE_STATUSES,
    Appointment,
    AppointmentEvent,
    AppointmentService,
    AppointmentSource,
    AppointmentStatus,
    PaymentMethod,
    PaymentStatus,
    RescheduleRequest,
    RescheduleStatus,
)

logger = logging.getLogger(__name__)

__all__ = ["PaymentStatus"]  # re-exported for the payments app

SOFT_PREFIX = "soft."
SOFT_SALT = "primp.soft-hold"
HOLD_RATE = (30, 60)  # holds per user per minute

S = AppointmentStatus
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    S.PENDING: {S.CONFIRMED, S.CANCELLED_BY_CUSTOMER, S.CANCELLED_BY_BUSINESS},
    S.CONFIRMED: {S.COMPLETED, S.NO_SHOW, S.CANCELLED_BY_CUSTOMER, S.CANCELLED_BY_BUSINESS},
    S.COMPLETED: set(),
    S.CANCELLED_BY_CUSTOMER: set(),
    S.CANCELLED_BY_BUSINESS: set(),
    S.NO_SHOW: set(),
}


@dataclass(frozen=True)
class Actor:
    user_id: int | None
    role: str


@dataclass(frozen=True)
class HoldResult:
    token: str
    expires_at: datetime
    business_id: int
    staff_id: int | None
    start: datetime
    end: datetime
    soft: bool = False


def _slot_unavailable() -> AppError:
    return conflict("slot_unavailable", _("That time is no longer available."))


# ---------------------------------------------------------------- holds


def _staff_preference(
    business_id: int, day_start: datetime, day_end: datetime, ids: list[int]
) -> list[int]:
    """Least-busy first (appointments that day), ties by id: spreads 'any staff' bookings."""
    counts = dict.fromkeys(ids, 0)
    rows = (
        Appointment.objects.filter(
            business_id=business_id,
            staff_id__in=ids,
            status__in=ACTIVE_STATUSES,
            scheduled_start__gte=day_start,
            scheduled_start__lt=day_end,
        )
        .values("staff_id")
        .annotate(n=Count("id"))
    )
    for row in rows:
        counts[row["staff_id"]] = row["n"]
    return sorted(ids, key=lambda sid: (counts[sid], sid))


def create_hold(
    *,
    user_id: int,
    business_ref: str,
    service_ids: list[int],
    staff_id: int | None,
    start: datetime,
    now: datetime | None = None,
    appointment_id: str | None = None,
) -> HoldResult:
    """Hold a slot. Pass `appointment_id` (own booking) to hold a new time for a reschedule: that
    booking then does not block its own replacement slot."""
    ratelimit.enforce(f"hold-user:{user_id}", limit=HOLD_RATE[0], window_seconds=HOLD_RATE[1])
    config = biz.get_booking_config(business_ref)
    if config is None:
        raise not_found()
    now = now or timezone.now()
    ids = list(dict.fromkeys(service_ids))
    ignore: frozenset[int] = frozenset()
    if appointment_id:
        own = _own_appointment(user_id, appointment_id)
        if own.business_id != config.id or own.status not in ACTIVE_STATUSES:
            raise AppError("hold_mismatch", _("That appointment cannot be rescheduled here."), 422)
        ignore = frozenset({own.pk})
    first = availability.find_slot(
        config, start, ids, staff_id, now=now, ignore_appointment_ids=ignore
    )
    if first is None:
        raise _slot_unavailable()
    services = availability.load_services(config, ids)
    end = first.end

    if config.staff_level_booking:
        tz = ZoneInfo(config.timezone)
        local_start = start.astimezone(tz)
        day_start = local_start.replace(hour=0, minute=0, second=0, microsecond=0)
        ordered: list[int] = (
            [staff_id]
            if staff_id is not None
            else _staff_preference(
                config.id, day_start, day_start + timedelta(days=1), list(first.staff_ids)
            )
        )
        candidates: list[int | None] = [*ordered]
    else:
        candidates = [None]

    for candidate in candidates:
        try:
            with holds.hold_lock(config.id, candidate):
                # re-check under the lock: someone may have taken it since the first look
                slot = availability.find_slot(
                    config, start, ids, candidate, now=now, ignore_appointment_ids=ignore
                )
                if slot is None:
                    continue
                hold = holds.place_hold(
                    user_id=user_id,
                    business_id=config.id,
                    staff_id=candidate,
                    start=start,
                    end=end,
                    service_ids=tuple(s.id for s in services),
                    capacity=config.parallel_capacity if candidate is None else 1,
                )
        except RedisError:
            logger.warning(
                "Redis unavailable: issuing a soft hold; the DB constraint still protects the slot"
            )
            return _soft_hold(
                user_id, config.id, candidate, start, end, tuple(s.id for s in services)
            )
        if hold is not None:
            biz_cache_touch(config.id)
            return HoldResult(hold.token, hold.expires_at, config.id, candidate, start, end)
    raise _slot_unavailable()


def biz_cache_touch(business_id: int) -> None:
    from .cache_keys import invalidate_next_slot

    invalidate_next_slot(business_id)


def _soft_hold(
    user_id: int,
    business_id: int,
    staff_id: int | None,
    start: datetime,
    end: datetime,
    service_ids: tuple[int, ...],
) -> HoldResult:
    expires = timezone.now() + timedelta(seconds=holds.hold_ttl_seconds())
    payload = {
        "u": user_id,
        "b": business_id,
        "s": staff_id,
        "st": start.isoformat(),
        "en": end.isoformat(),
        "sv": list(service_ids),
    }
    token = SOFT_PREFIX + signing.dumps(payload, salt=SOFT_SALT)
    return HoldResult(token, expires, business_id, staff_id, start, end, soft=True)


def _resolve_soft(token: str) -> holds.Hold | None:
    try:
        d = signing.loads(
            token[len(SOFT_PREFIX) :], salt=SOFT_SALT, max_age=holds.hold_ttl_seconds()
        )
    except signing.BadSignature:
        return None
    return holds.Hold(
        token=token,
        user_id=d["u"],
        business_id=d["b"],
        staff_id=d["s"],
        start=datetime.fromisoformat(d["st"]),
        end=datetime.fromisoformat(d["en"]),
        expires_at=timezone.now(),
        service_ids=tuple(d["sv"]),
    )


def resolve_hold(token: str, user_id: int) -> holds.Hold:
    """The caller's live hold, else 409 hold_expired. Another user's hold looks like 404."""
    if token.startswith(SOFT_PREFIX):
        hold = _resolve_soft(token)
    else:
        try:
            hold = holds.get_hold(token)
        except RedisError as exc:
            raise AppError(
                "holds_unavailable", _("Booking is temporarily unavailable. Try again."), 503
            ) from exc
    if hold is None:
        raise conflict("hold_expired", _("Your hold has expired. Please pick a time again."))
    if hold.user_id != user_id:
        raise not_found()
    return hold


def release_hold(token: str, user_id: int) -> None:
    if token.startswith(SOFT_PREFIX):
        return
    try:
        hold = holds.get_hold(token)
        if hold is None:
            return
        if hold.user_id != user_id:
            raise not_found()
        holds.release_hold(token)
    except RedisError:
        logger.warning("Redis unavailable while releasing a hold; it will expire on its own")
        return
    biz_cache_touch(hold.business_id)


# ---------------------------------------------------------- appointments


def _record(
    appointment: Appointment, from_status: str, to_status: str, actor: Actor, note: str = ""
) -> None:
    AppointmentEvent.objects.create(
        appointment=appointment,
        from_status=from_status,
        to_status=to_status,
        actor_id=actor.user_id,
        actor_role=actor.role,
        note=note[:500],
    )


def create_appointment(
    *,
    user_id: int,
    hold_token: str,
    idempotency_key: str | None = None,
    now: datetime | None = None,
) -> tuple[Appointment, bool]:
    """Turn a hold into a booking. Returns (appointment, created); created is False on replay."""
    if idempotency_key:
        existing = Appointment.objects.filter(
            customer_id=user_id, idempotency_key=idempotency_key
        ).first()
        if existing:
            return existing, False
    now = now or timezone.now()
    hold = resolve_hold(hold_token, user_id)
    try:
        with transaction.atomic():
            appointment = _create_locked(user_id, hold, hold_token, idempotency_key, now)
    except IntegrityError as exc:
        if idempotency_key:
            winner = Appointment.objects.filter(
                customer_id=user_id, idempotency_key=idempotency_key
            ).first()
            if winner:
                return winner, False
        raise _slot_unavailable() from exc  # exclusion constraint: another booking won the slot
    try:
        if not hold_token.startswith(SOFT_PREFIX):
            holds.release_hold(hold_token)
    except RedisError:
        logger.warning("Redis unavailable while releasing a hold after booking")
    return appointment, True


def _create_locked(
    user_id: int, hold: holds.Hold, token: str, key: str | None, now: datetime
) -> Appointment:
    config = biz.get_booking_config_by_id(hold.business_id, lock=True)  # serialises per business
    if config is None or biz.get_booking_config(config.public_id) is None:
        raise not_found()
    services = availability.load_services(config, list(hold.service_ids))
    if config.staff_level_booking and (hold.staff_id is None or not biz.lock_staff(hold.staff_id)):
        raise _slot_unavailable()
    slot = availability.find_slot(
        config,
        hold.start,
        [s.id for s in services],
        hold.staff_id if config.staff_level_booking else None,
        now=now,
        ignore_hold_tokens=frozenset({token}),
    )
    if slot is None:
        raise _slot_unavailable()
    status = S.CONFIRMED if config.confirm_mode == "instant" else S.PENDING
    appointment = Appointment.objects.create(
        customer_id=user_id,
        business_id=config.id,
        staff_id=hold.staff_id if config.staff_level_booking else None,
        status=status,
        scheduled_start=hold.start,
        scheduled_end=slot.end,
        total_price=sum(s.price for s in services),
        payment_method=PaymentMethod.PAY_AT_VENUE,
        payment_status=PaymentStatus.UNPAID,
        source=AppointmentSource.ONLINE,
        idempotency_key=key or None,
    )
    AppointmentService.objects.bulk_create(
        [
            AppointmentService(
                appointment=appointment,
                service_id=s.id,
                name=s.name,
                price_at_booking=s.price,
                duration_at_booking=s.duration_minutes,
            )
            for s in services
        ]
    )
    _record(appointment, "", status, Actor(user_id, "customer"))
    events.emit(events.booking_created, appointment.pk)
    if status == S.CONFIRMED:
        events.emit(events.booking_confirmed, appointment.pk)
    return appointment


# ---------------------------------------------------------- status machine


def _locked(appointment_id: int) -> Appointment:
    appointment = Appointment.objects.select_for_update().filter(pk=appointment_id).first()
    if appointment is None:
        raise not_found()
    return appointment


def transition(
    appointment_id: int,
    to_status: str,
    actor: Actor,
    *,
    note: str = "",
    require_started: bool = False,
    now: datetime | None = None,
) -> Appointment:
    """Move an appointment to a new status along the allowed edges, writing a timeline event."""
    with transaction.atomic():
        appointment = _locked(appointment_id)
        if to_status not in ALLOWED_TRANSITIONS[appointment.status]:
            raise conflict(
                "invalid_transition",
                _("Cannot change an appointment from %(from)s to %(to)s.")
                % {"from": appointment.status, "to": to_status},
            )
        if require_started and (now or timezone.now()) < appointment.scheduled_start:
            raise conflict("appointment_not_started", _("The appointment has not started yet."))
        previous = appointment.status
        appointment.status = to_status
        update = ["status", "updated_at"]
        if to_status in (S.CANCELLED_BY_BUSINESS, S.CANCELLED_BY_CUSTOMER):
            appointment.cancelled_at = timezone.now()
            update.append("cancelled_at")
        appointment.save(update_fields=update)
        _record(appointment, previous, to_status, actor, note)
    return appointment


def confirm(appointment_id: int, actor: Actor) -> Appointment:
    appointment = transition(appointment_id, S.CONFIRMED, actor)
    events.emit(events.booking_confirmed, appointment.pk)
    return appointment


def complete(appointment_id: int, actor: Actor, *, now: datetime | None = None) -> Appointment:
    appointment = transition(appointment_id, S.COMPLETED, actor, require_started=True, now=now)
    events.emit(events.booking_completed, appointment.pk)
    return appointment


def mark_no_show(appointment_id: int, actor: Actor, *, now: datetime | None = None) -> Appointment:
    appointment = transition(appointment_id, S.NO_SHOW, actor, require_started=True, now=now)
    events.emit(events.booking_no_show, appointment.pk)
    return appointment


def cancel_by_business(appointment_id: int, actor: Actor, reason: str = "") -> Appointment:
    with transaction.atomic():
        appointment = transition(appointment_id, S.CANCELLED_BY_BUSINESS, actor, note=reason)
        appointment.cancellation_reason = "business"
        appointment.cancellation_note = reason[:500]
        appointment.save(update_fields=["cancellation_reason", "cancellation_note", "updated_at"])
        RescheduleRequest.objects.filter(
            appointment=appointment, status=RescheduleStatus.PENDING
        ).update(status=RescheduleStatus.CANCELLED, decided_at=timezone.now())
        events.emit(events.booking_cancelled, appointment.pk, cancelled_by="business")
    return appointment


def set_payment_status(appointment_public_id: str, payment_status: str) -> bool:
    """Idempotently record a payment outcome. False when the appointment is unknown."""
    with transaction.atomic():
        appointment = (
            Appointment.objects.select_for_update().filter(public_id=appointment_public_id).first()
        )
        if appointment is None:
            return False
        if appointment.payment_status != payment_status:
            appointment.payment_status = payment_status
            appointment.save(update_fields=["payment_status", "updated_at"])
            events.emit(events.booking_payment_changed, appointment.pk)
    return True


# ------------------------------------------------------- customer actions

CANCEL_REASONS = (
    "change_of_plans",
    "found_alternative",
    "schedule_conflict",
    "price",
    "emergency",
    "other",
)


@dataclass(frozen=True)
class CancellationPolicy:
    notice_hours: int
    free_until: datetime
    is_late_now: bool
    can_cancel: bool


def _own_appointment(user_id: int, public_id: str, *, lock: bool = False) -> Appointment:
    qs = Appointment.objects.filter(public_id=public_id, customer_id=user_id)
    appointment = (qs.select_for_update() if lock else qs).first()
    if appointment is None:
        raise not_found()
    return appointment


def cancellation_policy(
    appointment: Appointment, now: datetime | None = None
) -> CancellationPolicy:
    now = now or timezone.now()
    config = biz.get_booking_config_by_id(appointment.business_id)
    notice = config.cancellation_notice_hours if config else 0
    tz = ZoneInfo(config.timezone) if config else timezone.get_current_timezone()
    free_until = (appointment.scheduled_start - timedelta(hours=notice)).astimezone(tz)
    can_cancel = appointment.status in ACTIVE_STATUSES and now < appointment.scheduled_start
    # late means strictly inside the notice window; exactly at the boundary is still free
    return CancellationPolicy(notice, free_until, now > free_until, can_cancel)


def get_policy(user_id: int, public_id: str, now: datetime | None = None) -> CancellationPolicy:
    return cancellation_policy(_own_appointment(user_id, public_id), now)


def cancel_by_customer(
    user_id: int, public_id: str, reason: str, note: str = "", now: datetime | None = None
) -> Appointment:
    """Cancel own booking. Late cancellations (inside the notice window) are allowed but flagged."""
    now = now or timezone.now()
    with transaction.atomic():
        appointment = _own_appointment(user_id, public_id, lock=True)
        if S.CANCELLED_BY_CUSTOMER not in ALLOWED_TRANSITIONS[appointment.status]:
            raise conflict(
                "invalid_transition",
                _("This appointment can no longer be cancelled."),
            )
        if appointment.scheduled_start <= now:
            raise conflict("appointment_started", _("The appointment has already started."))
        policy = cancellation_policy(appointment, now)
        previous = appointment.status
        appointment.status = S.CANCELLED_BY_CUSTOMER
        appointment.cancellation_reason = reason
        appointment.cancellation_note = note.strip()[:500]
        appointment.cancelled_at = now
        appointment.is_late_cancellation = policy.is_late_now
        appointment.save(
            update_fields=[
                "status",
                "cancellation_reason",
                "cancellation_note",
                "cancelled_at",
                "is_late_cancellation",
                "updated_at",
            ]
        )
        RescheduleRequest.objects.filter(
            appointment=appointment, status=RescheduleStatus.PENDING
        ).update(status=RescheduleStatus.CANCELLED, decided_at=now)
        _record(
            appointment,
            previous,
            S.CANCELLED_BY_CUSTOMER,
            Actor(user_id, "customer"),
            f"{reason}{' (late)' if policy.is_late_now else ''}",
        )
        events.emit(
            events.booking_cancelled,
            appointment.pk,
            cancelled_by="customer",
            is_late=policy.is_late_now,
        )
    return appointment


@dataclass(frozen=True)
class RescheduleResult:
    appointment: Appointment
    request: RescheduleRequest | None
    applied: bool


def _validate_new_slot(
    appointment: Appointment,
    config: biz.BookingConfig,
    start: datetime,
    staff_id: int | None,
    service_ids: list[int],
    token: str | None,
    now: datetime,
) -> availability.Slot:
    ignore_tokens = frozenset({token}) if token else frozenset()
    slot = availability.find_slot(
        config,
        start,
        service_ids,
        staff_id if config.staff_level_booking else None,
        now=now,
        ignore_hold_tokens=ignore_tokens,
        ignore_appointment_ids=frozenset({appointment.pk}),
    )
    if slot is None:
        raise _slot_unavailable()
    return slot


def _apply_move(
    appointment: Appointment,
    start: datetime,
    end: datetime,
    staff_id: int | None,
    actor: Actor,
    note: str,
    by: str = "customer",
    **event_kwargs: object,
) -> None:
    appointment.scheduled_start = start
    appointment.scheduled_end = end
    appointment.staff_id = staff_id
    appointment.save(update_fields=["scheduled_start", "scheduled_end", "staff", "updated_at"])
    _record(appointment, appointment.status, appointment.status, actor, note)
    events.emit(events.booking_rescheduled, appointment.pk, by=by, **event_kwargs)


def reschedule_by_customer(
    user_id: int, public_id: str, hold_token: str, now: datetime | None = None
) -> RescheduleResult:
    """Move a booking to a newly held slot (instantly, or as a request for manual-confirm shops)."""
    now = now or timezone.now()
    hold = resolve_hold(hold_token, user_id)
    actor = Actor(user_id, "customer")
    try:
        with transaction.atomic():
            appointment = _own_appointment(user_id, public_id, lock=True)
            if appointment.status not in ACTIVE_STATUSES:
                raise conflict(
                    "invalid_transition", _("This appointment can no longer be rescheduled.")
                )
            if appointment.scheduled_start <= now:
                raise conflict("appointment_started", _("The appointment has already started."))
            if hold.business_id != appointment.business_id:
                raise AppError("hold_mismatch", _("That hold is for a different business."), 422)
            current_services = set(appointment.items.values_list("service_id", flat=True))
            if set(hold.service_ids) != current_services:
                raise AppError("hold_mismatch", _("That hold is for different services."), 422)
            if hold.start == appointment.scheduled_start and (
                hold.staff_id in (None, appointment.staff_id)
            ):
                raise AppError("same_time", _("Choose a different time."), 422)
            config = biz.get_booking_config_by_id(appointment.business_id, lock=True)
            assert config is not None
            if config.staff_level_booking and (
                hold.staff_id is None or not biz.lock_staff(hold.staff_id)
            ):
                raise _slot_unavailable()
            slot = _validate_new_slot(
                appointment,
                config,
                hold.start,
                hold.staff_id,
                sorted(current_services),
                hold_token,
                now,
            )
            staff_id = hold.staff_id if config.staff_level_booking else None
            if config.confirm_mode == "instant":
                _apply_move(appointment, hold.start, slot.end, staff_id, actor, "rescheduled")
                result = RescheduleResult(appointment, None, True)
            else:
                if RescheduleRequest.objects.filter(
                    appointment=appointment, status=RescheduleStatus.PENDING
                ).exists():
                    raise conflict(
                        "reschedule_pending",
                        _("A reschedule request is already waiting for approval."),
                    )
                request = RescheduleRequest.objects.create(
                    appointment=appointment,
                    new_start=hold.start,
                    new_end=slot.end,
                    new_staff_id=staff_id,
                )
                _record(
                    appointment,
                    appointment.status,
                    appointment.status,
                    actor,
                    "reschedule requested",
                )
                events.emit(events.booking_reschedule_requested, appointment.pk)
                result = RescheduleResult(appointment, request, False)
    except IntegrityError as exc:
        raise _slot_unavailable() from exc
    _release_after(hold_token)
    return result


def _release_after(token: str) -> None:
    if token.startswith(SOFT_PREFIX):
        return
    try:
        holds.release_hold(token)
    except RedisError:
        logger.warning("Redis unavailable while releasing a hold after reschedule")


def accept_reschedule(
    request_id: int, actor: Actor, now: datetime | None = None
) -> RescheduleResult:
    """Provider accepts a pending request (exposed by Plan 6.2). Re-validates the slot first."""
    now = now or timezone.now()
    try:
        with transaction.atomic():
            request = RescheduleRequest.objects.select_for_update().filter(pk=request_id).first()
            if request is None:
                raise not_found()
            if request.status != RescheduleStatus.PENDING:
                raise conflict("invalid_transition", _("This request was already handled."))
            appointment = _locked(request.appointment_id)
            if appointment.status not in ACTIVE_STATUSES or appointment.scheduled_start <= now:
                raise conflict("invalid_transition", _("The appointment can no longer be moved."))
            config = biz.get_booking_config_by_id(appointment.business_id, lock=True)
            assert config is not None
            if config.staff_level_booking and (
                request.new_staff_id is None or not biz.lock_staff(request.new_staff_id)
            ):
                raise _slot_unavailable()
            service_ids = sorted(appointment.items.values_list("service_id", flat=True))
            slot = _validate_new_slot(
                appointment, config, request.new_start, request.new_staff_id, service_ids, None, now
            )
            _apply_move(
                appointment,
                request.new_start,
                slot.end,
                request.new_staff_id,
                actor,
                "reschedule accepted",
                by="business",
                accepted_request=True,
            )
            request.status = RescheduleStatus.ACCEPTED
            request.decided_at = now
            request.save(update_fields=["status", "decided_at", "updated_at"])
    except IntegrityError as exc:
        raise _slot_unavailable() from exc
    return RescheduleResult(appointment, request, True)


def decline_reschedule(
    request_id: int, actor: Actor, now: datetime | None = None
) -> RescheduleRequest:
    with transaction.atomic():
        request = RescheduleRequest.objects.select_for_update().filter(pk=request_id).first()
        if request is None:
            raise not_found()
        if request.status != RescheduleStatus.PENDING:
            raise conflict("invalid_transition", _("This request was already handled."))
        request.status = RescheduleStatus.DECLINED
        request.decided_at = now or timezone.now()
        request.save(update_fields=["status", "decided_at", "updated_at"])
        appointment = request.appointment
        _record(appointment, appointment.status, appointment.status, actor, "reschedule declined")
        events.emit(events.booking_reschedule_declined, appointment.pk)
    return request

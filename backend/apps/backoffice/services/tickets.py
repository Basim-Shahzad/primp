"""Support tickets and disputes: requester side, admin side, automatic tickets, statistics."""

import statistics
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import Case, IntegerField, QuerySet, Value, When
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.accounts import adminops as accounts
from apps.bookings import adminops as bookings
from apps.businesses import adminops as businesses
from apps.core import audit, ratelimit
from apps.core.errors import conflict, not_found, validation_error
from apps.notifications import services as notifications
from apps.reviews import adminops as reviews

from ..models import (
    Ticket,
    TicketEvent,
    TicketMessage,
    TicketPriority,
    TicketStatus,
    TicketType,
)
from ..permissions import AdminActor, permissions_of

USER_TYPES = (
    TicketType.BOOKING_DISPUTE,
    TicketType.REFUND_REQUEST,
    TicketType.PROVIDER_COMPLAINT,
    TicketType.ACCOUNT_ISSUE,
    TicketType.OTHER,
)
CREATE_LIMIT = (10, 3600)
MESSAGE_LIMIT = (30, 3600)
MAX_MESSAGES = 200
S = TicketStatus
TRANSITIONS: dict[str, set[str]] = {
    S.OPEN: {S.IN_PROGRESS},
    S.IN_PROGRESS: {S.RESOLVED},
    S.RESOLVED: {S.CLOSED, S.IN_PROGRESS},
    S.CLOSED: set(),
}
PRIORITY_RANK = {
    TicketPriority.URGENT: 0,
    TicketPriority.HIGH: 1,
    TicketPriority.NORMAL: 2,
    TicketPriority.LOW: 3,
}
SORTS = {
    "priority": ("priority_rank", "created_at", "id"),
    "created_at": ("created_at", "id"),
    "-created_at": ("-created_at", "-id"),
    "updated_at": ("updated_at", "id"),
    "-updated_at": ("-updated_at", "-id"),
}


def default_priority(ticket_type: str) -> str:
    configured = getattr(settings, "TICKET_PRIORITIES", {})
    value = str(configured.get(ticket_type, "normal"))
    return value if value in TicketPriority.values else "normal"


def _event(ticket: Ticket, kind: str, actor_id: int | None, old: str = "", new: str = "") -> None:
    TicketEvent.objects.create(
        ticket=ticket, actor_id=actor_id, kind=kind, from_value=old, to_value=new
    )


def get_ticket(ref: str, *, lock: bool = False) -> Ticket:
    import uuid

    try:
        public_id = uuid.UUID(str(ref))
    except ValueError:
        raise not_found() from None
    qs = Ticket.objects.select_related("requester", "assigned_to")
    if lock:
        qs = qs.select_for_update(of=("self",))
    ticket = qs.filter(public_id=public_id).first()
    if ticket is None:
        raise not_found()
    return ticket


# ---------------------------------------------------------------- requester side


def create_ticket(
    user: Any,
    *,
    type: str,
    subject: str,
    body: str,
    appointment_id: str | None,
    business_id: str | None,
    review_id: str | None,
) -> Ticket:
    if type not in USER_TYPES:
        raise validation_error({"type": [str(_("This kind of request cannot be opened here."))]})
    needs: dict[str, list[str]] = {}
    if type in (TicketType.BOOKING_DISPUTE, TicketType.REFUND_REQUEST) and not appointment_id:
        needs["appointment_id"] = [str(_("Choose the booking this is about."))]
    if type == TicketType.PROVIDER_COMPLAINT and not (business_id or appointment_id):
        needs["business_id"] = [str(_("Choose the business this is about."))]
    if needs:
        raise validation_error(needs)
    ratelimit.enforce(
        f"ticket-create:{user.pk}", limit=CREATE_LIMIT[0], window_seconds=CREATE_LIMIT[1]
    )
    appointment_pk = business_pk = review_pk = None
    if appointment_id:
        found = bookings.appointment_for_requester(appointment_id, user.pk)
        if found is None:
            raise not_found()
        appointment_pk, business_pk = found
    if business_id:
        linked = businesses.business_for_requester(business_id, user.pk)
        if linked is None or (business_pk is not None and linked != business_pk):
            raise not_found()
        business_pk = linked
    if review_id:
        review_pk = reviews.review_for_requester(review_id, user.pk)
        if review_pk is None:
            raise not_found()
    with transaction.atomic():
        ticket = Ticket.objects.create(
            type=type,
            priority=default_priority(type),
            subject=subject.strip(),
            requester_id=user.pk,
            appointment_id=appointment_pk,
            business_id=business_pk,
            review_id=review_pk,
        )
        TicketMessage.objects.create(ticket=ticket, author_id=user.pk, body=body.strip())
        _event(ticket, "created", user.pk, "", ticket.status)
    return ticket


def own_tickets(user_id: int, status: str | None) -> QuerySet[Ticket]:
    qs = Ticket.objects.filter(requester_id=user_id)
    return (qs.filter(status=status) if status else qs).order_by("-created_at", "-id")


def get_own(user_id: int, ref: str) -> Ticket:
    ticket = get_ticket(ref)
    if ticket.requester_id != user_id:
        raise not_found()
    return ticket


def user_message(user: Any, ref: str, body: str) -> TicketMessage:
    ratelimit.enforce(
        f"ticket-message:{user.pk}", limit=MESSAGE_LIMIT[0], window_seconds=MESSAGE_LIMIT[1]
    )
    with transaction.atomic():
        ticket = get_ticket(ref, lock=True)
        if ticket.requester_id != user.pk:
            raise not_found()
        if ticket.status == S.CLOSED:
            raise conflict("ticket_closed", _("This request is closed."))
        if ticket.messages.count() >= MAX_MESSAGES:
            raise conflict("too_many_messages", _("This request has too many messages."))
        message = TicketMessage.objects.create(ticket=ticket, author_id=user.pk, body=body.strip())
        if ticket.status == S.RESOLVED:  # a reply reopens it
            ticket.status = S.IN_PROGRESS
            ticket.resolved_at = None
            ticket.version += 1
            ticket.save(update_fields=["status", "resolved_at", "version", "updated_at"])
            _event(ticket, "status", user.pk, S.RESOLVED, S.IN_PROGRESS)
        else:
            ticket.save(update_fields=["updated_at"])
        _event(ticket, "message", user.pk)
    return message


# ---------------------------------------------------------------- admin side


def admin_queryset(
    *,
    status: str | None,
    priority: str | None,
    ticket_type: str | None,
    assignee: str | None,
    older_than_hours: int | None,
    admin_id: int,
    sort: str,
) -> QuerySet[Ticket]:
    qs = Ticket.objects.select_related("requester__profile", "assigned_to__profile").annotate(
        priority_rank=Case(
            *[When(priority=k, then=Value(v)) for k, v in PRIORITY_RANK.items()],
            default=Value(9),
            output_field=IntegerField(),
        )
    )
    if status:
        qs = qs.filter(status=status)
    if priority:
        qs = qs.filter(priority=priority)
    if ticket_type:
        qs = qs.filter(type=ticket_type)
    if assignee == "me":
        qs = qs.filter(assigned_to_id=admin_id)
    elif assignee == "unassigned":
        qs = qs.filter(assigned_to__isnull=True)
    elif assignee:
        admin = accounts.get_user(assignee)
        qs = qs.filter(assigned_to_id=admin.pk)
    if older_than_hours is not None:
        qs = qs.filter(created_at__lte=timezone.now() - timedelta(hours=older_than_hours))
    return qs.order_by(*SORTS[sort])


def linked_summaries(ticket: Ticket) -> dict[str, Any]:
    out: dict[str, Any] = {}
    if ticket.appointment_id:
        out["appointment"] = bookings.appointment_summary(ticket.appointment_id)
    if ticket.business_id:
        out["business"] = businesses.business_summary(ticket.business_id)
    if ticket.review_id:
        out["review"] = reviews.review_summary(ticket.review_id)
    if ticket.related_user_id:
        out["user"] = {"id": ticket.related_user.public_id}  # type: ignore[union-attr]
    return out


def _check_version(ticket: Ticket, version: int) -> None:
    if ticket.version != version:
        raise conflict("stale_version", _("The ticket changed; reload it and try again."))


def assign(actor: AdminActor, ref: str, assignee_ref: str | None, version: int) -> Ticket:
    actor.require("tickets.handle")
    with transaction.atomic():
        ticket = get_ticket(ref, lock=True)
        _check_version(ticket, version)
        assignee = None
        if assignee_ref is not None:
            assignee = accounts.get_user(assignee_ref)
            if "tickets.handle" not in permissions_of(assignee):
                raise validation_error(
                    {"assignee_id": [str(_("This admin cannot handle tickets."))]}
                )
        before = ticket.assigned_to.public_id if ticket.assigned_to_id else None  # type: ignore[union-attr]
        ticket.assigned_to = assignee
        ticket.version += 1
        ticket.save(update_fields=["assigned_to", "version", "updated_at"])
        _event(
            ticket,
            "assigned",
            actor.id,
            str(before or ""),
            str(assignee.public_id) if assignee else "",
        )
        audit.record(
            "admin.ticket_assigned",
            actor_id=actor.id,
            target_type="ticket",
            target_id=ticket.public_id,
            before={"assigned_to": str(before) if before else None},
            after={"assigned_to": str(assignee.public_id) if assignee else None},
            ip=actor.ip,
        )
    return ticket


def change_status(actor: AdminActor, ref: str, status: str, version: int) -> Ticket:
    actor.require("tickets.handle")
    with transaction.atomic():
        ticket = get_ticket(ref, lock=True)
        _check_version(ticket, version)
        if status not in TRANSITIONS.get(ticket.status, set()):
            raise conflict(
                "invalid_transition",
                _("A %(from)s ticket cannot move to %(to)s.")
                % {"from": ticket.status, "to": status},
            )
        old = ticket.status
        now = timezone.now()
        ticket.status = status
        if status == S.RESOLVED:
            ticket.resolved_at = now
        elif status == S.CLOSED:
            ticket.closed_at = now
        elif status == S.IN_PROGRESS:
            ticket.resolved_at = None  # reopened
        ticket.version += 1
        ticket.save()
        _event(ticket, "status", actor.id, old, status)
        audit.record(
            "admin.ticket_status_changed",
            actor_id=actor.id,
            target_type="ticket",
            target_id=ticket.public_id,
            before={"status": old},
            after={"status": status},
            ip=actor.ip,
        )
    return ticket


def admin_message(actor: AdminActor, ref: str, body: str, internal: bool) -> TicketMessage:
    actor.require("tickets.handle")
    with transaction.atomic():
        ticket = get_ticket(ref, lock=True)
        if ticket.status == S.CLOSED:
            raise conflict("ticket_closed", _("This ticket is closed."))
        if ticket.messages.count() >= MAX_MESSAGES:
            raise conflict("too_many_messages", _("This ticket has too many messages."))
        message = TicketMessage.objects.create(
            ticket=ticket, author_id=actor.id, body=body.strip(), is_internal=internal
        )
        if not internal and ticket.first_response_at is None:
            ticket.first_response_at = timezone.now()
        ticket.save(update_fields=["first_response_at", "updated_at"])
        _event(ticket, "note" if internal else "reply", actor.id)
        audit.record(
            "admin.ticket_note_added" if internal else "admin.ticket_replied",
            actor_id=actor.id,
            target_type="ticket",
            target_id=ticket.public_id,
            after={"message_id": message.pk, "internal": internal},
            ip=actor.ip,
        )
        if not internal and ticket.requester_id is not None:
            notifications.notify(
                ticket.requester_id,
                "ticket_reply",
                {"subject": ticket.subject, "message": body.strip()[:200]},
                include_sms=False,
            )
    return message


def stats(
    actor: AdminActor, start: date | None, end: date | None, now: datetime | None = None
) -> dict[str, Any]:
    actor.require("tickets.view")
    tz = ZoneInfo(settings.BUSINESS_TIME_ZONE)
    today = (now or timezone.now()).astimezone(tz).date()
    end = end or today
    start = start or end - timedelta(days=29)
    if end < start:
        raise validation_error({"to": [str(_("The end date must not be before the start."))]})
    if (end - start).days >= 366:
        raise validation_error({"to": [str(_("The range may span at most 366 days."))]})
    lo = datetime.combine(start, time.min, tzinfo=tz)
    hi = datetime.combine(end + timedelta(days=1), time.min, tzinfo=tz)
    rows = list(Ticket.objects.filter(created_at__gte=lo, created_at__lt=hi))
    by_status = dict.fromkeys(TicketStatus.values, 0)
    for t in rows:
        by_status[t.status] += 1
    first = [
        (t.first_response_at - t.created_at).total_seconds() for t in rows if t.first_response_at
    ]
    resolved = [(t.resolved_at - t.created_at).total_seconds() for t in rows if t.resolved_at]
    return {
        "from_date": start,
        "to_date": end,
        "total": len(rows),
        "by_status": by_status,
        "median_first_response_seconds": int(statistics.median(first)) if first else None,
        "median_resolution_seconds": int(statistics.median(resolved)) if resolved else None,
        "first_response_count": len(first),
        "resolved_count": len(resolved),
    }


# ---------------------------------------------------------------- automatic tickets


def system_ticket(
    *,
    type: str,
    subject: str,
    body: str,
    source_key: str,
    requester_id: int | None = None,
    related_user_id: int | None = None,
    appointment_id: int | None = None,
    business_id: int | None = None,
    review_id: int | None = None,
    priority: str | None = None,
) -> Ticket | None:
    """Create a ticket once per `source_key`; a repeated delivery returns None."""
    try:
        with transaction.atomic():
            ticket = Ticket.objects.create(
                type=type,
                priority=priority or default_priority(type),
                subject=subject[:200],
                requester_id=requester_id,
                related_user_id=related_user_id,
                appointment_id=appointment_id,
                business_id=business_id,
                review_id=review_id,
                source_key=source_key,
            )
            TicketMessage.objects.create(ticket=ticket, author_id=None, body=body, is_internal=True)
            _event(ticket, "created", None, "", ticket.status)
    except IntegrityError:
        return None
    return ticket


def process_review_flags() -> int:
    """Turn unprocessed `review_flagged` outbox rows into tickets (exactly once per flag)."""
    created = 0
    with transaction.atomic():
        for flag in reviews.claim_flag_events():
            ticket = system_ticket(
                type=TicketType.FLAGGED_REVIEW,
                subject="A review was reported",
                body=f"Reported for: {flag.reason}",
                source_key=f"review_flag:{flag.flag_id}",
                requester_id=flag.reporter_id or None,
                business_id=flag.business_id,
                review_id=flag.review_id,
            )
            created += int(ticket is not None)
    return created

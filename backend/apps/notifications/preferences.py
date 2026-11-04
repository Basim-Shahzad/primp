"""Owner notification preferences: per event, SMS and email toggles (dashboard is always on)."""

from dataclasses import dataclass
from typing import Any

from django.db import transaction
from django.utils.translation import gettext_lazy as _

from apps.core.errors import validation_error

from .models import NotificationPreference

# event key -> (label, default sms, default email). The in-app copy is always delivered.
OWNER_EVENTS: dict[str, tuple[Any, bool, bool]] = {
    "new_booking": (_("New booking"), False, True),
    "booking_cancelled_by_customer": (_("Booking cancelled by customer"), False, True),
    "provider_booking_rescheduled": (_("Customer rescheduled"), False, True),
    "provider_reschedule_requested": (_("Reschedule request"), False, True),
    "new_review": (_("New review"), False, True),
    "pending_escalation": (_("Booking waiting for your answer"), True, False),
    "onboarding_status_changed": (_("Business verification updates"), True, False),
}


@dataclass(frozen=True)
class Preference:
    event: str
    label: str
    dashboard: bool
    sms: bool
    email: bool


def channels_for(user_id: int, key: str) -> tuple[bool, bool]:
    """(sms, email) wanted for an owner event, from stored choices over the defaults."""
    _label, sms, email = OWNER_EVENTS[key]
    row = NotificationPreference.objects.filter(user_id=user_id, event_key=key).first()
    if row is not None:
        return row.sms, row.email
    return sms, email


def list_preferences(user_id: int) -> list[Preference]:
    stored = {p.event_key: p for p in NotificationPreference.objects.filter(user_id=user_id)}
    rows = []
    for key, (label, sms, email) in OWNER_EVENTS.items():
        row = stored.get(key)
        rows.append(
            Preference(
                key,
                str(label),
                True,
                row.sms if row else sms,
                row.email if row else email,
            )
        )
    return rows


def update_preferences(
    user_id: int, updates: list[tuple[str, bool | None, bool | None]]
) -> list[Preference]:
    unknown = sorted({event for event, _s, _e in updates if event not in OWNER_EVENTS})
    if unknown:
        raise validation_error({"preferences": [f"Unknown event: {e}" for e in unknown]})
    with transaction.atomic():
        for event, sms, email in updates:
            current_sms, current_email = channels_for(user_id, event)
            NotificationPreference.objects.update_or_create(
                user_id=user_id,
                event_key=event,
                defaults={
                    "sms": current_sms if sms is None else sms,
                    "email": current_email if email is None else email,
                },
            )
    return list_preferences(user_id)

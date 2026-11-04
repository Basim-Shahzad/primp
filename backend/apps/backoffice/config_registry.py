"""The typed registry of platform settings: key, Pydantic schema, code default, description.

Defaults come from Django settings (so today's environment variables keep working); a row in
`PlatformSetting` overrides them at runtime without a restart.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Any, Literal

from django.conf import settings
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReminderOffsets(Strict):
    reminder_24h: int = Field(ge=3600, le=7 * 86400)
    reminder_2h: int = Field(ge=300, le=48 * 3600)

    @model_validator(mode="after")
    def _ordered(self) -> "ReminderOffsets":
        if self.reminder_24h <= self.reminder_2h:
            raise ValueError("reminder_24h must be longer than reminder_2h")
        return self


class OtpLimits(Strict):
    ttl_seconds: int = Field(ge=60, le=3600)
    resend_cooldown_seconds: int = Field(ge=10, le=600)
    max_verify_attempts: int = Field(ge=1, le=10)
    max_requests_per_hour: int = Field(ge=1, le=20)
    max_requests_per_ip_hour: int = Field(ge=1, le=500)

    @model_validator(mode="after")
    def _cooldown_shorter_than_ttl(self) -> "OtpLimits":
        if self.resend_cooldown_seconds >= self.ttl_seconds:
            raise ValueError("resend_cooldown_seconds must be shorter than ttl_seconds")
        return self


Priority = Literal["low", "normal", "high", "urgent"]
TicketTypeName = Literal[
    "booking_dispute",
    "refund_request",
    "flagged_review",
    "provider_complaint",
    "account_issue",
    "failed_notification",
    "other",
]


@dataclass(frozen=True)
class SettingDef:
    key: str
    description: str
    adapter: TypeAdapter[Any]
    default: Callable[[], Any]

    def schema(self) -> dict[str, Any]:
        return dict(self.adapter.json_schema())


def _int(low: int, high: int) -> TypeAdapter[int]:
    return TypeAdapter(Annotated[int, Field(ge=low, le=high, strict=True)])


def _define(
    key: str, description: str, adapter: TypeAdapter[Any], default: Callable[[], Any]
) -> SettingDef:
    return SettingDef(key, description, adapter, default)


PRIORITY_DEFAULTS = {
    "booking_dispute": "high",
    "refund_request": "high",
    "flagged_review": "normal",
    "provider_complaint": "normal",
    "account_issue": "normal",
    "failed_notification": "low",
    "other": "low",
}

_DEFS = [
    _define(
        "reminder_offsets",
        "Seconds before an appointment at which the 24 h and 2 h reminders are sent.",
        TypeAdapter(ReminderOffsets),
        lambda: dict(settings.REMINDER_OFFSETS),
    ),
    _define(
        "default_cancellation_notice_hours",
        "Cancellation notice a new business starts with (hours).",
        _int(0, 72),
        lambda: 24,
    ),
    _define(
        "commission_rate_bps",
        "Platform commission in basis points (100 = 1%). Informational until online payments.",
        _int(0, 5000),
        lambda: 0,
    ),
    _define(
        "slot_hold_seconds",
        "How long a slot hold lasts while the customer checks out.",
        _int(60, 1800),
        lambda: int(settings.BOOKING_HOLD_SECONDS),
    ),
    _define(
        "pending_escalation_minutes",
        "Alert the owner when a manual-accept booking is unanswered for this long.",
        _int(5, 1440),
        lambda: int(settings.PENDING_ESCALATION_MINUTES),
    ),
    _define(
        "pending_auto_cancel_minutes",
        "Auto-cancel an unanswered manual-accept booking after this long (or at its start).",
        _int(30, 4320),
        lambda: int(settings.PENDING_AUTO_CANCEL_MINUTES),
    ),
    _define(
        "otp_limits",
        "One-time-password lifetime, resend cooldown and request/attempt limits.",
        TypeAdapter(OtpLimits),
        lambda: {
            "ttl_seconds": 300,
            "resend_cooldown_seconds": 60,
            "max_verify_attempts": 5,
            "max_requests_per_hour": 5,
            "max_requests_per_ip_hour": 20,
        },
    ),
    _define(
        "points_expiry_months",
        "Months until earned points expire (a month is 30.4375 days).",
        _int(1, 60),
        lambda: 12,
    ),
    _define(
        "points_adjust_daily_limit",
        "Total absolute points one admin may adjust per day.",
        _int(1, 1_000_000),
        lambda: int(settings.POINTS_ADJUST_DAILY_LIMIT),
    ),
    _define(
        "points_adjust_max",
        "Largest single manual points adjustment (absolute value).",
        _int(1, 100_000),
        lambda: int(settings.POINTS_ADJUST_MAX),
    ),
    _define(
        "auto_ticket_priorities",
        "Default priority per support-ticket type (types not listed keep their built-in default).",
        TypeAdapter(dict[TicketTypeName, Priority]),
        lambda: {**PRIORITY_DEFAULTS, **dict(getattr(settings, "TICKET_PRIORITIES", {}))},
    ),
    _define(
        "auto_ticket_late_cancellations",
        "Open a support ticket for every late customer cancellation.",
        TypeAdapter(bool, config=ConfigDict(strict=True)),
        lambda: bool(settings.AUTO_TICKET_LATE_CANCELLATIONS),
    ),
]

REGISTRY: dict[str, SettingDef] = {d.key: d for d in _DEFS}


class UnknownSetting(KeyError):
    pass


def definition(key: str) -> SettingDef:
    try:
        return REGISTRY[key]
    except KeyError:
        raise UnknownSetting(key) from None


def cross_check(key: str, value: Any, read: Callable[[str], Any]) -> str | None:
    """Rules that span two keys. Returns a message when the new value breaks one."""
    if key == "pending_escalation_minutes" and value >= read("pending_auto_cancel_minutes"):
        return "must be shorter than pending_auto_cancel_minutes"
    if key == "pending_auto_cancel_minutes" and value <= read("pending_escalation_minutes"):
        return "must be longer than pending_escalation_minutes"
    if key == "points_adjust_max" and value > read("points_adjust_daily_limit"):
        return "must not exceed points_adjust_daily_limit"
    if key == "points_adjust_daily_limit" and value < read("points_adjust_max"):
        return "must not be below points_adjust_max"
    return None

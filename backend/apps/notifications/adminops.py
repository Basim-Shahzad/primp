"""Notification maintenance for the backoffice."""

from .models import Notification


def scrub_recipient(user_id: int) -> int:
    """Blank stored phone/email recipients and payload names after account anonymisation."""
    count = 0
    for n in Notification.objects.filter(user_id=user_id):
        n.recipient = ""
        n.payload = {
            k: ("" if k in ("customer_name",) else v) for k, v in (n.payload or {}).items()
        }
        n.save(update_fields=["recipient", "payload"])
        count += 1
    return count


def failure_summary(notification_id: int) -> dict[str, object]:
    n = Notification.objects.filter(pk=notification_id).first()
    if n is None:
        return {}
    return {
        "user_id": n.user_id,
        "channel": n.channel,
        "template_key": n.template_key,
        "attempts": n.attempts,
        "error": n.last_error[:300],
    }


# ---- template editing -------------------------------------------------------------------

from django.db import transaction  # noqa: E402

from .models import NotificationTemplate  # noqa: E402
from .seed_data import template_rows  # noqa: E402
from .templating import placeholders, render_text  # noqa: E402

TEMPLATE_FIELDS = ("subject", "body", "is_active")


class TemplateError(Exception):
    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field
        self.message = message


class StaleTemplate(Exception):
    pass


def allowed_placeholders(key: str) -> set[str]:
    """Placeholders the code supplies for a template key (taken from the shipped defaults)."""
    names: set[str] = set()
    for row in template_rows():
        if row["key"] == key:
            names |= placeholders(row["subject"]) | placeholders(row["body"])
    return names


def sample_payload(key: str) -> dict[str, str]:
    return {name: f"<{name}>" for name in allowed_placeholders(key)}


def template_snapshot(t: NotificationTemplate) -> dict[str, object]:
    return {
        "id": t.pk,
        "key": t.key,
        "channel": t.channel,
        "language": t.language,
        "subject": t.subject,
        "body": t.body,
        "is_active": t.is_active,
        "version": t.version,
        "placeholders": sorted(allowed_placeholders(t.key)),
    }


def list_templates(key: str | None = None) -> list[NotificationTemplate]:
    qs = NotificationTemplate.objects.order_by("key", "channel", "language")
    return list(qs.filter(key=key) if key else qs)


def find_template(template_id: int) -> NotificationTemplate | None:
    return NotificationTemplate.objects.filter(pk=template_id).first()


def validate_text(key: str, subject: str, body: str) -> None:
    allowed = allowed_placeholders(key)
    for field, text in (("subject", subject), ("body", body)):
        try:
            unknown = placeholders(text) - allowed
        except ValueError:
            raise TemplateError(field, "Unbalanced braces in the template.") from None
        if unknown:
            raise TemplateError(field, f"Unknown placeholders: {', '.join(sorted(unknown))}.")


def preview(key: str, subject: str, body: str) -> dict[str, str]:
    validate_text(key, subject, body)
    payload = sample_payload(key)
    return {"subject": render_text(subject, payload), "body": render_text(body, payload)}


def update_template(
    template_id: int, version: int, changes: dict[str, object]
) -> tuple[dict[str, object], dict[str, object]] | None:
    """Apply `changes`; returns (before, after) or None when the template does not exist."""
    with transaction.atomic():
        t = NotificationTemplate.objects.select_for_update().filter(pk=template_id).first()
        if t is None:
            return None
        if t.version != version:
            raise StaleTemplate
        before = template_snapshot(t)
        subject = str(changes.get("subject", t.subject))
        body = str(changes.get("body", t.body))
        validate_text(t.key, subject, body)
        if not body.strip():
            raise TemplateError("body", "The body cannot be empty.")
        t.subject, t.body = subject, body
        if "is_active" in changes:
            t.is_active = bool(changes["is_active"])
        t.version += 1
        t.save(update_fields=[*TEMPLATE_FIELDS, "version"])
        return before, template_snapshot(t)

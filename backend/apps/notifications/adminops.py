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

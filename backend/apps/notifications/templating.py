from string import Formatter
from typing import Any

from .models import NotificationTemplate


class MissingPlaceholder(Exception):
    """A template needs a value the payload does not provide."""


class TemplateNotFound(Exception):
    pass


def placeholders(text: str) -> set[str]:
    return {name for _lit, name, _fmt, _conv in Formatter().parse(text) if name}


def render_text(text: str, payload: dict[str, Any]) -> str:
    missing = sorted(placeholders(text) - set(payload))
    if missing:
        raise MissingPlaceholder(f"missing placeholders: {', '.join(missing)}")
    return text.format_map(payload)


def get_template(key: str, channel: str, language: str = "en") -> NotificationTemplate:
    template = (
        NotificationTemplate.objects.filter(
            key=key, channel=channel, language=language, is_active=True
        ).first()
        or NotificationTemplate.objects.filter(
            key=key, channel=channel, language="en", is_active=True
        ).first()
    )
    if template is None:
        raise TemplateNotFound(f"{key}/{channel}/{language}")
    return template


def render(
    key: str, channel: str, payload: dict[str, Any], language: str = "en"
) -> tuple[str, str]:
    """Return (subject, body). Raises MissingPlaceholder / TemplateNotFound."""
    template = get_template(key, channel, language)
    return render_text(template.subject, payload), render_text(template.body, payload)


def active_channels(key: str, language: str = "en") -> set[str]:
    return set(
        NotificationTemplate.objects.filter(key=key, language=language, is_active=True).values_list(
            "channel", flat=True
        )
    ) or set(
        NotificationTemplate.objects.filter(key=key, language="en", is_active=True).values_list(
            "channel", flat=True
        )
    )

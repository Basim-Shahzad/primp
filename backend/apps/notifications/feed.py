"""The owner's in-app notification feed."""

from django.db.models import QuerySet
from django.utils import timezone

from apps.core.pagination import CursorPage, paginate_cursor

from . import preferences
from .models import Channel, Notification, NotificationTemplate
from .templating import MissingPlaceholder, TemplateNotFound, render, render_text

ORDERING = ("-created_at", "-id")


def _owner_feed(user_id: int) -> QuerySet[Notification]:
    return Notification.objects.filter(
        user_id=user_id,
        channel=Channel.IN_APP,
        template_key__in=list(preferences.OWNER_EVENTS),
    )


def feed(
    user_id: int, *, cursor: str | None, limit: int, unread_only: bool = False
) -> tuple[CursorPage[Notification], int]:
    qs = _owner_feed(user_id)
    unread = qs.filter(read_at__isnull=True).count()
    if unread_only:
        qs = qs.filter(read_at__isnull=True)
    return paginate_cursor(qs, ordering=ORDERING, cursor=cursor, limit=limit), unread


def mark_read(user_id: int, *, ids: list[int] | None, all_: bool) -> int:
    """Mark the owner's own unread items read. Foreign or unknown ids are silently ignored."""
    qs = _owner_feed(user_id).filter(read_at__isnull=True)
    if not all_:
        qs = qs.filter(pk__in=ids or [])
    return int(qs.update(read_at=timezone.now()))


def render_items(items: list[Notification]) -> list[tuple[str, str]]:
    """(title, body) for each item with one template query for the whole page."""
    keys = {n.template_key for n in items}
    templates = {
        (t.key, t.language): t
        for t in NotificationTemplate.objects.filter(
            key__in=keys, channel=Channel.IN_APP, is_active=True
        )
    }
    out: list[tuple[str, str]] = []
    for n in items:
        template = templates.get((n.template_key, n.language)) or templates.get(
            (n.template_key, "en")
        )
        try:
            if template is None:
                raise TemplateNotFound(n.template_key)
            out.append(
                (render_text(template.subject, n.payload), render_text(template.body, n.payload))
            )
        except (MissingPlaceholder, TemplateNotFound):
            out.append((n.template_key.replace("_", " ").capitalize(), ""))
    return out


def title_and_body(notification: Notification) -> tuple[str, str]:
    try:
        return render(
            notification.template_key, Channel.IN_APP, notification.payload, notification.language
        )
    except (MissingPlaceholder, TemplateNotFound):
        return notification.template_key.replace("_", " ").capitalize(), ""


def failed_for_admin() -> QuerySet[Notification]:
    """Notifications that failed for good (surfaced in the Plan 6.3 admin)."""
    return Notification.objects.filter(admin_flagged=True).order_by("-created_at", "-id")

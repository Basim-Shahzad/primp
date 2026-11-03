from typing import Any

from django.apps import AppConfig


class NotificationsConfig(AppConfig):
    name = "apps.notifications"
    label = "notifications"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from . import receivers

        receivers.connect()
        post_migrate.connect(_ensure_templates, sender=self)


def _ensure_templates(sender: Any, **kwargs: Any) -> None:
    """Re-create missing default templates (a test flush wipes migration-seeded rows)."""
    from .models import NotificationTemplate
    from .seed_data import template_rows

    NotificationTemplate.objects.bulk_create(
        [NotificationTemplate(**row) for row in template_rows()], ignore_conflicts=True
    )

from typing import Any

from django.apps import AppConfig


class BackofficeConfig(AppConfig):
    name = "apps.backoffice"
    label = "backoffice"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from . import receivers

        receivers.connect()
        post_migrate.connect(_sync_groups, sender=self)


def _sync_groups(sender: Any, **kwargs: Any) -> None:
    """Re-create the permission groups (a test flush wipes migration-seeded rows)."""
    from django.apps import apps

    from .permissions import sync_groups

    sync_groups(apps)

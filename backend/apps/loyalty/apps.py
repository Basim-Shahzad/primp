from typing import Any

from django.apps import AppConfig


class LoyaltyConfig(AppConfig):
    name = "apps.loyalty"
    label = "loyalty"

    def ready(self) -> None:
        from django.db.models.signals import post_migrate

        from . import receivers

        receivers.connect()
        post_migrate.connect(_ensure_rules, sender=self)


def _ensure_rules(sender: Any, **kwargs: Any) -> None:
    """Re-create missing default rules (a test flush wipes migration-seeded rows)."""
    from .models import PointRule
    from .seed_data import RULES

    for action, kind, points, unit, description in RULES:
        if not PointRule.objects.filter(action=action).exists():
            PointRule.objects.create(
                action=action, kind=kind, points=points, unit_amount=unit, description=description
            )

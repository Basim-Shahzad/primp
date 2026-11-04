"""Seed editable starter-menu templates (idempotent). Creates missing top-level categories."""

from typing import Any

from django.core.management.base import BaseCommand

from apps.businesses.models import StarterMenuItem
from apps.catalog.models import Category

MENU: dict[str, tuple[str, list[tuple[str, str, int, int]]]] = {
    "hair": (
        "Hair",
        [
            ("Haircut", "Wash, cut and blow-dry", 1500, 45),
            ("Hair colour", "Full colour with finish", 6000, 120),
            ("Hair spa", "Deep conditioning treatment", 3500, 60),
            ("Blow-dry", "Wash and styling", 1200, 30),
        ],
    ),
    "nails": (
        "Nails",
        [
            ("Manicure", "Shape, cuticle care and polish", 1500, 45),
            ("Pedicure", "Soak, scrub and polish", 2000, 60),
            ("Gel polish", "Long-lasting gel colour", 2500, 60),
        ],
    ),
    "makeup": (
        "Makeup",
        [
            ("Party makeup", "Full face for events", 7000, 90),
            ("Bridal makeup", "Bridal look with trial notes", 35000, 180),
        ],
    ),
    "skin": (
        "Skin",
        [
            ("Facial", "Cleansing facial", 3000, 60),
            ("Threading", "Eyebrows and upper lip", 400, 15),
            ("Waxing", "Full arms and legs", 3500, 60),
        ],
    ),
    "barber": (
        "Barber",
        [
            ("Men's haircut", "Cut and style", 800, 30),
            ("Beard trim", "Shape and line-up", 500, 20),
            ("Shave", "Hot towel shave", 600, 25),
        ],
    ),
}


def seed_starter_menu() -> int:
    created = 0
    for slug, (name, items) in MENU.items():
        category, _ = Category.objects.get_or_create(slug=slug, defaults={"name": name})
        for order, (item, desc, price, minutes) in enumerate(items):
            _obj, was_created = StarterMenuItem.objects.get_or_create(
                category=category,
                name=item,
                defaults={
                    "description": desc,
                    "price": price,
                    "duration_minutes": minutes,
                    "sort_order": order,
                },
            )
            created += int(was_created)
    return created


class Command(BaseCommand):
    help = "Create starter service templates for the standard beauty categories."

    def handle(self, *args: Any, **options: Any) -> None:
        self.stdout.write(f"Starter menu items created: {seed_starter_menu()}")

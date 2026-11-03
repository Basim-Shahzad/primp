# ruff: noqa: S311, E501
"""Idempotent demo data: cities, areas, categories and businesses around Karachi.

Usage: python manage.py seed_demo [--count 30]. Re-running never duplicates rows.
"""

import random
from datetime import time
from typing import Any

from django.contrib.auth.hashers import make_password
from django.contrib.gis.geos import Point
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils.text import slugify

from apps.accounts.models import AuthProvider, User
from apps.accounts.roles import Role
from apps.businesses.models import (
    Business,
    GenderType,
    OpeningHours,
    Service,
    Staff,
    StaffService,
    VerificationStatus,
)
from apps.catalog.models import Category
from apps.geo.models import Area, City, CityVisibility

CITIES = [
    ("Karachi", "karachi", (67.0011, 24.8607), CityVisibility.PUBLIC),
    ("Lahore", "lahore", (74.3587, 31.5204), CityVisibility.PREVIEW),
    ("Islamabad", "islamabad", (73.0479, 33.6844), CityVisibility.HIDDEN),
]

# (name, lat, lng) - Karachi
KARACHI_AREAS = [
    ("Clifton", 24.8138, 67.0300),
    ("DHA Phase 5", 24.8000, 67.0600),
    ("Gulshan-e-Iqbal", 24.9215, 67.0920),
    ("PECHS", 24.8680, 67.0500),
    ("Bahadurabad", 24.8810, 67.0680),
    ("North Nazimabad", 24.9370, 67.0400),
    ("Saddar", 24.8607, 67.0100),
    ("Gulistan-e-Johar", 24.9180, 67.1300),
]
LAHORE_AREAS = [("Gulberg", 31.5204, 74.3587), ("DHA Phase 6", 31.4697, 74.4137)]

# top-level slug -> (name, gender, [(sub slug, sub name)], services[(name, price, minutes, sub slug)])
TAXONOMY: dict[str, dict[str, Any]] = {
    "hair": {
        "name": "Hair",
        "gender": GenderType.WOMEN,
        "subs": [
            ("haircut", "Haircut & Styling"),
            ("colour", "Hair Colour"),
            ("hair-treatment", "Hair Treatments"),
        ],
        "services": [
            ("Women's Haircut", 1500, 45, "haircut"),
            ("Blow Dry", 1200, 40, "haircut"),
            ("Hair Colour", 6000, 120, "colour"),
            ("Keratin Treatment", 15000, 150, "hair-treatment"),
        ],
    },
    "barber": {
        "name": "Barber",
        "gender": GenderType.MEN,
        "subs": [("beard", "Beard & Shave"), ("mens-haircut", "Men's Haircut")],
        "services": [
            ("Men's Haircut", 800, 30, "mens-haircut"),
            ("Beard Trim", 500, 20, "beard"),
            ("Hot Towel Shave", 700, 30, "beard"),
            ("Haircut + Beard", 1200, 45, "mens-haircut"),
        ],
    },
    "nails": {
        "name": "Nails",
        "gender": GenderType.WOMEN,
        "subs": [("manicure", "Manicure"), ("pedicure", "Pedicure")],
        "services": [
            ("Classic Manicure", 1500, 45, "manicure"),
            ("Pedicure", 2000, 60, "pedicure"),
            ("Gel Polish", 2500, 60, "manicure"),
        ],
    },
    "skin": {
        "name": "Skin & Facial",
        "gender": GenderType.UNISEX,
        "subs": [("facial", "Facials"), ("threading", "Threading & Waxing")],
        "services": [
            ("Basic Facial", 3000, 60, "facial"),
            ("Deep Cleansing Facial", 4500, 75, "facial"),
            ("Eyebrow Threading", 400, 15, "threading"),
        ],
    },
    "spa": {
        "name": "Spa & Massage",
        "gender": GenderType.UNISEX,
        "subs": [("massage", "Massage")],
        "services": [
            ("Swedish Massage", 6000, 60, "massage"),
            ("Aromatherapy Massage", 7500, 75, "massage"),
            ("Head & Shoulder Massage", 3000, 30, "massage"),
        ],
    },
    "bridal": {
        "name": "Bridal & Makeup",
        "gender": GenderType.WOMEN,
        "subs": [("makeup", "Makeup")],
        "services": [
            ("Party Makeup", 8000, 90, "makeup"),
            ("Bridal Makeup", 35000, 180, "makeup"),
        ],
    },
}

PREFIXES = [
    "Glow",
    "Velvet",
    "Royal",
    "Luxe",
    "Urban",
    "Aura",
    "Elite",
    "Pearl",
    "Studio",
    "Bliss",
    "Noor",
    "Zara",
]
SUFFIXES = ["Salon", "Studio", "Lounge", "Cuts", "Spa", "Beauty Bar", "Parlour", "Grooming"]
STAFF_NAMES = [
    "Ayesha",
    "Bilal",
    "Sana",
    "Hamza",
    "Fatima",
    "Usman",
    "Hira",
    "Zain",
    "Maria",
    "Ali",
    "Nida",
    "Kamran",
]


class Command(BaseCommand):
    help = "Create idempotent demo data around Karachi"

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument("--count", type=int, default=30, help="Number of businesses")

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        count: int = options["count"]
        rng = random.Random(42)
        cities = self._cities()
        areas = self._areas(cities)
        subs = self._categories()
        created = self._businesses(count, rng, cities["karachi"], areas, subs)
        self.stdout.write(
            self.style.SUCCESS(f"seed_demo: {created} new businesses ({count} requested)")
        )

    def _cities(self) -> dict[str, City]:
        result = {}
        for name, slug, (lng, lat), visibility in CITIES:
            city, _ = City.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": name,
                    "centre": Point(lng, lat, srid=4326),
                    "visibility": visibility,
                    "is_active": True,
                },
            )
            result[slug] = city
        return result

    def _areas(self, cities: dict[str, City]) -> list[Area]:
        out: list[Area] = []
        for city_slug, rows in (("karachi", KARACHI_AREAS), ("lahore", LAHORE_AREAS)):
            for name, lat, lng in rows:
                area, _ = Area.objects.update_or_create(
                    city=cities[city_slug],
                    slug=slugify(name),
                    defaults={
                        "name": name,
                        "centre": Point(lng, lat, srid=4326),
                        "is_pilot": city_slug == "karachi",
                    },
                )
                if city_slug == "karachi":
                    out.append(area)
        return out

    def _categories(self) -> dict[str, dict[str, Category]]:
        subs: dict[str, dict[str, Category]] = {}
        for order, (slug, spec) in enumerate(TAXONOMY.items()):
            top, _ = Category.objects.update_or_create(
                slug=slug,
                defaults={
                    "name": spec["name"],
                    "sort_order": order,
                    "parent": None,
                    "is_active": True,
                },
            )
            subs[slug] = {"__top__": top}
            for sub_order, (sub_slug, sub_name) in enumerate(spec["subs"]):
                sub, _ = Category.objects.update_or_create(
                    slug=sub_slug,
                    defaults={
                        "name": sub_name,
                        "sort_order": sub_order,
                        "parent": top,
                        "is_active": True,
                    },
                )
                subs[slug][sub_slug] = sub
        return subs

    def _businesses(
        self,
        count: int,
        rng: random.Random,
        city: City,
        areas: list[Area],
        subs: dict[str, dict[str, Category]],
    ) -> int:
        existing = set(Business.objects.filter(city=city).values_list("slug", flat=True))
        tax_keys = list(TAXONOMY)
        created = 0
        for i in range(count):
            prefix = PREFIXES[i % len(PREFIXES)]
            suffix = SUFFIXES[(i // len(PREFIXES)) % len(SUFFIXES)]
            round_no = i // (len(PREFIXES) * len(SUFFIXES))
            name = f"{prefix} {suffix}" + (f" {round_no + 1}" if round_no else "")
            slug = slugify(f"{name}-{i}")
            if slug in existing:
                continue
            top_slug = tax_keys[i % len(tax_keys)]
            spec = TAXONOMY[top_slug]
            area = areas[i % len(areas)]
            owner = self._owner(i)
            point = Point(
                area.centre.x + rng.uniform(-0.01, 0.01),
                area.centre.y + rng.uniform(-0.01, 0.01),
                srid=4326,
            )
            business = Business.objects.create(
                owner=owner,
                name=name,
                slug=slug,
                category=subs[top_slug]["__top__"],
                description=f"{name} offers {spec['name'].lower()} services in {area.name}.",
                address=f"Plot {rng.randint(1, 99)}, {area.name}, Karachi",
                location=point,
                city=city,
                area=area,
                phone=f"+92300{i:07d}"[:13],
                whatsapp=f"+92300{i:07d}"[:13],
                gender_type=spec["gender"],
                verification_status=VerificationStatus.VERIFIED
                if i % 10 < 7
                else VerificationStatus.PENDING,
                is_listed=i % 15 != 14,
                staff_level_booking=i % 2 == 0,
                parallel_capacity=1 if i % 2 == 0 else 2 + i % 2,
                slot_interval_minutes=(15, 30, 60)[i % 3],
                lead_time_minutes=60,
                rating_avg=round(rng.uniform(3.5, 5.0), 2),
                rating_count=rng.randint(0, 200),
            )
            self._children(business, spec, subs[top_slug], rng, i)
            existing.add(slug)
            created += 1
        return created

    def _owner(self, i: int) -> User:
        email = f"owner{i}@demo.primp.pk"
        user = User.objects.filter(email=email).first()
        if user is None:
            user = User.objects.create(
                email=email,
                role=Role.BUSINESS_OWNER,
                auth_provider=AuthProvider.PASSWORD,
                is_email_verified=True,
                password=make_password(None),
            )
        return user

    def _children(
        self,
        business: Business,
        spec: dict[str, Any],
        sub_map: dict[str, Category],
        rng: random.Random,
        i: int,
    ) -> None:
        staff = Staff.objects.bulk_create(
            [
                Staff(business=business, name=STAFF_NAMES[(i + k) % len(STAFF_NAMES)])
                for k in range(2 + i % 2)
            ]
        )
        services = Service.objects.bulk_create(
            [
                Service(
                    business=business,
                    category=sub_map[sub],
                    name=name,
                    price=price + 100 * (i % 4),
                    duration_minutes=minutes,
                    is_from_price=(k % 3 == 0),
                    sort_order=k,
                )
                for k, (name, price, minutes, sub) in enumerate(spec["services"])
            ]
        )
        StaffService.objects.bulk_create(
            [
                StaffService(staff=member, service=service)
                for si, member in enumerate(staff)
                for ki, service in enumerate(services)
                if (si + ki) % 3 != 0 or si == 0
            ]
        )
        hours: list[OpeningHours] = []
        for weekday in range(7):
            if weekday == 6 and i % 2:
                continue  # closed Sundays for half the businesses
            if weekday == 4:  # Friday split shift around Jummah
                hours.append(
                    OpeningHours(
                        business=business, weekday=4, opens_at=time(10), closes_at=time(12, 30)
                    )
                )
                hours.append(
                    OpeningHours(
                        business=business, weekday=4, opens_at=time(14, 30), closes_at=time(20)
                    )
                )
            else:
                hours.append(
                    OpeningHours(
                        business=business, weekday=weekday, opens_at=time(10), closes_at=time(20)
                    )
                )
        OpeningHours.objects.bulk_create(hours)

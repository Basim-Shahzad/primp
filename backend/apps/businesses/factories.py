import random
from datetime import time

import factory
from django.contrib.gis.geos import Point
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.accounts.factories import UserFactory
from apps.accounts.roles import Role
from apps.catalog.factories import CategoryFactory
from apps.geo.factories import AreaFactory

from .models import (
    Business,
    BusinessPhoto,
    Closure,
    OpeningHours,
    Service,
    Staff,
    StaffService,
    VerificationStatus,
)


def random_karachi_point(rng: random.Random | None = None) -> Point:
    r = rng or random
    return Point(67.0 + r.uniform(-0.08, 0.12), 24.86 + r.uniform(-0.06, 0.08), srid=4326)


class BusinessFactory(DjangoModelFactory):
    class Meta:
        model = Business

    owner = factory.SubFactory(UserFactory, role=Role.BUSINESS_OWNER)
    name = factory.Sequence(lambda n: f"Salon {n}")
    slug = factory.Sequence(lambda n: f"salon-{n}")
    category = factory.SubFactory(CategoryFactory)
    area = factory.SubFactory(AreaFactory)
    city = factory.SelfAttribute("area.city")
    location = factory.LazyFunction(lambda: Point(67.0011, 24.8607, srid=4326))
    verification_status = VerificationStatus.VERIFIED
    is_listed = True
    lead_time_minutes = 0
    max_advance_days = 60


class BusinessPhotoFactory(DjangoModelFactory):
    class Meta:
        model = BusinessPhoto

    business = factory.SubFactory(BusinessFactory)
    image = factory.Sequence(lambda n: f"businesses/photo-{n}.jpg")


class StaffFactory(DjangoModelFactory):
    class Meta:
        model = Staff

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Staff {n}")


class ServiceFactory(DjangoModelFactory):
    class Meta:
        model = Service

    business = factory.SubFactory(BusinessFactory)
    name = factory.Sequence(lambda n: f"Service {n}")
    price = 1000
    duration_minutes = 60


class StaffServiceFactory(DjangoModelFactory):
    class Meta:
        model = StaffService

    staff = factory.SubFactory(StaffFactory)
    service = factory.SubFactory(ServiceFactory, business=factory.SelfAttribute("..staff.business"))


class OpeningHoursFactory(DjangoModelFactory):
    class Meta:
        model = OpeningHours

    business = factory.SubFactory(BusinessFactory)
    weekday = 0
    opens_at = time(10, 0)
    closes_at = time(20, 0)


def open_all_week(
    business: Business, opens: time = time(10, 0), closes: time = time(20, 0)
) -> None:
    for weekday in range(7):
        OpeningHoursFactory(business=business, weekday=weekday, opens_at=opens, closes_at=closes)


class ClosureFactory(DjangoModelFactory):
    class Meta:
        model = Closure

    business = factory.SubFactory(BusinessFactory)
    starts_at = factory.LazyFunction(timezone.now)
    ends_at = factory.LazyFunction(
        lambda: timezone.now() + __import__("datetime").timedelta(hours=1)
    )

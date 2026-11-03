from datetime import timedelta

import factory
from django.utils import timezone
from factory.django import DjangoModelFactory

from apps.accounts.factories import UserFactory
from apps.businesses.factories import BusinessFactory

from .models import Appointment, AppointmentStatus


class AppointmentFactory(DjangoModelFactory):
    class Meta:
        model = Appointment

    customer = factory.SubFactory(UserFactory)
    business = factory.SubFactory(BusinessFactory)
    status = AppointmentStatus.CONFIRMED
    scheduled_start = factory.LazyFunction(lambda: timezone.now() + timedelta(days=1))
    scheduled_end = factory.LazyAttribute(lambda o: o.scheduled_start + timedelta(hours=1))
    total_price = 1000

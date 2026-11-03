import factory
from factory.django import DjangoModelFactory

from apps.accounts.factories import UserFactory
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import AppointmentStatus

from .models import Review, ReviewFlag, ReviewReply


class ReviewFactory(DjangoModelFactory):
    class Meta:
        model = Review

    appointment = factory.SubFactory(AppointmentFactory, status=AppointmentStatus.COMPLETED)
    customer = factory.SelfAttribute("appointment.customer")
    business = factory.SelfAttribute("appointment.business")
    rating = 5
    comment = "Great service"


class ReviewReplyFactory(DjangoModelFactory):
    class Meta:
        model = ReviewReply

    review = factory.SubFactory(ReviewFactory)
    author = factory.LazyAttribute(lambda o: o.review.business.owner)
    body = "Thank you!"


class ReviewFlagFactory(DjangoModelFactory):
    class Meta:
        model = ReviewFlag

    review = factory.SubFactory(ReviewFactory)
    reporter = factory.SubFactory(UserFactory)
    reason = "offensive"

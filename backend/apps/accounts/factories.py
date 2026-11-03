import factory
from factory.django import DjangoModelFactory

from .models import CustomerProfile, Role, User


class UserFactory(DjangoModelFactory):
    class Meta:
        model = User
        skip_postgeneration_save = True

    email = factory.Sequence(lambda n: f"user{n}@example.com")
    phone = factory.Sequence(lambda n: f"+92300{n:07d}")
    is_email_verified = True
    is_phone_verified = True
    role = Role.CUSTOMER

    @factory.post_generation
    def password(self, create: bool, extracted: str | None, **kwargs: object) -> None:
        self.set_password(extracted or "Str0ng-pass-phrase!")  # type: ignore[attr-defined]
        if create:
            self.save(update_fields=["password"])  # type: ignore[attr-defined]


class CustomerProfileFactory(DjangoModelFactory):
    class Meta:
        model = CustomerProfile

    user = factory.SubFactory(UserFactory)
    name = factory.Faker("name")


def make_customer(**kwargs: object) -> User:
    name = kwargs.pop("name", "Test Customer")
    user = UserFactory(**kwargs)
    CustomerProfileFactory(user=user, name=name)
    return user

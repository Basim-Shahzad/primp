import pytest
from django.db import IntegrityError, transaction

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import CustomerProfile, Role, User

pytestmark = pytest.mark.django_db


def test_email_or_phone_required() -> None:
    with pytest.raises(ValueError):
        User.objects.create_user()
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create(email=None, phone=None)


def test_phone_only_user_ok_and_str() -> None:
    u = User.objects.create_user(phone="+923001234567")
    assert u.email is None and str(u) == "+923001234567" and not u.has_usable_password()


def test_email_lowercased_and_unique() -> None:
    u = User.objects.create_user(email="Foo@Example.COM", password="x")
    assert u.email == "foo@example.com"
    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user(email="foo@example.com")


def test_duplicate_phone_rejected() -> None:
    UserFactory(phone="+923001111111")
    with pytest.raises(IntegrityError), transaction.atomic():
        UserFactory(phone="+923001111111")


def test_superuser_defaults() -> None:
    u = User.objects.create_superuser("root@example.com", "pw")
    assert u.is_superuser and u.is_staff and u.role == Role.SUPER_ADMIN and u.is_active


def test_referral_code_generated_unique() -> None:
    codes = {make_customer().profile.referral_code for _ in range(5)}
    assert len(codes) == 5


def test_referral_code_collision_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_customer().profile.referral_code
    seq = iter([first, first, "ZZZZZZZ2"])
    monkeypatch.setattr("apps.accounts.models.generate_referral_code", lambda: next(seq))
    assert make_customer().profile.referral_code == "ZZZZZZZ2"


def test_referral_code_exhausted(monkeypatch: pytest.MonkeyPatch) -> None:
    first = make_customer().profile.referral_code
    monkeypatch.setattr("apps.accounts.models.generate_referral_code", lambda: first)
    with pytest.raises(RuntimeError):
        CustomerProfile.objects.create(user=UserFactory())


def test_password_uses_argon2_in_base_hashers() -> None:
    from django.contrib.auth.hashers import Argon2PasswordHasher

    from config.settings import base

    assert base.PASSWORD_HASHERS[0].endswith("Argon2PasswordHasher")
    assert Argon2PasswordHasher().encode("pw", "saltsaltsalt").startswith("argon2$")

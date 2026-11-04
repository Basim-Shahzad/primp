from dataclasses import dataclass

from .models import CustomerProfile, User, Wishlist


def profile_of(user: User) -> CustomerProfile | None:
    return CustomerProfile.objects.filter(user=user).first()


def wishlisted_business_ids(user_id: int, business_ids: list[int]) -> set[int]:
    return set(
        Wishlist.objects.filter(user_id=user_id, business_id__in=business_ids).values_list(
            "business_id", flat=True
        )
    )


def display_names(user_ids: list[int]) -> dict[int, str]:
    """Public display name, e.g. "Ayesha K." (first name plus last initial)."""
    names: dict[int, str] = {}
    for user_id, name in CustomerProfile.objects.filter(user_id__in=user_ids).values_list(
        "user_id", "name"
    ):
        parts = name.split()
        if not parts:
            names[user_id] = "Customer"
        elif len(parts) == 1:
            names[user_id] = parts[0]
        else:
            names[user_id] = f"{parts[0]} {parts[-1][0]}."
    return names


@dataclass(frozen=True)
class Contact:
    user_id: int
    name: str
    phone: str | None
    email: str | None


def contact_for(user_id: int) -> Contact | None:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return None
    profile = CustomerProfile.objects.filter(user_id=user_id).values_list("name", flat=True).first()
    return Contact(user.pk, profile or "", user.phone, user.email)


def profile_is_complete(user_id: int) -> bool:
    """Name, photo and default city set, and a verified phone or email."""
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return False
    profile = CustomerProfile.objects.filter(user_id=user_id).first()
    if profile is None or not profile.name or not profile.photo or profile.default_city_id is None:
        return False
    return user.is_phone_verified or user.is_email_verified


def full_names(user_ids: list[int]) -> dict[int, str]:
    """Complete profile names (for the business owner's own bookings view)."""
    return {
        user_id: name or "Customer"
        for user_id, name in CustomerProfile.objects.filter(user_id__in=user_ids).values_list(
            "user_id", "name"
        )
    }


def verified_user_id_by_phone(phone: str) -> int | None:
    """Active account whose phone is verified and equals `phone` (E.164)."""
    return (
        User.objects.filter(phone=phone, is_phone_verified=True, status="active")
        .values_list("pk", flat=True)
        .first()
    )


def contacts_for(user_ids: list[int]) -> dict[int, Contact]:
    users = {u.pk: u for u in User.objects.filter(pk__in=user_ids)}
    names = dict(
        CustomerProfile.objects.filter(user_id__in=user_ids).values_list("user_id", "name")
    )
    return {uid: Contact(uid, names.get(uid) or "", u.phone, u.email) for uid, u in users.items()}


def no_show_count(user_id: int) -> int:
    value = CustomerProfile.objects.filter(user_id=user_id).values_list("no_show_count", flat=True)
    return int(value.first() or 0)

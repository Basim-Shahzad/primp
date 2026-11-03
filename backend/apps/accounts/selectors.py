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

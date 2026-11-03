from .models import CustomerProfile, User, Wishlist


def profile_of(user: User) -> CustomerProfile | None:
    return CustomerProfile.objects.filter(user=user).first()


def wishlisted_business_ids(user_id: int, business_ids: list[int]) -> set[int]:
    return set(
        Wishlist.objects.filter(user_id=user_id, business_id__in=business_ids).values_list(
            "business_id", flat=True
        )
    )

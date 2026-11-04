"""Business operations for the backoffice (it may not import this app's models)."""

from .invalidation import touch
from .models import Business, VerificationStatus


def hide_owner_listings(owner_id: int) -> dict[str, bool]:
    """Unlist every business of the owner. Returns {business_id: was_listed} to restore later."""
    previous: dict[str, bool] = {}
    for business in Business.objects.select_for_update().filter(owner_id=owner_id):
        previous[str(business.pk)] = business.is_listed
        if business.is_listed:
            business.is_listed = False
            business.save(update_fields=["is_listed", "updated_at"])
            touch(business.pk)
    return previous


def restore_owner_listings(owner_id: int, previous: dict[str, bool]) -> list[int]:
    """Put back exactly the listing state recorded at suspension (verified businesses only)."""
    restored: list[int] = []
    for business in Business.objects.select_for_update().filter(
        owner_id=owner_id, pk__in=[int(k) for k in previous]
    ):
        if previous.get(str(business.pk)) and business.verification_status == (
            VerificationStatus.VERIFIED
        ):
            business.is_listed = True
            business.save(update_fields=["is_listed", "updated_at"])
            touch(business.pk)
            restored.append(business.pk)
    return restored


def owns_businesses(owner_id: int) -> bool:
    return Business.objects.filter(owner_id=owner_id).exists()


def owner_business_ids(owner_id: int) -> list[int]:
    return list(Business.objects.filter(owner_id=owner_id).values_list("pk", flat=True))

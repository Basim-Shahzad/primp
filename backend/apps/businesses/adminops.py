"""Business operations for the backoffice (it may not import this app's models)."""

import uuid
from typing import Any

from django.conf import settings
from django.contrib.gis.measure import D
from django.contrib.postgres.search import TrigramSimilarity

from apps.core.errors import not_found

from . import files, page
from . import verification as verification_service
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


# ---------------------------------------------------------------- review support


def get_business(ref: str | uuid.UUID) -> Business:
    try:
        public_id = ref if isinstance(ref, uuid.UUID) else uuid.UUID(str(ref))
    except ValueError:
        raise not_found() from None
    business = (
        Business.objects.select_related("city", "area", "category", "owner")
        .filter(public_id=public_id)
        .first()
    )
    if business is None:
        raise not_found()
    return business


def get_business_by_id(business_id: int) -> Business:
    business = (
        Business.objects.select_related("city", "area", "category", "owner")
        .filter(pk=business_id)
        .first()
    )
    if business is None:
        raise not_found()
    return business


def preview(business: Business) -> dict[str, Any]:
    """Exactly what the public page would show (even when unlisted)."""
    return page.render_page(business)


def private_fields(business: Business) -> dict[str, Any]:
    owner = business.owner
    return {
        "status": business.verification_status,
        "status_message": business.status_message,
        "is_listed": business.is_listed,
        "owner_email": owner.email,
        "owner_phone": owner.phone,
        "address": business.address,
        "phone": business.phone,
        "whatsapp": business.whatsapp,
        "location": {"lat": business.location.y, "lng": business.location.x},
        "pending_review_changes": business.pending_review_changes,
        "onboarding_progress": business.onboarding_progress,
        "submissions": [
            {
                "number": s.number,
                "terms_version": s.terms_version,
                "accepted_at": s.accepted_at,
                "ip_address": s.ip_address,
                "after_rejection": s.is_resubmission_after_rejection,
            }
            for s in business.submissions.all()
        ],
    }


def documents_with_urls(business: Business) -> list[dict[str, Any]]:
    """Verification documents as signed, short-lived links (never raw storage paths)."""
    return [
        {
            "id": d.pk,
            "kind": d.kind,
            "status": d.status,
            "reviewer_note": d.reviewer_note,
            "content_type": d.content_type,
            "size": d.size,
            "url": files.signed_url(d.pk),
            "expires_in_seconds": min(int(settings.PRIVATE_URL_TTL_SECONDS), 300),
        }
        for d in business.documents.all()
    ]


def find_duplicates(business: Business) -> list[dict[str, Any]]:
    """Other businesses with the same phone, or very close by with a similar name."""
    found: dict[int, dict[str, Any]] = {}
    others = Business.objects.exclude(pk=business.pk)
    phones = [p for p in {business.phone, business.whatsapp} if p]
    if phones:
        for other in others.filter(phone__in=phones):
            found.setdefault(other.pk, _dup(other))["reasons"].append("same_phone")
    nearby = (
        others.filter(
            location__dwithin=(business.location, D(m=settings.DUPLICATE_DISTANCE_METERS))
        )
        .annotate(sim=TrigramSimilarity("name", business.name))
        .filter(sim__gte=settings.DUPLICATE_NAME_SIMILARITY)
    )
    for other in nearby:
        found.setdefault(other.pk, _dup(other))["reasons"].append("nearby_similar_name")
    return list(found.values())


def _dup(other: Business) -> dict[str, Any]:
    return {
        "business_id": other.public_id,
        "name": other.name,
        "status": other.verification_status,
        "reasons": [],
    }


def brief(business_id: int) -> dict[str, Any]:
    b = get_business_by_id(business_id)
    return {
        "id": b.public_id,
        "name": b.name,
        "city": b.city.name,
        "category": b.category.name,
        "status": b.verification_status,
        "owner_id": b.owner.public_id,
    }


# thin pass-throughs so backoffice calls one module of this app
mark_verified = verification_service.mark_verified
reject = verification_service.reject
request_more_info = verification_service.request_more_info
revoke_verification = verification_service.revoke_verification
approve_pending_changes = verification_service.approve_pending_changes
reject_pending_changes = verification_service.reject_pending_changes


def category_usage() -> dict[int, tuple[int, int]]:
    """{category_id: (services using it, businesses in it)} for the taxonomy screen."""
    from django.db.models import Count

    from .models import Service

    usage: dict[int, list[int]] = {}
    for cid, n in (
        Service.objects.exclude(category__isnull=True)
        .values_list("category_id")
        .annotate(n=Count("id"))
        .order_by()
    ):
        usage.setdefault(cid, [0, 0])[0] = n
    for cid, n in Business.objects.values_list("category_id").annotate(n=Count("id")).order_by():
        usage.setdefault(cid, [0, 0])[1] = n
    return {k: (v[0], v[1]) for k, v in usage.items()}


def count_listed(*, city_id: int | None = None, area_id: int | None = None) -> int:
    qs = Business.objects.filter(is_listed=True)
    if city_id is not None:
        qs = qs.filter(city_id=city_id)
    if area_id is not None:
        qs = qs.filter(area_id=area_id)
    return qs.count()


def notify_owners_unavailable(
    *, city_id: int | None = None, area_id: int | None = None, place: str
) -> int:
    """Tell the owners of listed businesses that their city/area was switched off."""
    from .onboarding import notify_status_changed

    qs = Business.objects.filter(is_listed=True)
    if city_id is not None:
        qs = qs.filter(city_id=city_id)
    if area_id is not None:
        qs = qs.filter(area_id=area_id)
    count = 0
    for business in qs:
        notify_status_changed(
            business,
            "hidden",
            f"{place} is temporarily not served on Primp, so your listing is not shown.",
        )
        touch(business.pk)
        count += 1
    return count


def business_for_requester(public_id: str, user_id: int) -> int | None:
    """Business pk if the user owns it or has a booking there."""
    import uuid

    from django.db.models import Q

    try:
        ref = uuid.UUID(str(public_id))
    except ValueError:
        return None
    row = (
        Business.objects.filter(public_id=ref)
        .filter(Q(owner_id=user_id) | Q(appointments__customer_id=user_id))
        .values_list("pk", flat=True)
        .first()
    )
    return int(row) if row is not None else None


def business_summary(business_id: int) -> dict[str, Any]:
    b = Business.objects.select_related("city").filter(pk=business_id).first()
    if b is None:
        return {}
    return {
        "id": b.public_id,
        "name": b.name,
        "city": b.city.name,
        "status": b.verification_status,
        "is_listed": b.is_listed,
    }

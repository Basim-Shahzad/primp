from datetime import time

import pytest
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.db import IntegrityError, transaction

from apps.businesses.factories import (
    BusinessFactory,
    BusinessPhotoFactory,
    ClosureFactory,
    OpeningHoursFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
)
from apps.businesses.models import (
    Business,
    Closure,
    OpeningHours,
    Service,
    Staff,
    StaffService,
    VerificationStatus,
)
from apps.businesses.selectors import get_public_business, public_businesses
from apps.businesses.services import link_staff_service
from apps.catalog.factories import CategoryFactory
from apps.catalog.models import Category
from apps.core.errors import AppError
from apps.geo.factories import AreaFactory, CityFactory
from apps.geo.models import Area

pytestmark = pytest.mark.django_db


def db_fails():  # type: ignore[no-untyped-def]
    return pytest.raises(IntegrityError)


# ---- services ----


def test_negative_price_rejected_by_db() -> None:
    with db_fails(), transaction.atomic():
        ServiceFactory(price=-1)


def test_zero_price_allowed() -> None:
    assert ServiceFactory(price=0).price == 0


@pytest.mark.parametrize("minutes", [0])
def test_zero_duration_rejected(minutes: int) -> None:
    with db_fails(), transaction.atomic():
        ServiceFactory(duration_minutes=minutes)


def test_negative_duration_rejected() -> None:
    with db_fails(), transaction.atomic():
        ServiceFactory(duration_minutes=-5)


# ---- staff services ----


def test_staff_service_same_business_ok() -> None:
    link = StaffServiceFactory()
    assert link.staff.business_id == link.service.business_id


def test_staff_service_across_businesses_rejected_in_model() -> None:
    staff, service = StaffFactory(), ServiceFactory()
    with pytest.raises(ValidationError):
        StaffService(staff=staff, service=service).save()
    assert StaffService.objects.count() == 0


def test_staff_service_across_businesses_rejected_in_service_layer() -> None:
    with pytest.raises(AppError) as exc:
        link_staff_service(StaffFactory(), ServiceFactory())
    assert exc.value.code == "business_mismatch"


def test_link_staff_service_is_idempotent() -> None:
    biz = BusinessFactory()
    staff, service = StaffFactory(business=biz), ServiceFactory(business=biz)
    a, b = link_staff_service(staff, service), link_staff_service(staff, service)
    assert a.pk == b.pk and StaffService.objects.count() == 1


def test_staff_service_unique_pair() -> None:
    link = StaffServiceFactory()
    with db_fails(), transaction.atomic():
        StaffService.objects.bulk_create([StaffService(staff=link.staff, service=link.service)])


# ---- business ----


def test_slug_unique_per_city_but_reusable_across_cities() -> None:
    a, b = AreaFactory(), AreaFactory()
    BusinessFactory(slug="glow", area=a)
    BusinessFactory(slug="glow", area=b)  # different city: fine
    with db_fails(), transaction.atomic():
        BusinessFactory(slug="glow", area=a)


@pytest.mark.parametrize("interval", [0, 10, 45, 120])
def test_invalid_slot_interval_rejected(interval: int) -> None:
    with db_fails(), transaction.atomic():
        BusinessFactory(slot_interval_minutes=interval)


@pytest.mark.parametrize("interval", [15, 30, 60])
def test_valid_slot_intervals(interval: int) -> None:
    assert BusinessFactory(slot_interval_minutes=interval).slot_interval_minutes == interval


def test_parallel_capacity_min_one() -> None:
    with db_fails(), transaction.atomic():
        BusinessFactory(parallel_capacity=0)


def test_rating_range_enforced() -> None:
    with db_fails(), transaction.atomic():
        BusinessFactory(rating_avg=5.5)


def test_defaults() -> None:
    b = BusinessFactory()
    assert b.timezone == "Asia/Karachi" and b.confirm_mode == "instant"
    assert b.cancellation_notice_hours == 24 and b.is_verified
    assert str(b.public_id)


def test_business_location_is_geography_point() -> None:
    b = Business.objects.get(pk=BusinessFactory().pk)
    assert b.location.srid == 4326 and round(b.location.x, 3) == 67.001


# ---- photos ----


def test_second_cover_photo_rejected() -> None:
    biz = BusinessFactory()
    BusinessPhotoFactory(business=biz, is_cover=True)
    BusinessPhotoFactory(business=biz, is_cover=False)
    with db_fails(), transaction.atomic():
        BusinessPhotoFactory(business=biz, is_cover=True)


def test_covers_allowed_for_different_businesses_and_cover_sorted_first() -> None:
    a, b = BusinessFactory(), BusinessFactory()
    BusinessPhotoFactory(business=a, is_cover=True)
    BusinessPhotoFactory(business=b, is_cover=True)
    BusinessPhotoFactory(business=a, sort_order=0)
    cover = BusinessPhotoFactory(business=a, sort_order=5, is_cover=False)
    assert next(iter(a.photos.all())).is_cover and cover in a.photos.all()


# ---- opening hours ----


def test_hours_close_must_be_after_open() -> None:
    with db_fails(), transaction.atomic():
        OpeningHoursFactory(opens_at=time(12), closes_at=time(12))
    with db_fails(), transaction.atomic():
        OpeningHoursFactory(opens_at=time(12), closes_at=time(11))


@pytest.mark.parametrize("weekday", [-1, 7])
def test_weekday_range(weekday: int) -> None:
    with pytest.raises((IntegrityError, Exception)), transaction.atomic():
        OpeningHoursFactory(weekday=weekday)


def test_overlapping_hours_rejected_by_db() -> None:
    biz = BusinessFactory()
    OpeningHoursFactory(business=biz, opens_at=time(10), closes_at=time(14))
    with db_fails(), transaction.atomic():
        OpeningHoursFactory(business=biz, opens_at=time(13), closes_at=time(18))


def test_contained_and_identical_hours_rejected() -> None:
    biz = BusinessFactory()
    OpeningHoursFactory(business=biz, opens_at=time(10), closes_at=time(20))
    with db_fails(), transaction.atomic():
        OpeningHoursFactory(business=biz, opens_at=time(12), closes_at=time(13))
    with db_fails(), transaction.atomic():
        OpeningHoursFactory(business=biz, opens_at=time(10), closes_at=time(20))


def test_split_shifts_and_touching_ranges_allowed() -> None:
    biz = BusinessFactory()
    OpeningHoursFactory(business=biz, opens_at=time(10), closes_at=time(13))
    OpeningHoursFactory(business=biz, opens_at=time(13), closes_at=time(18))  # touching
    OpeningHoursFactory(business=biz, opens_at=time(19), closes_at=time(22))
    assert biz.opening_hours.count() == 3


def test_same_hours_other_weekday_staff_or_business_allowed() -> None:
    biz, other = BusinessFactory(), BusinessFactory()
    staff = StaffFactory(business=biz)
    OpeningHoursFactory(business=biz, weekday=0)
    OpeningHoursFactory(business=biz, weekday=1)
    OpeningHoursFactory(business=biz, weekday=0, staff=staff)  # staff override
    OpeningHoursFactory(business=other, weekday=0)
    assert OpeningHours.objects.count() == 4


def test_overlap_per_staff_rejected() -> None:
    biz = BusinessFactory()
    staff = StaffFactory(business=biz)
    OpeningHoursFactory(business=biz, staff=staff, opens_at=time(10), closes_at=time(14))
    with db_fails(), transaction.atomic():
        OpeningHoursFactory(business=biz, staff=staff, opens_at=time(12), closes_at=time(16))


def test_hours_full_clean_overlap_and_staff_mismatch() -> None:
    biz = BusinessFactory()
    OpeningHoursFactory(business=biz, opens_at=time(10), closes_at=time(14))
    with pytest.raises(ValidationError):
        OpeningHours(business=biz, weekday=0, opens_at=time(13), closes_at=time(15)).clean()
    with pytest.raises(ValidationError):
        OpeningHours(business=biz, weekday=0, opens_at=time(15), closes_at=time(15)).clean()
    with pytest.raises(ValidationError):
        OpeningHours(
            business=biz, staff=StaffFactory(), weekday=2, opens_at=time(9), closes_at=time(10)
        ).clean()
    OpeningHours(business=biz, weekday=0, opens_at=time(14), closes_at=time(16)).clean()  # ok


# ---- closures ----


def test_closure_must_end_after_start() -> None:
    from django.utils import timezone

    now = timezone.now()
    with db_fails(), transaction.atomic():
        ClosureFactory(starts_at=now, ends_at=now)
    assert Closure.objects.count() == 0
    assert ClosureFactory().pk


# ---- catalog ----


def test_category_depth_max_two() -> None:
    top = CategoryFactory()
    sub = CategoryFactory(parent=top)
    with pytest.raises(ValidationError):
        CategoryFactory(parent=sub)
    assert sub.parent_id == top.pk and top.is_top_level


def test_category_cannot_be_own_parent_or_nest_with_children() -> None:
    top = CategoryFactory()
    CategoryFactory(parent=top)
    other = CategoryFactory()
    top.parent = other
    with pytest.raises(ValidationError):
        top.save()
    other.parent = other
    with pytest.raises(ValidationError):
        other.save()
    assert Category.objects.filter(parent__isnull=True).count() == 2


def test_category_selectors() -> None:
    from apps.catalog.selectors import category_ids_with_descendants, category_tree

    top = CategoryFactory()
    sub = CategoryFactory(parent=top)
    CategoryFactory(parent=top, is_active=False)
    CategoryFactory(is_active=False)
    assert category_tree()[0].pk == top.pk
    assert sorted(category_ids_with_descendants(top.pk))[
        0
    ] == top.pk and sub.pk in category_ids_with_descendants(top.pk)


# ---- geo ----


def test_area_slug_unique_per_city() -> None:
    city = CityFactory()
    AreaFactory(city=city, slug="clifton")
    AreaFactory(slug="clifton")  # other city OK
    with db_fails(), transaction.atomic():
        AreaFactory(city=city, slug="clifton")
    assert Area.objects.count() == 2


def test_city_visibility_property_and_selectors() -> None:
    from apps.geo import selectors

    public, hidden = CityFactory(), CityFactory(visibility="hidden")
    inactive = CityFactory(is_active=False)
    assert public.is_visible and not hidden.is_visible and not inactive.is_visible
    assert selectors.visible_cities() == [public]
    assert selectors.get_active_city(inactive.pk) is None
    assert selectors.get_active_city(public.pk) == public


# ---- selectors ----


def test_public_businesses_policy() -> None:
    visible = BusinessFactory()
    BusinessFactory(is_listed=False)
    BusinessFactory(area=AreaFactory(city=CityFactory(visibility="hidden")))
    BusinessFactory(area=AreaFactory(city=CityFactory(visibility="preview")))
    BusinessFactory(area=AreaFactory(city=CityFactory(is_active=False)))
    pending = BusinessFactory(verification_status=VerificationStatus.PENDING)
    draft = BusinessFactory(verification_status=VerificationStatus.REJECTED)
    ids = set(public_businesses().values_list("pk", flat=True))
    assert ids == {visible.pk, pending.pk, draft.pk}


def test_get_public_business_by_uuid_slug_and_city() -> None:
    a, b = AreaFactory(), AreaFactory()
    first = BusinessFactory(slug="glow", area=a)
    second = BusinessFactory(slug="glow", area=b)
    assert get_public_business(str(first.public_id)) == first
    assert get_public_business("glow") == first  # oldest
    assert get_public_business("glow", b.city.slug) == second
    assert get_public_business("glow", "nowhere") is None
    assert get_public_business("missing") is None
    unlisted = BusinessFactory(is_listed=False)
    assert get_public_business(str(unlisted.public_id)) is None


def test_owner_protected_from_deletion() -> None:
    from django.db.models import ProtectedError

    biz = BusinessFactory()
    with pytest.raises(ProtectedError):
        biz.owner.delete()
    assert Business.objects.filter(pk=biz.pk).exists()


# ---- seed ----


def test_seed_demo_creates_and_is_idempotent() -> None:
    call_command("seed_demo", "--count", "12", stdout=__import__("io").StringIO())
    counts = (
        Business.objects.count(),
        Staff.objects.count(),
        Service.objects.count(),
        StaffService.objects.count(),
        OpeningHours.objects.count(),
        Area.objects.count(),
        Category.objects.count(),
    )
    assert counts[0] == 12 and all(c > 0 for c in counts)
    call_command("seed_demo", "--count", "12", stdout=__import__("io").StringIO())
    again = (
        Business.objects.count(),
        Staff.objects.count(),
        Service.objects.count(),
        StaffService.objects.count(),
        OpeningHours.objects.count(),
        Area.objects.count(),
        Category.objects.count(),
    )
    assert again == counts


def test_seed_demo_extending_count_adds_only_new() -> None:
    out = __import__("io").StringIO()
    call_command("seed_demo", "--count", "5", stdout=out)
    call_command("seed_demo", "--count", "8", stdout=out)
    assert Business.objects.count() == 8
    assert "3 new businesses" in out.getvalue()


def test_seed_demo_data_quality() -> None:
    call_command("seed_demo", "--count", "20", stdout=__import__("io").StringIO())
    from apps.geo.models import City

    assert City.objects.filter(visibility="public").count() == 1
    for biz in Business.objects.all():
        assert biz.opening_hours.exists() and biz.staff.exists() and biz.services.exists()
        # every service has at least one staff member who can do it
        for svc in biz.services.all():
            assert svc.staff_services.exists()
        assert 24.7 < biz.location.y < 25.1 and 66.8 < biz.location.x < 67.3
    assert Business.objects.filter(is_listed=False).exists()
    assert Category.objects.filter(parent__isnull=False).count() >= 6
    assert not Category.objects.filter(parent__parent__isnull=False).exists()

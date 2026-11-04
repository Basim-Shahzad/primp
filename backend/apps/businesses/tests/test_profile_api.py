import io
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.accounts.factories import UserFactory
from apps.accounts.roles import Role
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import AppointmentService
from apps.businesses import events
from apps.businesses.factories import (
    BusinessFactory,
    BusinessPhotoFactory,
    ServiceFactory,
    open_all_week,
)
from apps.businesses.models import Business, BusinessPhoto, Closure, OpeningHours, Service
from apps.businesses.tests.provider_helpers import (
    P,
    call,
    customer_headers,
    owner_headers,
    png_bytes,
)
from apps.catalog.factories import CategoryFactory
from apps.core.models import AuditLog
from apps.geo.factories import AreaFactory, CityFactory

pytestmark = pytest.mark.django_db
PUBLIC = "/api/v1/businesses"


def mine(**kw: Any) -> tuple[Business, dict[str, str]]:
    owner, h = owner_headers()
    return BusinessFactory(owner=owner, **kw), h


def url(b: Business, tail: str = "") -> str:
    return f"{P}/businesses/{b.public_id}{tail}"


# ------------------------------------------------------------ access control


def test_every_endpoint_requires_auth_role_and_ownership() -> None:
    b, _h = mine()
    svc = ServiceFactory(business=b)
    photo = BusinessPhotoFactory(business=b)
    closure = Closure.objects.create(
        business=b, starts_at=timezone.now(), ends_at=timezone.now() + timedelta(days=1)
    )
    _, other = owner_headers()
    cust = customer_headers()
    calls: list[tuple[str, str, Any]] = [
        ("get", url(b), None),
        ("patch", url(b), {"description": "x"}),
        ("get", url(b, "/preview"), None),
        ("get", url(b, "/photos"), None),
        ("patch", url(b, "/photos/order"), {"photo_ids": [photo.pk]}),
        ("patch", url(b, f"/photos/{photo.pk}/cover"), None),
        ("delete", url(b, f"/photos/{photo.pk}"), None),
        ("get", url(b, "/services"), None),
        ("post", url(b, "/services"), {"name": "a", "price": 1, "duration_minutes": 5}),
        ("patch", url(b, "/services/order"), {"service_ids": [svc.pk]}),
        ("patch", f"{P}/services/{svc.pk}", {"price": 5}),
        ("delete", f"{P}/services/{svc.pk}", None),
        ("get", url(b, "/hours"), None),
        ("put", url(b, "/hours"), {"days": []}),
        ("get", url(b, "/closures"), None),
        ("post", url(b, "/closures"), {"starts_on": "2026-12-01", "ends_on": "2026-12-02"}),
        ("delete", f"{P}/closures/{closure.pk}", None),
        ("get", url(b, "/booking-settings"), None),
        ("patch", url(b, "/booking-settings"), {"buffer_minutes": 5}),
    ]
    for method, path, body in calls:
        anon = call(method, path, {}, body)
        assert anon.status_code == 401, (method, path)
        bad = call(method, path, {"HTTP_AUTHORIZATION": "Bearer junk"}, body)
        assert bad.status_code == 401, (method, path)
        assert call(method, path, cust, body).status_code == 403, (method, path)
        assert call(method, path, other, body).status_code == 404, (method, path)
    assert Service.objects.filter(pk=svc.pk, price=1000).exists()
    assert BusinessPhoto.objects.filter(pk=photo.pk).exists() and Closure.objects.exists()


# ------------------------------------------------------------ profile


def test_get_business_returns_owner_view() -> None:
    b, h = mine()
    r = call("get", url(b), h)
    assert r.status_code == 200
    data = r.json()
    assert data["id"] == str(b.public_id) and data["status"] == "verified"
    assert data["is_verified"] is True and data["pending_review_changes"] is None


def test_patch_details_apply_immediately_and_refresh_public_page() -> None:
    b, h = mine()
    before = Client().get(f"{PUBLIC}/{b.public_id}").json()
    area = AreaFactory(city=b.city)
    r = call(
        "patch",
        url(b),
        h,
        {
            "description": "New text",
            "phone": "+92 300 7654321",
            "whatsapp": "",
            "address": "5 Street",
            "gender_type": "men",
            "area_id": area.pk,
            "lat": 24.87,
            "lng": 67.01,
        },
    )
    assert r.status_code == 200, r.content
    b.refresh_from_db()
    assert (b.description, b.phone, b.whatsapp, b.address) == (
        "New text",
        "+923007654321",
        "",
        "5 Street",
    )
    assert b.gender_type == "men" and b.area_id == area.pk
    assert (round(b.location.y, 2), round(b.location.x, 2)) == (24.87, 67.01)
    after = Client().get(f"{PUBLIC}/{b.public_id}").json()  # cache invalidated
    assert before["description"] != after["description"] == "New text"
    assert after["location"] == {"lat": 24.87, "lng": 67.01} and after["gender_type"] == "men"


def test_partial_patch_leaves_other_fields() -> None:
    b, h = mine(description="keep", address="keep addr")
    assert call("patch", url(b), h, {"phone": "+923001112233"}).status_code == 200
    b.refresh_from_db()
    assert b.description == "keep" and b.address == "keep addr" and b.phone == "+923001112233"


def test_area_can_be_cleared_with_null() -> None:
    b, h = mine()
    assert b.area_id
    assert call("patch", url(b), h, {"area_id": None}).status_code == 200
    b.refresh_from_db()
    assert b.area_id is None


@pytest.mark.parametrize(
    "body,field",
    [
        ({"name": None}, "name"),
        ({"name": ""}, "name"),
        ({"name": "n" * 151}, "name"),
        ({"gender_type": "x"}, "gender_type"),
        ({"phone": "nope"}, "phone"),
        ({"address": ""}, "address"),
        ({"description": "d" * 2001}, "description"),
        ({"lat": 100}, "lat"),
        ({"lng": -181}, "lng"),
        ({"category_id": 0}, "category_id"),
        ({"area_id": 0}, "area_id"),
        ({"city_id": 0}, "city_id"),
    ],
)
def test_profile_validation(body: dict[str, Any], field: str) -> None:
    b, h = mine()
    r = call("patch", url(b), h, body)
    assert r.status_code == 422 and field in r.json()["fields"], r.content


def test_pin_outside_city_rejected() -> None:
    b, h = mine()
    r = call("patch", url(b), h, {"lat": 31.5, "lng": 74.3})  # Lahore, from a Karachi city
    assert r.status_code == 422 and "lat" in r.json()["fields"]


def test_name_change_on_verified_business_waits_for_review(run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h = mine(name="Old Name")
    new_cat = CategoryFactory()
    received: list[tuple[int, dict[str, Any]]] = []

    def handler(sender: Any, business_id: int, changes: dict[str, Any], **kw: Any) -> None:
        received.append((business_id, changes))

    events.business_change_review_requested.connect(handler, weak=False)
    try:
        with run_commit():
            r = call(
                "patch",
                url(b),
                h,
                {"name": "Brand New", "category_id": new_cat.pk, "address": "A1"},
            )
    finally:
        events.business_change_review_requested.disconnect(handler)
    assert r.status_code == 200, r.content
    assert r.json()["name"] == "Old Name" and r.json()["address"] == "A1"
    assert r.json()["pending_review_changes"] == {"name": "Brand New", "category_id": new_cat.pk}
    b.refresh_from_db()
    assert b.name == "Old Name" and b.slug and b.category_id != new_cat.pk
    public = Client().get(f"{PUBLIC}/{b.public_id}").json()
    assert public["name"] == "Old Name" and public["address"] == "A1"
    assert received == [(b.pk, {"name": "Brand New", "category_id": new_cat.pk})]
    a = AuditLog.objects.get(action="business.change_review_requested")
    assert a.after == {"name": "Brand New", "category_id": new_cat.pk}


def test_same_name_on_verified_business_is_not_a_change() -> None:
    b, h = mine(name="Same")
    r = call("patch", url(b), h, {"name": "Same"})
    assert r.status_code == 200 and r.json()["pending_review_changes"] is None


def test_unverified_business_name_applies_immediately() -> None:
    b, h = mine(verification_status="draft", is_listed=False, name="Old")
    r = call("patch", url(b), h, {"name": "Fresh Name"})
    assert r.status_code == 200 and r.json()["name"] == "Fresh Name"
    b.refresh_from_db()
    assert b.slug == "fresh-name" and b.pending_review_changes is None


def test_second_gated_change_merges_pending() -> None:
    b, h = mine(name="Old")
    call("patch", url(b), h, {"name": "One"})
    r = call("patch", url(b), h, {"category_id": CategoryFactory().pk})
    assert set(r.json()["pending_review_changes"]) == {"name", "category_id"}


def test_change_triggers_frontend_revalidation(run_commit, settings) -> None:  # type: ignore[no-untyped-def]
    settings.FRONTEND_REVALIDATE_URL = "https://front.example/revalidate"
    settings.FRONTEND_REVALIDATE_SECRET = "s3cret"
    b, h = mine()
    with mock.patch("apps.businesses.tasks.requests.post") as post, run_commit():
        assert call("patch", url(b), h, {"description": "x"}).status_code == 200
    assert post.called and post.call_args.args[0] == "https://front.example/revalidate"


# ------------------------------------------------------------ preview


def test_preview_equals_public_listing_and_works_unlisted() -> None:
    b, h = mine()
    ServiceFactory(business=b)
    open_all_week(b)
    public = Client().get(f"{PUBLIC}/{b.public_id}").json()
    preview = call("get", url(b, "/preview"), h).json()
    for key in ("is_open_now",):
        public.pop(key, None), preview.pop(key, None)
    assert preview == public
    Business.objects.filter(pk=b.pk).update(is_listed=False, verification_status="draft")
    assert Client().get(f"{PUBLIC}/{b.public_id}").status_code == 404
    again = call("get", url(b, "/preview"), h)
    assert again.status_code == 200 and again.json()["id"] == str(b.public_id)


# ------------------------------------------------------------ photos


def jpeg_with_exif(size: tuple[int, int] = (2400, 1200)) -> bytes:
    exif = Image.Exif()
    exif[0x010F] = "SecretCameraMaker"
    out = io.BytesIO()
    Image.new("RGB", size, (1, 2, 3)).save(out, format="JPEG", exif=exif.tobytes())
    return out.getvalue()


def upload_photos(b: Business, h: dict[str, str], *datas: bytes) -> Any:
    files = [
        SimpleUploadedFile(f"p{i}.jpg", d, content_type="image/jpeg") for i, d in enumerate(datas)
    ]
    return Client().post(url(b, "/photos"), {"files": files}, **h)


def test_multi_upload_resizes_to_three_sizes_and_strips_exif() -> None:
    b, h = mine()
    r = upload_photos(b, h, jpeg_with_exif(), png_bytes((300, 200)))
    assert r.status_code == 201, r.content
    data = r.json()
    assert len(data) == 2 and data[0]["is_cover"] is True and data[1]["is_cover"] is False
    first = BusinessPhoto.objects.get(pk=data[0]["id"])
    stem = first.image.rsplit(".", 1)[0]
    sizes = {}
    for suffix in ("", "_480", "_160"):
        with default_storage.open(f"{stem}{suffix}.jpg") as f:
            img = Image.open(f)
            assert not img.getexif() and img.format == "JPEG"
            sizes[suffix] = max(img.size)
    assert sizes == {"": 1600, "_480": 480, "_160": 160}
    assert data[0]["thumb"].endswith("_160.jpg") and data[0]["medium"].endswith("_480.jpg")


def test_upload_rejects_bad_files_and_empty_request() -> None:
    b, h = mine()
    assert upload_photos(b, h, b"not an image").status_code == 422
    assert upload_photos(b, h, png_bytes(), b"junk").status_code == 422
    assert BusinessPhoto.objects.filter(business=b).count() == 0
    assert Client().post(url(b, "/photos"), {}, **h).status_code == 422


def test_photo_limit_is_20() -> None:
    b, h = mine()
    for _ in range(19):
        BusinessPhotoFactory(business=b)
    assert upload_photos(b, h, png_bytes()).status_code == 201
    r = upload_photos(b, h, png_bytes())
    assert r.status_code == 422 and r.json()["code"] == "too_many_photos"
    assert b.photos.count() == 20


def test_photo_order_must_be_a_permutation() -> None:
    b, h = mine()
    a, c, d = (BusinessPhotoFactory(business=b, sort_order=i) for i in range(3))
    foreign = BusinessPhotoFactory()
    ok = call("patch", url(b, "/photos/order"), h, {"photo_ids": [d.pk, a.pk, c.pk]})
    assert ok.status_code == 200
    assert [p["id"] for p in call("get", url(b, "/photos"), h).json()][0:3] != []
    ordered = list(b.photos.order_by("sort_order").values_list("pk", flat=True))
    assert ordered == [d.pk, a.pk, c.pk]
    for bad in (
        [a.pk, c.pk],
        [a.pk, a.pk, c.pk, d.pk],
        [a.pk, c.pk, d.pk, foreign.pk],
        [a.pk, c.pk, foreign.pk],
    ):
        r = call("patch", url(b, "/photos/order"), h, {"photo_ids": bad})
        assert r.status_code == 422 and "photo_ids" in r.json()["fields"], bad
    assert list(b.photos.order_by("sort_order").values_list("pk", flat=True)) == ordered


def test_set_cover_moves_flag_and_foreign_photo_404() -> None:
    b, h = mine()
    one = BusinessPhotoFactory(business=b, is_cover=True)
    two = BusinessPhotoFactory(business=b)
    r = call("patch", url(b, f"/photos/{two.pk}/cover"), h)
    assert r.status_code == 200 and r.json()["is_cover"] is True
    one.refresh_from_db()
    assert one.is_cover is False and b.photos.filter(is_cover=True).count() == 1
    foreign = BusinessPhotoFactory()
    assert call("patch", url(b, f"/photos/{foreign.pk}/cover"), h).status_code == 404
    assert call("delete", url(b, f"/photos/{foreign.pk}"), h).status_code == 404


def test_delete_photo_promotes_new_cover_and_removes_files(run_commit) -> None:  # type: ignore[no-untyped-def]
    b, h = mine()
    assert upload_photos(b, h, png_bytes(), png_bytes()).status_code == 201
    cover = b.photos.get(is_cover=True)
    with run_commit():
        assert call("delete", url(b, f"/photos/{cover.pk}"), h).status_code == 204
    assert not default_storage.exists(cover.image)
    assert b.photos.count() == 1 and b.photos.get().is_cover is True


def test_cannot_delete_last_photo_of_listed_business() -> None:
    b, h = mine(is_listed=True)
    only = BusinessPhotoFactory(business=b, is_cover=True)
    r = call("delete", url(b, f"/photos/{only.pk}"), h)
    assert r.status_code == 409 and r.json()["code"] == "last_photo"
    Business.objects.filter(pk=b.pk).update(is_listed=False)
    assert call("delete", url(b, f"/photos/{only.pk}"), h).status_code == 204


# ------------------------------------------------------------ services

SVC = {"name": "Facial", "price": 2500, "duration_minutes": 45}


def test_create_list_service_and_public_page_reflects() -> None:
    b, h = mine()
    cat = CategoryFactory()
    r = call(
        "post",
        url(b, "/services"),
        h,
        {**SVC, "category_id": cat.pk, "gender": "women", "is_from_price": True},
    )
    assert r.status_code == 201, r.content
    s = Service.objects.get(pk=r.json()["id"])
    assert (s.business_id, s.price, s.duration_minutes, s.gender, s.is_from_price) == (
        b.pk,
        2500,
        45,
        "women",
        True,
    )
    assert s.category_id == cat.pk
    listing = call("get", url(b, "/services"), h).json()
    assert [x["name"] for x in listing] == ["Facial"]
    page = Client().get(f"{PUBLIC}/{b.public_id}").json()
    assert page["services"][0]["services"][0]["name"] == "Facial"


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"name": ""}, "name"),
        ({"name": "n" * 151}, "name"),
        ({"price": -1}, "price"),
        ({"price": "x"}, "price"),
        ({"duration_minutes": 4}, "duration_minutes"),
        ({"duration_minutes": 7}, "duration_minutes"),
        ({"duration_minutes": 605}, "duration_minutes"),
        ({"duration_minutes": 0}, "duration_minutes"),
        ({"gender": "x"}, "gender"),
        ({"description": "d" * 1001}, "description"),
        ({"category_id": 0}, "category_id"),
    ],
)
def test_service_validation(bad: dict[str, Any], field: str) -> None:
    b, h = mine()
    r = call("post", url(b, "/services"), h, {**SVC, **bad})
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not b.services.exists()


@pytest.mark.parametrize("duration,price", [(5, 0), (600, 0), (300, 10_000_000)])
def test_service_boundaries_ok(duration: int, price: int) -> None:
    b, h = mine()
    r = call("post", url(b, "/services"), h, {**SVC, "duration_minutes": duration, "price": price})
    assert r.status_code == 201


def test_inactive_category_rejected_for_service() -> None:
    b, h = mine()
    inactive = CategoryFactory(is_active=False)
    r = call("post", url(b, "/services"), h, {**SVC, "category_id": inactive.pk})
    assert r.status_code == 422 and "category_id" in r.json()["fields"]


def test_patch_service_and_price_change_keeps_existing_snapshot() -> None:
    b, h = mine()
    s = ServiceFactory(business=b, price=1000)
    appt = AppointmentFactory(business=b)
    AppointmentService.objects.create(
        appointment=appt, service=s, name=s.name, price_at_booking=1000, duration_at_booking=60
    )
    r = call(
        "patch", f"{P}/services/{s.pk}", h, {"price": 1800, "name": "Renamed", "is_active": False}
    )
    assert r.status_code == 200 and r.json()["price"] == 1800
    s.refresh_from_db()
    assert (s.price, s.name, s.is_active) == (1800, "Renamed", False)
    item = AppointmentService.objects.get(appointment=appt)
    assert item.price_at_booking == 1000 and item.name != "Renamed"


def test_patch_service_validation_and_nulls() -> None:
    b, h = mine()
    s = ServiceFactory(business=b)
    assert call("patch", f"{P}/services/{s.pk}", h, {"price": None}).status_code == 422
    assert call("patch", f"{P}/services/{s.pk}", h, {"duration_minutes": 11}).status_code == 422
    assert call("patch", f"{P}/services/{s.pk}", h, {"category_id": None}).status_code == 200
    assert call("patch", f"{P}/services/{s.pk}", h, {"category_id": 0}).status_code == 422


def test_delete_unreferenced_service_removes_it() -> None:
    b, h = mine()
    s = ServiceFactory(business=b)
    r = call("delete", f"{P}/services/{s.pk}", h)
    assert r.status_code == 200 and r.json() == {"deactivated": False}
    assert not Service.objects.filter(pk=s.pk).exists()


def test_delete_referenced_service_deactivates_and_bookings_stay_valid() -> None:
    b, h = mine()
    s = ServiceFactory(business=b)
    appt = AppointmentFactory(business=b, status="confirmed")
    AppointmentService.objects.create(
        appointment=appt, service=s, name=s.name, price_at_booking=s.price, duration_at_booking=60
    )
    r = call("delete", f"{P}/services/{s.pk}", h)
    assert r.json() == {"deactivated": True}
    s.refresh_from_db()
    assert s.is_active is False
    appt.refresh_from_db()
    assert appt.status == "confirmed" and appt.items.count() == 1
    page = Client().get(f"{PUBLIC}/{b.public_id}").json()
    assert page["services"] == []


def test_service_order_permutation() -> None:
    b, h = mine()
    a, c = ServiceFactory(business=b, sort_order=0), ServiceFactory(business=b, sort_order=1)
    foreign = ServiceFactory()
    assert (
        call("patch", url(b, "/services/order"), h, {"service_ids": [c.pk, a.pk]}).status_code
        == 200
    )
    assert [x.pk for x in b.services.all()] == [c.pk, a.pk]
    for bad in ([a.pk], [a.pk, a.pk], [a.pk, c.pk, foreign.pk], [a.pk, foreign.pk]):
        r = call("patch", url(b, "/services/order"), h, {"service_ids": bad})
        assert r.status_code == 422 and "service_ids" in r.json()["fields"]


# ------------------------------------------------------------ hours


def week(**days: list[tuple[str, str]]) -> dict[str, Any]:
    return {
        "days": [
            {"weekday": int(d[1:]), "shifts": [{"opens_at": o, "closes_at": c} for o, c in shifts]}
            for d, shifts in days.items()
        ]
    }


def test_put_hours_replaces_week_with_split_shifts() -> None:
    b, h = mine()
    open_all_week(b)
    r = call(
        "put",
        url(b, "/hours"),
        h,
        week(d0=[("09:00", "13:00"), ("15:00", "21:00")], d1=[("10:00", "18:00")]),
    )
    assert r.status_code == 200, r.content
    rows = list(
        OpeningHours.objects.filter(business=b).values_list("weekday", "opens_at", "closes_at")
    )
    assert len(rows) == 3  # every other day removed
    days = {d["weekday"]: d["shifts"] for d in r.json()["days"]}
    assert len(days[0]) == 2 and days[2] == [] and len(days) == 7
    assert call("get", url(b, "/hours"), h).json() == r.json()
    page = Client().get(f"{PUBLIC}/{b.public_id}").json()
    assert len(page["hours"][0]["shifts"]) == 2


@pytest.mark.parametrize(
    "days",
    [
        week(d0=[("10:00", "12:00"), ("11:00", "14:00")]),
        week(d0=[("12:00", "10:00")]),
        week(d0=[("10:00", "10:00")]),
        week(d0=[("08:00", "09:00"), ("10:00", "11:00"), ("12:00", "13:00")]),
        {"days": [{"weekday": 0, "shifts": []}, {"weekday": 0, "shifts": []}]},
        {"days": [{"weekday": 7, "shifts": []}]},
        {"days": [{"weekday": 0, "shifts": [{"opens_at": "25:00", "closes_at": "26:00"}]}]},
    ],
)
def test_hours_validation_leaves_existing_untouched(days: dict[str, Any]) -> None:
    b, h = mine()
    open_all_week(b)
    r = call("put", url(b, "/hours"), h, days)
    assert r.status_code == 422, r.content
    assert OpeningHours.objects.filter(business=b).count() == 7


def test_touching_shifts_are_allowed_and_clearing_week_works() -> None:
    b, h = mine()
    assert (
        call(
            "put", url(b, "/hours"), h, week(d0=[("09:00", "12:00"), ("12:00", "15:00")])
        ).status_code
        == 200
    )
    assert call("put", url(b, "/hours"), h, {"days": []}).status_code == 200
    assert not OpeningHours.objects.filter(business=b).exists()


# ------------------------------------------------------------ closures


def test_closure_whole_days_in_business_timezone() -> None:
    b, h = mine()
    r = call(
        "post",
        url(b, "/closures"),
        h,
        {"starts_on": "2026-12-24", "ends_on": "2026-12-25", "message": " Eid break "},
    )
    assert r.status_code == 201, r.content
    c = Closure.objects.get(pk=r.json()["id"])
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Asia/Karachi")
    assert c.starts_at.astimezone(tz).strftime("%Y-%m-%d %H:%M") == "2026-12-24 00:00"
    assert c.ends_at.astimezone(tz).strftime("%Y-%m-%d %H:%M") == "2026-12-26 00:00"
    assert c.reason == "Eid break" and c.staff_id is None and c.kind == "holiday"
    listing = call("get", url(b, "/closures"), h).json()
    assert [x["id"] for x in listing] == [c.pk] and listing[0]["message"] == "Eid break"


def test_closure_validation_and_limits() -> None:
    b, h = mine()
    bad = call("post", url(b, "/closures"), h, {"starts_on": "2026-12-25", "ends_on": "2026-12-24"})
    assert bad.status_code == 422 and "ends_on" in bad.json()["fields"]
    same_day = call(
        "post", url(b, "/closures"), h, {"starts_on": "2026-12-25", "ends_on": "2026-12-25"}
    )
    assert same_day.status_code == 201
    long = call(
        "post", url(b, "/closures"), h, {"starts_on": "2026-01-01", "ends_on": "2027-01-02"}
    )
    assert long.status_code == 422
    assert (
        call(
            "post", url(b, "/closures"), h, {"starts_on": "nope", "ends_on": "2026-01-01"}
        ).status_code
        == 422
    )
    assert (
        call(
            "post",
            url(b, "/closures"),
            h,
            {"starts_on": "2026-01-01", "ends_on": "2026-01-02", "message": "m" * 256},
        ).status_code
        == 422
    )


def test_delete_closure_and_other_owner_404() -> None:
    b, h = mine()
    c = Closure.objects.create(
        business=b, starts_at=timezone.now(), ends_at=timezone.now() + timedelta(days=1)
    )
    _, other = owner_headers()
    assert call("delete", f"{P}/closures/{c.pk}", other).status_code == 404
    assert call("delete", f"{P}/closures/{c.pk}", h).status_code == 204
    assert not Closure.objects.filter(pk=c.pk).exists()
    assert call("delete", f"{P}/closures/{c.pk}", h).status_code == 404


# ------------------------------------------------------------ booking settings


def test_booking_settings_patch_success_audit_and_cache() -> None:
    b, h = mine()
    r = call(
        "patch",
        url(b, "/booking-settings"),
        h,
        {
            "confirm_mode": "manual",
            "cancellation_notice_hours": 12,
            "lead_time_minutes": 90,
            "max_advance_days": 30,
            "slot_interval_minutes": 15,
            "buffer_minutes": 10,
            "staff_level_booking": True,
            "parallel_capacity": 3,
        },
    )
    assert r.status_code == 200, r.content
    b.refresh_from_db()
    assert (b.confirm_mode, b.cancellation_notice_hours, b.lead_time_minutes) == ("manual", 12, 90)
    assert (b.max_advance_days, b.slot_interval_minutes, b.buffer_minutes) == (30, 15, 10)
    assert b.staff_level_booking is True and b.parallel_capacity == 3
    page = Client().get(f"{PUBLIC}/{b.public_id}").json()["booking"]
    assert page["confirm_mode"] == "manual" and page["slot_interval_minutes"] == 15
    assert (
        AuditLog.objects.get(action="business.booking_settings_changed").after["buffer_minutes"]
        == 10
    )
    assert call("get", url(b, "/booking-settings"), h).json()["parallel_capacity"] == 3


@pytest.mark.parametrize(
    "body",
    [
        {"confirm_mode": "auto"},
        {"cancellation_notice_hours": -1},
        {"cancellation_notice_hours": 73},
        {"lead_time_minutes": -1},
        {"lead_time_minutes": 1441},
        {"max_advance_days": 0},
        {"max_advance_days": 181},
        {"slot_interval_minutes": 20},
        {"slot_interval_minutes": 10},
        {"buffer_minutes": -1},
        {"buffer_minutes": 61},
        {"parallel_capacity": 0},
        {"parallel_capacity": 51},
        {"staff_level_booking": "maybe"},
        {"buffer_minutes": None},
    ],
)
def test_booking_settings_out_of_range_rejected(body: dict[str, Any]) -> None:
    b, h = mine()
    r = call("patch", url(b, "/booking-settings"), h, body)
    assert r.status_code == 422, r.content


@pytest.mark.parametrize(
    "body",
    [
        {"cancellation_notice_hours": 0},
        {"cancellation_notice_hours": 72},
        {"lead_time_minutes": 0},
        {"lead_time_minutes": 1440},
        {"max_advance_days": 1},
        {"max_advance_days": 180},
        {"slot_interval_minutes": 60},
        {"buffer_minutes": 0},
        {"buffer_minutes": 60},
        {"parallel_capacity": 1},
        {"parallel_capacity": 50},
    ],
)
def test_booking_settings_boundaries_accepted(body: dict[str, Any]) -> None:
    b, h = mine()
    assert call("patch", url(b, "/booking-settings"), h, body).status_code == 200


def test_unused_imports_guard() -> None:
    assert UserFactory and Role and CityFactory

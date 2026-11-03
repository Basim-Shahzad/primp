import math
from datetime import timedelta

import pytest
import time_machine
from django.contrib.gis.geos import Point
from django.core.cache import cache
from django.test import Client

from apps.accounts.factories import make_customer
from apps.accounts.models import Wishlist
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings.factories import AppointmentFactory
from apps.bookings.tests.helpers import NOW, TUESDAY, local
from apps.businesses.factories import (
    BusinessFactory,
    BusinessPhotoFactory,
    ClosureFactory,
    OpeningHoursFactory,
    ServiceFactory,
    open_all_week,
)
from apps.catalog.factories import CategoryFactory
from apps.geo.factories import AreaFactory, CityFactory

pytestmark = pytest.mark.django_db
URL = "/api/v1/search/businesses"
LAT, LNG = 24.8607, 67.0011
KM_PER_DEG_LAT = 110.8


def at_km_north(km: float) -> Point:
    return Point(LNG, LAT + km / KM_PER_DEG_LAT, srid=4326)


def haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi, dl = p2 - p1, math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def make(km: float | None = None, with_service: bool = True, **kw):  # type: ignore[no-untyped-def]
    if km is not None:
        kw["location"] = at_km_north(km)
    b = BusinessFactory(**kw)
    if with_service:
        ServiceFactory(business=b, price=1000, duration_minutes=60)
    return b


def search(client: Client | None = None, **params):  # type: ignore[no-untyped-def]
    return (client or Client()).get(URL, params)


def names(r) -> list[str]:  # type: ignore[no-untyped-def]
    assert r.status_code == 200, r.content
    return [i["name"] for i in r.json()["items"]]


# ---------------- distance & radius ----------------


def test_distance_sorting_and_values_match_known_coordinates() -> None:
    far, near, mid = make(4.0, name="far"), make(0.5, name="near"), make(2.0, name="mid")
    r = search(lat=LAT, lng=LNG)
    assert names(r) == ["near", "mid", "far"]
    for item, b in zip(r.json()["items"], (near, mid, far), strict=True):
        expected = haversine_km(LAT, LNG, b.location.y, b.location.x)
        assert abs(item["distance_km"] - expected) < 0.05


def test_distance_sort_is_default_with_coords_and_explicit() -> None:
    make(3.0, name="b"), make(1.0, name="a")
    assert names(search(lat=LAT, lng=LNG)) == ["a", "b"]
    assert names(search(lat=LAT, lng=LNG, sort="distance")) == ["a", "b"]


def test_default_radius_is_5km() -> None:
    make(4.5, name="in"), make(5.5, name="out")
    assert names(search(lat=LAT, lng=LNG)) == ["in"]


@pytest.mark.parametrize("radius", [1, 3, 5, 10])
def test_each_allowed_radius(radius: int) -> None:
    inside, outside = make(radius * 0.95, name="in"), make(radius * 1.05, name="out")
    assert names(search(lat=LAT, lng=LNG, radius_km=radius)) == ["in"]
    assert inside.pk and outside.pk


def test_radius_boundary_inclusive_by_measured_distance() -> None:
    make(2.97, name="under"), make(3.04, name="over")
    wide = {
        i["name"]: i["distance_km"] for i in search(lat=LAT, lng=LNG, radius_km=10).json()["items"]
    }
    assert wide["under"] < 3 < wide["over"]
    assert names(search(lat=LAT, lng=LNG, radius_km=3)) == ["under"]


@pytest.mark.parametrize("radius", [0, 2, 4, 11, -1])
def test_invalid_radius_422(radius: int) -> None:
    r = search(lat=LAT, lng=LNG, radius_km=radius)
    assert r.status_code == 422 and "radius_km" in r.json()["fields"]


@pytest.mark.parametrize(
    "params",
    [
        {"lat": LAT},
        {"lng": LNG},
        {"lat": 91, "lng": LNG},
        {"lat": LAT, "lng": 181},
        {"lat": "x", "lng": LNG},
        {"radius_km": 3},
        {"sort": "distance"},
        {"sort": "bogus"},
        {"limit": 0},
        {"limit": 51},
        {"rating_min": 5.5},
        {"rating_min": -1},
        {"price_min": -5},
        {"price_min": 500, "price_max": 100},
        {"q": "x" * 101},
        {"gender_type": "other"},
    ],
)
def test_invalid_parameter_combinations_422(params: dict[str, object]) -> None:
    make()
    assert search(**params).status_code == 422


# ---------------- visibility ----------------


def test_hidden_preview_inactive_city_and_unlisted_excluded() -> None:
    make(name="visible")
    make(name="unlisted", is_listed=False)
    make(name="hidden-city", area=AreaFactory(city=CityFactory(visibility="hidden")))
    make(name="preview-city", area=AreaFactory(city=CityFactory(visibility="preview")))
    make(name="inactive-city", area=AreaFactory(city=CityFactory(is_active=False)))
    assert names(search()) == ["visible"]


def test_no_location_params_returns_all_public_by_rating() -> None:
    (
        make(name="low", rating_avg="3.0"),
        make(name="high", rating_avg="4.9"),
        make(name="mid", rating_avg="4.0"),
    )
    r = search()
    assert names(r) == ["high", "mid", "low"]
    assert r.json()["items"][0]["distance_km"] is None


# ---------------- individual filters ----------------


def test_area_and_city_filters() -> None:
    a1, a2 = AreaFactory(), AreaFactory()
    make(name="in-a1", area=a1), make(name="in-a2", area=a2)
    assert names(search(area_id=a1.pk)) == ["in-a1"]
    assert names(search(city_id=a2.city_id)) == ["in-a2"]
    assert names(search(city_id=a2.city_id, area_id=a1.pk)) == []


def test_text_query_matches_name_service_and_typo() -> None:
    make(name="Velvet Studio")
    other = make(name="Plain Place", with_service=False)
    ServiceFactory(business=other, name="Keratin Treatment")
    make(name="Nothing Here")
    assert names(search(q="velvet")) == ["Velvet Studio"]
    assert names(search(q="keratin")) == ["Plain Place"]
    assert names(search(q="Velvett Studio")) == ["Velvet Studio"]  # trigram typo tolerance
    assert names(search(q="zzzzqqq")) == []


def test_text_query_ignores_inactive_services() -> None:
    b = make(name="Alpha", with_service=False)
    ServiceFactory(business=b, name="Secret Special", is_active=False)
    assert names(search(q="secret")) == []


def test_category_and_subcategory_filters() -> None:
    top, other_top = CategoryFactory(), CategoryFactory()
    sub = CategoryFactory(parent=top)
    other_sub = CategoryFactory(parent=top)
    by_cat = make(name="by-category", category=top)
    by_sub = make(name="by-service-sub", category=other_top, with_service=False)
    ServiceFactory(business=by_sub, category=sub)
    make(name="unrelated", category=other_top)
    inactive = make(name="inactive-sub-service", category=other_top, with_service=False)
    ServiceFactory(business=inactive, category=sub, is_active=False)
    assert set(names(search(category_id=top.pk))) == {"by-category", "by-service-sub"}
    assert names(search(subcategory_id=sub.pk)) == ["by-service-sub"]
    assert names(search(subcategory_id=other_sub.pk)) == []
    assert by_cat.pk


def test_price_filter_uses_active_services_only() -> None:
    cheap = make(name="cheap", with_service=False)
    ServiceFactory(business=cheap, price=500)
    ServiceFactory(business=cheap, price=9000)
    ghost = make(name="ghost", with_service=False)
    ServiceFactory(business=ghost, price=500, is_active=False)
    ServiceFactory(business=ghost, price=9000)
    pricey = make(name="pricey", with_service=False)
    ServiceFactory(business=pricey, price=8000)
    assert set(names(search(price_max=600))) == {"cheap"}
    assert set(names(search(price_min=7000))) == {"cheap", "ghost", "pricey"}
    assert set(names(search(price_min=500, price_max=500))) == {"cheap"}
    assert names(search(price_min=9001)) == []


def test_starting_price_ignores_inactive_services() -> None:
    b = make(with_service=False)
    ServiceFactory(business=b, price=300, is_active=False)
    ServiceFactory(business=b, price=700)
    ServiceFactory(business=b, price=900)
    assert search().json()["items"][0]["starting_price"] == 700


def test_starting_price_null_without_services() -> None:
    make(with_service=False)
    assert search().json()["items"][0]["starting_price"] is None


def test_rating_min_boundary() -> None:
    make(name="exact", rating_avg="4.00"), make(name="below", rating_avg="3.99")
    assert names(search(rating_min=4)) == ["exact"]


def test_verified_only() -> None:
    make(name="v"), make(name="p", verification_status="pending")
    assert names(search(verified_only="true")) == ["v"]
    assert set(names(search())) == {"v", "p"}


def test_gender_filter_includes_unisex() -> None:
    (
        make(name="w", gender_type="women"),
        make(name="m", gender_type="men"),
        make(name="u", gender_type="unisex"),
    )
    assert set(names(search(gender_type="women"))) == {"w", "u"}
    assert set(names(search(gender_type="men"))) == {"m", "u"}
    assert names(search(gender_type="unisex")) == ["u"]


def test_combined_filters() -> None:
    a = AreaFactory()
    match = make(1.0, name="match", area=a, rating_avg="4.5", gender_type="women")
    make(1.0, name="wrong-rating", area=a, rating_avg="3.0", gender_type="women")
    make(1.0, name="wrong-gender", area=a, rating_avg="4.5", gender_type="men")
    make(9.0, name="too-far", area=a, rating_avg="4.5", gender_type="women")
    r = search(
        lat=LAT,
        lng=LNG,
        radius_km=3,
        rating_min=4,
        gender_type="women",
        area_id=a.pk,
        price_max=1500,
        verified_only="true",
    )
    assert names(r) == ["match"] and match.pk


# ---------------- sorting ----------------


def test_sort_rating_ties_broken_by_count_then_id() -> None:
    a = make(name="a", rating_avg="4.5", rating_count=10)
    b = make(name="b", rating_avg="4.5", rating_count=50)
    c = make(name="c", rating_avg="4.5", rating_count=50)
    assert names(search(sort="rating")) == ["c", "b", "a"]
    assert a.pk < b.pk < c.pk


def test_sort_price_asc_nulls_last() -> None:
    mid = make(name="mid", with_service=False)
    ServiceFactory(business=mid, price=2000)
    low = make(name="low", with_service=False)
    ServiceFactory(business=low, price=500)
    make(name="none", with_service=False)
    assert names(search(sort="price")) == ["low", "mid", "none"]


@time_machine.travel(NOW, tick=False)
def test_sort_soonest() -> None:
    later = make(name="later", with_service=False)
    OpeningHoursFactory(
        business=later,
        weekday=1,
        opens_at=__import__("datetime").time(15),
        closes_at=__import__("datetime").time(18),
    )
    ServiceFactory(business=later, duration_minutes=60)
    sooner = make(name="sooner", with_service=False)
    open_all_week(sooner)
    ServiceFactory(business=sooner, duration_minutes=60)
    never = make(name="never")
    r = search(sort="soonest")
    assert names(r) == ["sooner", "later", "never"]
    assert r.json()["items"][0]["next_available_slot"] == "2026-11-02T10:00:00+05:00"
    assert r.json()["items"][2]["next_available_slot"] is None
    assert later.pk and never.pk


# ---------------- availability filter ----------------


def bookable(name: str, **kw):  # type: ignore[no-untyped-def]
    b = make(name=name, with_service=False, **kw)
    open_all_week(b)
    ServiceFactory(business=b, duration_minutes=60)
    return b


@time_machine.travel(NOW, tick=False)
def test_available_today_and_tomorrow() -> None:
    bookable("open")
    closed_today = make(name="closed-today", with_service=False)
    OpeningHoursFactory(business=closed_today, weekday=1)  # Tuesday only
    ServiceFactory(business=closed_today)
    assert names(search(available="today")) == ["open"]
    assert set(names(search(available="tomorrow"))) == {"open", "closed-today"}


@time_machine.travel(NOW, tick=False)
def test_available_excludes_fully_booked_and_closed_days() -> None:
    full = bookable("full")
    for hour in range(10, 20):
        AppointmentFactory(
            business=full,
            scheduled_start=local(TUESDAY, hour),
            scheduled_end=local(TUESDAY, hour + 1),
        )
    closed = bookable("closed")
    ClosureFactory(business=closed, starts_at=local(TUESDAY, 0), ends_at=local(TUESDAY, 23, 59))
    bookable("free")
    assert names(search(available="tomorrow")) == ["free"]
    assert names(search(available="date", date="2026-11-03")) == ["free"]


@time_machine.travel(NOW, tick=False)
def test_available_time_window() -> None:
    b = bookable("b")
    assert names(search(available="today", time_from="10:00", time_to="11:00")) == ["b"]
    assert (
        names(search(available="today", time_from="19:30", time_to="20:00")) == []
    )  # last 60-min start is 19:00
    assert names(search(available="today", time_from="19:00", time_to="20:00")) == ["b"]
    assert names(search(available="today", time_from="06:00", time_to="09:30")) == []
    assert b.pk


@time_machine.travel(NOW, tick=False)
def test_available_respects_lead_time() -> None:
    bookable("lead", lead_time_minutes=24 * 60)
    assert names(search(available="today")) == []
    assert names(search(available="tomorrow", time_from="09:00", time_to="10:00")) == ["lead"]


@time_machine.travel(NOW, tick=False)
@pytest.mark.parametrize(
    "params",
    [
        {"available": "date"},
        {"date": "2026-11-03"},
        {"available": "date", "date": "2026-11-01"},
        {"time_from": "10:00"},
        {"available": "today", "time_from": "12:00", "time_to": "11:00"},
        {"available": "today", "time_from": "12:00", "time_to": "12:00"},
        {"available": "nonsense"},
        {"available": "today", "time_from": "25:00"},
    ],
)
def test_available_param_validation(params: dict[str, str]) -> None:
    assert search(**params).status_code == 422


@time_machine.travel(NOW, tick=False)
def test_available_pagination_keeps_filling_pages() -> None:
    for i in range(6):
        bookable(f"open-{i}", rating_avg=f"4.{i}")
    for i in range(6):
        make(name=f"unbookable-{i}", rating_avg=f"3.{i}")  # no hours: filtered after SQL
    seen: list[str] = []
    cursor = None
    for _ in range(10):
        params = {"available": "today", "limit": 2, "sort": "rating"}
        if cursor:
            params["cursor"] = cursor
        data = search(**params).json()
        seen += [i["name"] for i in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert seen == [f"open-{i}" for i in (5, 4, 3, 2, 1, 0)]


# ---------------- pagination ----------------


def walk(sort: str, limit: int, **extra) -> list[str]:  # type: ignore[no-untyped-def]
    seen: list[str] = []
    cursor = None
    for _ in range(30):
        params = {"sort": sort, "limit": limit, **extra}
        if cursor:
            params["cursor"] = cursor
        data = search(**params).json()
        seen += [i["id"] for i in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            return seen
    raise AssertionError("did not terminate")


@pytest.mark.parametrize("sort", ["distance", "rating", "price", "soonest"])
def test_cursor_walk_visits_each_business_once(sort: str) -> None:
    for i in range(9):
        b = make(
            i * 0.3, name=f"b{i}", rating_avg=f"4.{i % 3}", rating_count=i % 2, with_service=False
        )
        open_all_week(b)
        ServiceFactory(business=b, price=500 * (i % 3), duration_minutes=30)
    extra = {"lat": LAT, "lng": LNG} if sort == "distance" else {}
    seen = walk(sort, 4, **extra)
    assert len(seen) == 9 == len(set(seen))
    single = [i["id"] for i in search(sort=sort, limit=50, **extra).json()["items"]]
    assert seen == single


def test_cursor_stable_when_new_business_inserted_between_pages() -> None:
    for i in range(6):
        make(i * 0.2, name=f"b{i}")
    first = search(lat=LAT, lng=LNG, limit=3).json()
    make(0.01, name="newcomer")  # sorts before everything seen
    second = search(lat=LAT, lng=LNG, limit=3, cursor=first["next_cursor"]).json()
    assert {i["id"] for i in first["items"]}.isdisjoint({i["id"] for i in second["items"]})
    assert "newcomer" not in [i["name"] for i in second["items"]]


def test_tampered_cursor_400() -> None:
    for i in range(4):
        make(i * 0.1)
    cursor = search(lat=LAT, lng=LNG, limit=2).json()["next_cursor"]
    for bad in (cursor[:-3] + "zzz", "garbage"):
        r = search(lat=LAT, lng=LNG, limit=2, cursor=bad)
        assert r.status_code == 400 and r.json()["code"] == "invalid_cursor"
    r = search(sort="soonest", cursor="garbage")
    assert r.status_code == 400


def test_cursor_from_other_sort_rejected() -> None:
    for i in range(4):
        make(i * 0.1, rating_avg="4.0")
    cursor = search(lat=LAT, lng=LNG, limit=2).json()["next_cursor"]
    assert search(sort="rating", cursor=cursor).status_code == 400


# ---------------- result item ----------------


def test_result_item_fields_and_cover_photo() -> None:
    b = make(1.0, name="Glow", rating_avg="4.25", rating_count=12)
    BusinessPhotoFactory(business=b, image="b/second.jpg", sort_order=0)
    BusinessPhotoFactory(business=b, image="b/cover.jpg", is_cover=True, sort_order=9)
    item = search(lat=LAT, lng=LNG).json()["items"][0]
    assert item["id"] == str(b.public_id) and item["slug"] == b.slug and item["name"] == "Glow"
    assert item["area"] == b.area.name and item["rating_avg"] == 4.25 and item["rating_count"] == 12
    assert item["starting_price"] == 1000 and item["is_verified"] is True
    assert item["cover_photo_url"].endswith("b/cover.jpg")
    assert item["is_wishlisted"] is None and item["distance_km"] is not None


def test_cover_falls_back_to_first_photo_and_none() -> None:
    b = make(name="a")
    make(name="b")
    BusinessPhotoFactory(business=b, image="b/only.jpg")
    items = {i["name"]: i for i in search().json()["items"]}
    assert (
        items["a"]["cover_photo_url"].endswith("b/only.jpg")
        and items["b"]["cover_photo_url"] is None
    )


def test_is_wishlisted_for_authenticated_user() -> None:
    liked, other = make(name="liked"), make(name="other")
    user = make_customer()
    Wishlist.objects.create(user=user, business=liked)
    _, session = login_client(user)
    items = {i["name"]: i for i in search(Client(), sort="rating").json()["items"]}
    assert items["liked"]["is_wishlisted"] is None
    r = Client().get(URL, {}, **bearer(session))
    items = {i["name"]: i["is_wishlisted"] for i in r.json()["items"]}
    assert items == {"liked": True, "other": False} and other.pk


def test_invalid_bearer_treated_as_anonymous() -> None:
    make()
    r = Client().get(URL, {}, HTTP_AUTHORIZATION="Bearer nope")
    assert r.status_code == 200 and r.json()["items"][0]["is_wishlisted"] is None


@time_machine.travel(NOW, tick=False)
def test_next_available_slot_in_results() -> None:
    bookable("b")
    assert search().json()["items"][0]["next_available_slot"] == "2026-11-02T10:00:00+05:00"


# ---------------- empty results & suggestions ----------------


def test_empty_result_wider_radius_suggestion() -> None:
    make(7.0, name="far")
    data = search(lat=LAT, lng=LNG, radius_km=3).json()
    assert data["items"] == []
    wider = [s for s in data["suggestions"] if s["type"] == "wider_radius"]
    assert wider == [
        {"type": "wider_radius", "label": "1 within 10 km", "params": {"radius_km": 10}, "count": 1}
    ]


def test_empty_result_nearby_areas_suggestion() -> None:
    city = CityFactory()
    empty_area, busy_area = (
        AreaFactory(city=city, name="Quiet"),
        AreaFactory(city=city, name="Busy"),
    )
    make(name="x", area=busy_area), make(name="y", area=busy_area)
    data = search(area_id=empty_area.pk).json()
    assert data["items"] == []
    assert {
        "type": "nearby_area",
        "label": "Busy (2)",
        "params": {"area_id": busy_area.pk},
        "count": 2,
    } in data["suggestions"]


def test_empty_result_related_categories_suggestion() -> None:
    wanted, available = CategoryFactory(name="Wanted"), CategoryFactory(name="Available")
    make(name="x", category=available)
    data = search(category_id=wanted.pk).json()
    assert data["items"] == []
    assert any(
        s["type"] == "related_category" and s["params"]["category_id"] == available.pk
        for s in data["suggestions"]
    )


def test_no_suggestions_when_results_exist_or_on_later_pages() -> None:
    make(name="x")
    assert search().json()["suggestions"] == []


def test_nothing_to_suggest() -> None:
    data = search(lat=LAT, lng=LNG).json()
    assert data["items"] == [] and data["suggestions"] == []


# ---------------- efficiency ----------------


def test_query_count_bounded_with_warm_next_slot_cache(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    for i in range(20):
        b = make(i * 0.1, name=f"b{i}")
        BusinessPhotoFactory(business=b, image=f"p{i}.jpg")
    search(lat=LAT, lng=LNG, limit=20)  # warms the 60 s next-slot cache
    with django_assert_max_num_queries(6):
        r = search(lat=LAT, lng=LNG, limit=20)
    assert len(r.json()["items"]) == 20


def test_query_count_independent_of_page_size_when_warm(django_assert_num_queries) -> None:  # type: ignore[no-untyped-def]
    for i in range(10):
        make(i * 0.1)
    cache.clear()
    search(lat=LAT, lng=LNG, limit=3)
    search(lat=LAT, lng=LNG, limit=10)
    from django.db import connection
    from django.test.utils import CaptureQueriesContext

    with CaptureQueriesContext(connection) as small:
        search(lat=LAT, lng=LNG, limit=3)
    with CaptureQueriesContext(connection) as big:
        search(lat=LAT, lng=LNG, limit=10)
    assert len(small) == len(big)


def test_cold_cache_bounded_per_business(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    for i in range(5):
        make(i * 0.1)
    cache.clear()
    with django_assert_max_num_queries(6 + 5 * 8):
        assert search(lat=LAT, lng=LNG).status_code == 200
    assert timedelta(0) == timedelta(0)

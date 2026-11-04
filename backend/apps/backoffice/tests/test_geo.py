import json
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client

from apps.accounts.factories import UserFactory
from apps.accounts.roles import Role
from apps.backoffice.tests.helpers import A, admin_headers, call, headers_for, reauth, user_for
from apps.businesses.factories import BusinessFactory
from apps.businesses.tests.provider_helpers import P, owner_headers
from apps.catalog.factories import CategoryFactory
from apps.core.models import AuditLog
from apps.geo.factories import AreaFactory, CityFactory
from apps.geo.models import Area, City, CityVisibility
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
G = f"{A}/cities"
SQUARE = {
    "type": "Polygon",
    "coordinates": [[[67.0, 24.8], [67.1, 24.8], [67.1, 24.9], [67.0, 24.9], [67.0, 24.8]]],
}


def ops() -> tuple[Any, dict[str, str]]:
    return admin_headers("ops_admin")


def audit(action: str) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action))


# ------------------------------------------------------------ cities


def test_create_list_and_update_city_with_audit() -> None:
    admin, h = ops()
    r = call("post", G, h, {"name": "Karachi", "lat": 24.86, "lng": 67.0, "visibility": "preview"})
    assert r.status_code == 201, r.content
    city = r.json()
    assert (city["slug"], city["visibility"], city["is_active"], city["version"]) == (
        "karachi",
        "preview",
        True,
        1,
    )
    assert (city["lat"], city["lng"], city["area_count"], city["listed_businesses"]) == (
        24.86,
        67.0,
        0,
        0,
    )
    (row,) = audit("admin.city_created")
    assert (
        row.actor_id == admin.pk
        and row.after["slug"] == "karachi"
        and row.before is None
        and row.ip_address
    )
    upd = call(
        "patch",
        f"{G}/{city['id']}",
        h,
        {"version": 1, "name": "Karachi City", "visibility": "public", "lat": 24.9},
    )
    assert upd.status_code == 200 and upd.json()["version"] == 2 and upd.json()["lat"] == 24.9
    assert upd.json()["lng"] == 67.0  # untouched half of the centre
    (u,) = audit("admin.city_updated")
    assert (
        u.before["visibility"] == "preview"
        and u.after["visibility"] == "public"
        and u.before["version"] == 1
    )
    assert [c["name"] for c in call("get", G, h).json()] == ["Karachi City"]


@pytest.mark.parametrize(
    "body,field",
    [
        ({"name": ""}, "name"),
        ({"name": "n" * 101}, "name"),
        ({"lat": 91}, "lat"),
        ({"lat": -91}, "lat"),
        ({"lng": 181}, "lng"),
        ({"lng": "x"}, "lng"),
        ({"visibility": "secret"}, "visibility"),
        ({"slug": "Bad Slug"}, "slug"),
        ({"slug": "s" * 51}, "slug"),
    ],
)
def test_city_validation(body: dict[str, Any], field: str) -> None:
    _, h = ops()
    base = {"name": "Okara", "lat": 30.8, "lng": 73.4}
    r = call("post", G, h, {**base, **body})
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not City.objects.filter(name="Okara").exists() and not audit("admin.city_created")


def test_city_boundaries_accepted_and_slug_rules() -> None:
    _, h = ops()
    edge = call("post", G, h, {"name": "Edge", "slug": "e" * 50, "lat": 90, "lng": -180})
    assert edge.status_code == 201
    first = call("post", G, h, {"name": "Lahore", "lat": 31.5, "lng": 74.3}).json()
    again = call("post", G, h, {"name": "Lahore", "lat": 31.5, "lng": 74.3}).json()
    assert (first["slug"], again["slug"]) == ("lahore", "lahore-2")
    clash = call("post", G, h, {"name": "Other", "slug": "lahore", "lat": 31.5, "lng": 74.3})
    assert clash.status_code == 409 and clash.json()["code"] == "slug_taken"
    patch_clash = call("patch", f"{G}/{again['id']}", h, {"version": 1, "slug": "lahore"})
    assert patch_clash.status_code == 409
    same = call("patch", f"{G}/{first['id']}", h, {"version": 1, "slug": "lahore"})
    assert same.status_code == 200


def test_city_updates_need_the_current_version_and_reject_nulls() -> None:
    _, h = ops()
    city = CityFactory()
    assert call("patch", f"{G}/{city.pk}", h, {"version": 1, "name": "One"}).status_code == 200
    stale = call("patch", f"{G}/{city.pk}", h, {"version": 1, "name": "Two"})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert call("patch", f"{G}/{city.pk}", h, {"name": "x"}).status_code == 422
    assert call("patch", f"{G}/{city.pk}", h, {"version": 2, "name": None}).status_code == 422
    assert call("patch", f"{G}/987654", h, {"version": 1, "name": "x"}).status_code == 404
    city.refresh_from_db()
    assert city.name == "One" and len(audit("admin.city_updated")) == 1


# ------------------------------------------------------------ preview visibility


def listed_in(city: City, **kw: Any) -> Any:
    return BusinessFactory(area=AreaFactory(city=city), **kw)


def search_names(headers: dict[str, str] | None = None) -> list[str]:
    r = Client().get("/api/v1/search/businesses", **(headers or {}))
    assert r.status_code == 200
    return sorted(i["name"] for i in r.json()["items"])


def test_preview_cities_are_visible_to_admins_only_and_hidden_ones_to_nobody() -> None:
    public = listed_in(CityFactory(visibility=CityVisibility.PUBLIC), name="Public Salon")
    preview = listed_in(CityFactory(visibility=CityVisibility.PREVIEW), name="Preview Salon")
    hidden = listed_in(CityFactory(visibility=CityVisibility.HIDDEN), name="Hidden Salon")
    inactive = listed_in(
        CityFactory(visibility=CityVisibility.PUBLIC, is_active=False), name="Dead Salon"
    )
    _, admin_h = admin_headers("support_agent")
    assert search_names() == ["Public Salon"]
    assert search_names(headers_for(user_for("customer"))) == ["Public Salon"]
    assert search_names(headers_for(UserFactory(role=Role.BUSINESS_OWNER))) == ["Public Salon"]
    assert search_names(admin_h) == ["Preview Salon", "Public Salon"]
    for business, visible_to_admin in (
        (public, True),
        (preview, True),
        (hidden, False),
        (inactive, False),
    ):
        anon = Client().get(f"/api/v1/businesses/{business.public_id}").status_code
        adm = Client().get(f"/api/v1/businesses/{business.public_id}", **admin_h).status_code
        assert anon == (200 if business is public else 404)
        assert adm == (200 if visible_to_admin else 404)


def test_hidden_cities_cannot_be_used_for_registration() -> None:
    owner, oh = owner_headers()
    hidden = CityFactory(visibility=CityVisibility.HIDDEN)
    cat = CategoryFactory()
    body = {"name": "Salon", "category_id": cat.pk, "gender_type": "women", "city_id": hidden.pk}
    r = call("post", f"{P}/businesses", oh, body)
    assert r.status_code == 422 and "city_id" in r.json()["fields"]
    _, h = ops()
    call("patch", f"{G}/{hidden.pk}", h, {"version": 1, "visibility": "preview"})
    assert call("post", f"{P}/businesses", oh, body).status_code == 201


# ------------------------------------------------------------ deactivation guard


def test_deactivating_a_city_with_listings_needs_force_and_reauth(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    city = CityFactory(visibility=CityVisibility.PUBLIC)
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001110000", email="o@example.com")
    b = listed_in(city, owner=owner)
    BusinessFactory(area=AreaFactory(city=city), is_listed=False)  # unlisted ones do not count
    blocked = call("patch", f"{G}/{city.pk}", h, {"version": 1, "is_active": False})
    assert blocked.status_code == 409 and blocked.json()["code"] == "has_active_listings"
    assert blocked.json()["fields"] == {"listed_businesses": ["1"]}
    city.refresh_from_db()
    assert city.is_active and not audit("admin.city_updated")
    no_reauth = call(
        "patch", f"{G}/{city.pk}", h, {"version": 1, "is_active": False, "force": True}
    )
    assert no_reauth.status_code == 403 and no_reauth.json()["code"] == "reauth_required"
    reauth(h)
    with run_commit():
        done = call("patch", f"{G}/{city.pk}", h, {"version": 1, "is_active": False, "force": True})
    assert done.status_code == 200 and done.json()["is_active"] is False
    assert Client().get(f"/api/v1/businesses/{b.public_id}").status_code == 404  # hidden at once
    assert (
        Notification.objects.filter(
            user=owner, template_key="onboarding_status_changed", channel="in_app"
        ).count()
        == 1
    )
    (row,) = audit("admin.city_updated")
    assert (
        row.after["hidden_listings"] == 1
        and row.after["forced"] is True
        and row.before["is_active"] is True
    )
    assert (
        call("patch", f"{G}/{city.pk}", h, {"version": 2, "is_active": True}).json()["is_active"]
        is True
    )


def test_deactivating_without_listings_needs_neither_force_nor_reauth() -> None:
    _, h = ops()
    city = CityFactory()
    r = call("patch", f"{G}/{city.pk}", h, {"version": 1, "is_active": False})
    assert r.status_code == 200 and r.json()["is_active"] is False
    again = call("patch", f"{G}/{city.pk}", h, {"version": 2, "is_active": False})
    assert again.status_code == 200  # already inactive: not a switch-off, nothing to guard


# ------------------------------------------------------------ areas


def areas_url(city: City | int, tail: str = "") -> str:
    pk = city.pk if isinstance(city, City) else city
    return f"{G}/{pk}/areas{tail}"


def test_area_crud_with_polygon_and_pilot_flag() -> None:
    admin, h = ops()
    city = CityFactory()
    r = call(
        "post",
        areas_url(city),
        h,
        {"name": "Clifton", "lat": 24.82, "lng": 67.03, "polygon": SQUARE, "is_pilot": True},
    )
    assert r.status_code == 201, r.content
    area = r.json()
    assert (area["slug"], area["is_pilot"], area["version"], area["city_id"]) == (
        "clifton",
        True,
        1,
        city.pk,
    )
    assert area["polygon"]["type"] == "Polygon" and area["polygon"]["coordinates"][0][0] == [
        67.0,
        24.8,
    ]
    (row,) = audit("admin.area_created")
    assert row.actor_id == admin.pk and row.after["has_polygon"] is True
    upd = call(
        "patch",
        areas_url(city, f"/{area['id']}"),
        h,
        {"version": 1, "name": "Clifton Block 5", "is_pilot": False, "polygon": None},
    )
    assert (
        upd.status_code == 200 and upd.json()["polygon"] is None and upd.json()["is_pilot"] is False
    )
    assert upd.json()["version"] == 2
    assert [a["name"] for a in call("get", areas_url(city), h).json()] == ["Clifton Block 5"]
    (changed,) = audit("admin.area_updated")
    assert changed.before["has_polygon"] is True and changed.after["has_polygon"] is False


@pytest.mark.parametrize(
    "polygon",
    [
        {"type": "Point", "coordinates": [67, 24]},
        {"type": "Polygon", "coordinates": []},
        {"type": "Polygon", "coordinates": [[[67, 24], [67.1, 24], [67.1, 24.1]]]},  # not closed
        {
            "type": "Polygon",
            "coordinates": [[[67, 24], [67.1, 24], [67.1, 24.1], [67, 24.1], [67, 24.2]]],
        },
        {"type": "Polygon", "coordinates": [[[200, 24], [200.1, 24], [200.1, 24.1], [200, 24]]]},
        {
            "type": "Polygon",
            "coordinates": [[[67, 24], [67.1, 24.1], [67.1, 24], [67, 24.1], [67, 24]]],
        },  # bow-tie
        {"type": "Polygon"},
        {"type": "Polygon", "coordinates": "nope"},
        {},
    ],
)
def test_invalid_polygons_are_rejected(polygon: dict[str, Any]) -> None:
    _, h = ops()
    city = CityFactory()
    r = call(
        "post", areas_url(city), h, {"name": "Bad", "lat": 24.0, "lng": 67.0, "polygon": polygon}
    )
    assert r.status_code == 422 and "polygon" in r.json()["fields"], r.content
    assert not Area.objects.filter(city=city).exists() and not audit("admin.area_created")


def test_polygon_with_too_many_points_is_rejected() -> None:
    _, h = ops()
    city = CityFactory()
    import math

    ring = [
        [67 + 0.01 * math.cos(i / 5001 * 2 * math.pi), 24 + 0.01 * math.sin(i / 5001 * 2 * math.pi)]
        for i in range(5001)
    ]
    ring.append(ring[0])
    r = call(
        "post",
        areas_url(city),
        h,
        {
            "name": "Big",
            "lat": 24.0,
            "lng": 67.0,
            "polygon": {"type": "Polygon", "coordinates": [ring]},
        },
    )
    assert r.status_code == 422 and "polygon" in r.json()["fields"]


@pytest.mark.parametrize(
    "body,field",
    [
        ({"name": ""}, "name"),
        ({"lat": 95}, "lat"),
        ({"lng": -181}, "lng"),
        ({"slug": "Bad_Slug"}, "slug"),
    ],
)
def test_area_validation_and_missing_city(body: dict[str, Any], field: str) -> None:
    _, h = ops()
    city = CityFactory()
    r = call("post", areas_url(city), h, {"name": "A", "lat": 24.0, "lng": 67.0, **body})
    assert r.status_code == 422 and field in r.json()["fields"]
    ok = {"name": "A", "lat": 24.0, "lng": 67.0}
    assert call("post", areas_url(987654), h, ok).status_code == 404
    assert call("get", areas_url(987654), h).status_code == 404


def test_area_slugs_are_unique_per_city_and_versions_are_enforced() -> None:
    _, h = ops()
    one, two = CityFactory(), CityFactory()
    a = call("post", areas_url(one), h, {"name": "Saddar", "lat": 24.8, "lng": 67.0}).json()
    other_city = call("post", areas_url(two), h, {"name": "Saddar", "lat": 24.8, "lng": 67.0})
    assert (
        other_city.status_code == 201 and other_city.json()["slug"] == "saddar"
    )  # same slug, other city
    dup = call(
        "post", areas_url(one), h, {"name": "Other", "slug": "saddar", "lat": 24.8, "lng": 67.0}
    )
    assert dup.status_code == 409 and dup.json()["code"] == "slug_taken"
    auto = call("post", areas_url(one), h, {"name": "Saddar", "lat": 24.8, "lng": 67.0}).json()
    assert auto["slug"] == "saddar-2"
    assert (
        call("patch", areas_url(one, f"/{a['id']}"), h, {"version": 9, "name": "x"}).json()["code"]
        == "stale_version"
    )
    foreign = call(
        "patch", areas_url(one, f"/{other_city.json()['id']}"), h, {"version": 1, "name": "x"}
    )
    assert foreign.status_code == 404  # an area of another city is not addressable here


def test_deactivating_an_area_with_listings_is_guarded(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    city = CityFactory(visibility=CityVisibility.PUBLIC)
    area = AreaFactory(city=city)
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    b = BusinessFactory(area=area, city=city, owner=owner)
    url = areas_url(city, f"/{area.pk}")
    blocked = call("patch", url, h, {"version": 1, "is_active": False})
    assert blocked.status_code == 409 and blocked.json()["code"] == "has_active_listings"
    assert (
        call("patch", url, h, {"version": 1, "is_active": False, "force": True}).json()["code"]
        == "reauth_required"
    )
    reauth(h)
    with run_commit():
        done = call("patch", url, h, {"version": 1, "is_active": False, "force": True})
    assert done.status_code == 200 and done.json()["is_active"] is False
    (row,) = audit("admin.area_updated")
    assert row.after["hidden_listings"] == 1
    assert Notification.objects.filter(
        user=owner, template_key="onboarding_status_changed"
    ).exists()
    assert b


# ------------------------------------------------------------ CSV import


def upload(
    h: dict[str, str], city: City | int, content: bytes | str, name: str = "areas.csv"
) -> Any:
    data = content.encode() if isinstance(content, str) else content
    return Client().post(
        areas_url(city, "/import"),
        {"file": SimpleUploadedFile(name, data, content_type="text/csv")},
        **h,
    )


HEADER = "name,slug,lat,lng,is_pilot\n"


def test_csv_import_all_rows_valid() -> None:
    admin, h = ops()
    city = CityFactory()
    AreaFactory(city=city, slug="existing")
    csv_text = (
        HEADER
        + "DHA,dha,24.80,67.06,true\nKorangi,korangi,24.83,67.12,\n"
        + "Nazimabad,nazimabad,24.92,67.03,no\n"
    )
    r = upload(h, city, csv_text)
    assert r.status_code == 200, r.content
    assert r.json() == {"imported": 3}
    created = {a.slug: a for a in Area.objects.filter(city=city)}
    assert set(created) == {"existing", "dha", "korangi", "nazimabad"}
    assert created["dha"].is_pilot is True and created["korangi"].is_pilot is False
    assert (round(created["dha"].centre.y, 2), round(created["dha"].centre.x, 2)) == (24.80, 67.06)
    (row,) = audit("admin.areas_imported")
    assert (
        row.actor_id == admin.pk
        and row.after == {"areas": 4, "imported": 3}
        and row.before == {"areas": 1}
    )


def test_csv_import_with_a_bad_row_imports_nothing_and_names_the_rows() -> None:
    _, h = ops()
    city = CityFactory()
    AreaFactory(city=city, slug="taken")
    csv_text = (
        HEADER
        + "Good,good,24.8,67.0,true\n"  # row 2 fine
        + ",no-name,24.8,67.0,false\n"  # row 3: name
        + "Bad Slug,Bad Slug,24.8,67.0,false\n"  # row 4: slug
        + "Dup A,dup,24.8,67.0,false\n"  # row 5 ok
        + "Dup B,dup,24.8,67.0,false\n"  # row 6: repeats row 5
        + "Taken,taken,24.8,67.0,false\n"  # row 7: exists in the city
        + "Coords,coords,95,67.0,false\n"  # row 8: lat
        + "Flag,flag,24.8,67.0,maybe\n"  # row 9: flag
        + "NoNumbers,nonum,abc,def,false\n"  # row 10
    )
    r = upload(h, city, csv_text)
    assert r.status_code == 422 and r.json()["code"] == "csv_invalid"
    fields = r.json()["fields"]
    assert set(fields) == {"row 3", "row 4", "row 6", "row 7", "row 8", "row 9", "row 10"}
    assert "repeats row 5" in fields["row 6"][0] and "already exists" in fields["row 7"][0]
    assert Area.objects.filter(city=city).count() == 1  # only the pre-existing one
    assert not audit("admin.areas_imported")


@pytest.mark.parametrize(
    "content,code,status",
    [
        ("name,slug,lat\nA,a,24.8\n", "invalid_csv", 422),  # missing columns
        (HEADER, "invalid_csv", 422),  # no rows
        (b"\xff\xfe\x00binary", "invalid_csv", 422),  # not UTF-8 text
        ("x" * (1024 * 1024 + 1), "file_too_large", 413),
    ],
)
def test_csv_import_rejects_unusable_files(content: Any, code: str, status: int) -> None:
    _, h = ops()
    city = CityFactory()
    r = upload(h, city, content)
    assert r.status_code == status and r.json()["code"] == code
    assert not Area.objects.filter(city=city).exists()


def test_csv_import_limits_bom_and_throttle() -> None:
    _, h = ops()
    city = CityFactory()
    many = HEADER + "".join(f"A{i},a{i},24.8,67.0,false\n" for i in range(2000))
    assert upload(h, city, many).json() == {"imported": 2000}
    over = HEADER + "".join(f"B{i},b{i},24.8,67.0,false\n" for i in range(2001))
    assert upload(h, city, over).status_code == 422 and not Area.objects.filter(slug="b0").exists()
    bom = upload(h, CityFactory(), "﻿" + HEADER + "BOM,bom,24.8,67.0,true\n")
    assert bom.status_code == 200 and bom.json() == {"imported": 1}
    assert call("post", areas_url(987654, "/import"), h, None).status_code in (404, 422)
    codes = [
        upload(h, CityFactory(), HEADER + "X,x,24.8,67.0,false\n").status_code for _ in range(10)
    ]
    assert codes[-1] == 429


def test_csv_import_needs_geo_manage() -> None:
    _, h = admin_headers("support_agent")
    city = CityFactory()
    assert upload(h, city, HEADER + "A,a,24.8,67.0,false\n").status_code == 403
    assert upload({}, city, "x").status_code == 401
    assert not Area.objects.filter(city=city).exists()


def test_json_helpers_roundtrip() -> None:
    assert json.loads(json.dumps(SQUARE))["type"] == "Polygon"

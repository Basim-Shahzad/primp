import io
from datetime import timedelta
from typing import Any
from unittest import mock

import pytest
import time_machine
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone
from PIL import Image
from redis.exceptions import RedisError

from apps.backoffice import banners, tasks
from apps.backoffice.models import Banner
from apps.backoffice.tests.helpers import A, admin_headers, call
from apps.businesses.tests.provider_helpers import png_bytes
from apps.core.models import AuditLog
from apps.geo.factories import CityFactory

pytestmark = pytest.mark.django_db
B = f"{A}/banners"
PUBLIC = "/api/v1/banners"


def ops() -> tuple[Any, dict[str, str]]:
    return admin_headers("ops_admin")


def iso(dt: Any) -> str:
    return dt.isoformat()


def body(**kw: Any) -> dict[str, Any]:
    now = timezone.now()
    return {
        "title": "Eid sale",
        "subtitle": "Up to 30% off",
        "link_url": "/offers/eid",
        "starts_at": iso(now - timedelta(days=1)),
        "ends_at": iso(now + timedelta(days=1)),
        **kw,
    }


def audit(action: str) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action))


def jpeg(size: tuple[int, int]) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, format="JPEG")
    return out.getvalue()


def upload(h: dict[str, str], banner_id: int, kind: str, data: bytes) -> Any:
    file = SimpleUploadedFile("b.jpg", data, content_type="image/jpeg")
    return Client().post(f"{B}/{banner_id}/images/{kind}", {"file": file}, **h)


def live_banner(h: dict[str, str], **kw: Any) -> dict[str, Any]:
    """Create, give both images and enable: a banner the public feed can show."""
    created = call("post", B, h, body(**kw)).json()
    assert upload(h, created["id"], "desktop", png_bytes((900, 300))).status_code == 200
    assert upload(h, created["id"], "mobile", png_bytes((300, 300))).status_code == 200
    version = Banner.objects.get(pk=created["id"]).version
    r = call("patch", f"{B}/{created['id']}", h, {"version": version, "is_enabled": True})
    assert r.status_code == 200, r.content
    return dict(r.json())


def feed(city: int | None = None) -> list[str]:
    qs = f"?city_id={city}" if city else ""
    r = Client().get(f"{PUBLIC}{qs}")
    assert r.status_code == 200
    return [b["title"] for b in r.json()]


# ------------------------------------------------------------ create / validate


def test_create_starts_disabled_without_images_and_is_audited() -> None:
    admin, h = ops()
    r = call("post", B, h, body())
    assert r.status_code == 201, r.content
    d = r.json()
    assert d["is_enabled"] is False and d["status"] == "disabled" and d["version"] == 1
    assert d["desktop_image_url"] is None and (d["impressions"], d["clicks"]) == (0, 0)
    assert (
        d["sort_order"] == 0 and call("post", B, h, body(title="Second")).json()["sort_order"] == 1
    )
    (row, _second) = audit("admin.banner_created")
    assert (
        row.actor_id == admin.pk
        and row.before is None
        and row.after["title"] == "Eid sale"
        and row.ip_address
    )


@pytest.mark.parametrize(
    "link",
    ["https://primp.pk/offers", "/offers", "/offers?x=1#top", "https://sub.example.com/a/b?c=d"],
)
def test_valid_links(link: str) -> None:
    _, h = ops()
    assert call("post", B, h, body(link_url=link)).status_code == 201


@pytest.mark.parametrize(
    "link",
    [
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "data:text/html,<script>1</script>",
        "vbscript:x",
        "http://insecure.example.com",
        "//evil.example.com/x",
        "/\\evil.example.com",
        "ftp://x.example.com",
        "https://user:pw@example.com/",
        "https:///nohost",
        "offers/no-leading-slash",
        "/has space",
        "https://example.com/\nX",
        "x" * 501,
        "",
    ],
)
def test_dangerous_or_malformed_links_are_rejected(link: str) -> None:
    _, h = ops()
    r = call("post", B, h, body(link_url=link))
    assert r.status_code == 422 and "link_url" in r.json()["fields"], link
    assert not Banner.objects.exists()


@pytest.mark.parametrize(
    "extra,field",
    [
        ({"title": ""}, "title"),
        ({"title": "t" * 121}, "title"),
        ({"subtitle": "s" * 201}, "subtitle"),
        ({"starts_at": "2030-01-01T00:00:00"}, "starts_at"),
        ({"ends_at": "nope"}, "ends_at"),
    ],
)
def test_create_validation(extra: dict[str, Any], field: str) -> None:
    _, h = ops()
    r = call("post", B, h, body(**extra))
    assert r.status_code == 422 and field in r.json()["fields"]


def test_schedule_and_city_rules_and_boundaries() -> None:
    _, h = ops()
    now = timezone.now()
    same = call("post", B, h, body(starts_at=iso(now), ends_at=iso(now)))
    assert same.status_code == 422 and "ends_at" in same.json()["fields"]
    assert (
        call(
            "post", B, h, body(starts_at=iso(now), ends_at=iso(now - timedelta(hours=1)))
        ).status_code
        == 422
    )
    assert (
        call(
            "post", B, h, body(starts_at=iso(now), ends_at=iso(now + timedelta(seconds=1)))
        ).status_code
        == 201
    )
    assert call("post", B, h, body(city_id=987654)).status_code == 404
    city = CityFactory()
    assert call("post", B, h, body(city_id=city.pk)).json()["city_id"] == city.pk
    assert call("post", B, h, body(title="t" * 120, subtitle="s" * 200)).status_code == 201


# ------------------------------------------------------------ images


def test_images_are_validated_resized_and_replaced(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    banner = call("post", B, h, body()).json()
    r = upload(h, banner["id"], "desktop", jpeg((3000, 1000)))
    assert r.status_code == 200 and r.json()["desktop_image_url"] and r.json()["version"] == 2
    row = Banner.objects.get(pk=banner["id"])
    with default_storage.open(row.desktop_image) as f:
        assert max(Image.open(f).size) == 1600
    upload(h, banner["id"], "mobile", jpeg((2000, 2000)))
    row.refresh_from_db()
    with default_storage.open(row.mobile_image) as f:
        assert max(Image.open(f).size) == 800
    old = row.desktop_image
    with run_commit():
        upload(h, banner["id"], "desktop", jpeg((800, 400)))
    row.refresh_from_db()
    assert row.desktop_image != old and not default_storage.exists(old)
    assert len(audit("admin.banner_image_set")) == 3
    assert audit("admin.banner_image_set")[0].actor_id == admin.pk
    assert upload(h, banner["id"], "desktop", b"not an image").json()["code"] == "invalid_image"
    assert upload(h, banner["id"], "tablet", jpeg((10, 10))).status_code == 422
    assert upload(h, 987654, "desktop", jpeg((10, 10))).status_code == 404
    assert Client().post(f"{B}/{banner['id']}/images/desktop", {}, **h).status_code == 422


def test_a_banner_cannot_be_enabled_without_both_images() -> None:
    _, h = ops()
    banner = call("post", B, h, body()).json()
    r = call("patch", f"{B}/{banner['id']}", h, {"version": 1, "is_enabled": True})
    assert r.status_code == 422 and r.json()["code"] == "images_required"
    upload(h, banner["id"], "desktop", jpeg((100, 50)))
    r = call("patch", f"{B}/{banner['id']}", h, {"version": 2, "is_enabled": True})
    assert r.status_code == 422
    upload(h, banner["id"], "mobile", jpeg((100, 50)))
    ok = call("patch", f"{B}/{banner['id']}", h, {"version": 3, "is_enabled": True})
    assert (
        ok.status_code == 200
        and ok.json()["is_enabled"] is True
        and ok.json()["status"] == "active"
    )
    assert Banner.objects.get(pk=banner["id"]).version == 4


# ------------------------------------------------------------ update / delete / reorder


def test_update_versions_nulls_and_audit() -> None:
    admin, h = ops()
    b = call("post", B, h, body()).json()
    r = call(
        "patch",
        f"{B}/{b['id']}",
        h,
        {"version": 1, "title": "New title", "link_url": "https://primp.pk/x"},
    )
    assert r.status_code == 200 and r.json()["title"] == "New title" and r.json()["version"] == 2
    (row,) = audit("admin.banner_updated")
    assert (
        row.actor_id == admin.pk
        and row.before["title"] == "Eid sale"
        and row.after["title"] == "New title"
    )
    stale = call("patch", f"{B}/{b['id']}", h, {"version": 1, "title": "x"})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    assert call("patch", f"{B}/{b['id']}", h, {"version": 2, "title": None}).status_code == 422
    assert (
        call("patch", f"{B}/{b['id']}", h, {"version": 2, "link_url": "javascript:x"}).status_code
        == 422
    )
    now = timezone.now()
    assert (
        call(
            "patch", f"{B}/{b['id']}", h, {"version": 2, "ends_at": iso(now - timedelta(days=30))}
        ).status_code
        == 422
    )
    assert call("patch", f"{B}/{b['id']}", h, {"version": 2, "city_id": 987654}).status_code == 404
    assert call("patch", f"{B}/{b['id']}", h, {"title": "x"}).status_code == 422
    assert call("patch", f"{B}/987654", h, {"version": 1}).status_code == 404
    assert len(audit("admin.banner_updated")) == 1


def test_delete_needs_the_version_and_removes_the_images(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    b = live_banner(h)
    row = Banner.objects.get(pk=b["id"])
    paths = [row.desktop_image, row.mobile_image]
    stale = call("delete", f"{B}/{b['id']}?version=1", h)
    assert stale.status_code == 409 and Banner.objects.filter(pk=b["id"]).exists()
    with run_commit():
        done = call("delete", f"{B}/{b['id']}?version={row.version}", h)
    assert done.status_code == 204 and not Banner.objects.filter(pk=b["id"]).exists()
    assert not any(default_storage.exists(p) for p in paths)
    (a,) = audit("admin.banner_deleted")
    assert a.actor_id == admin.pk and a.before["title"] == "Eid sale"
    assert call("delete", f"{B}/{b['id']}?version=1", h).status_code == 404
    assert call("delete", f"{B}/{b['id']}", h).status_code == 422


def test_reorder_is_a_full_permutation_and_drives_the_public_order() -> None:
    admin, h = ops()
    first, second, third = (live_banner(h, title=t) for t in ("One", "Two", "Three"))
    assert feed() == ["One", "Two", "Three"]
    ids = [third["id"], first["id"], second["id"]]
    r = call("post", f"{B}/reorder", h, {"ids": ids})
    assert r.status_code == 200 and [x["id"] for x in r.json()] == ids
    assert feed() == ["Three", "One", "Two"]
    (row,) = audit("admin.banners_reordered")
    assert row.actor_id == admin.pk and row.after == {"ids": ids}
    for bad in ([first["id"]], [first["id"], first["id"], second["id"]], [*ids, 987654], []):
        assert call("post", f"{B}/reorder", h, {"ids": bad}).status_code == 422
    assert feed() == ["Three", "One", "Two"]


def test_admin_list_filters_by_status_and_city() -> None:
    _, h = ops()
    now = timezone.now()
    city = CityFactory()
    active = live_banner(h, title="Active")
    scheduled = call(
        "post",
        B,
        h,
        body(
            title="Soon",
            starts_at=iso(now + timedelta(days=1)),
            ends_at=iso(now + timedelta(days=2)),
            city_id=city.pk,
        ),
    ).json()
    Banner.objects.create(
        title="Old",
        link_url="/x",
        starts_at=now - timedelta(days=3),
        ends_at=now - timedelta(days=2),
        is_enabled=True,
    )
    ids = lambda qs: sorted(x["title"] for x in call("get", f"{B}{qs}", h).json())  # noqa: E731
    assert ids("") == ["Active", "Old", "Soon"]
    assert ids("?status=active") == ["Active"]
    assert ids("?status=scheduled") == ["Soon"]
    assert ids("?status=expired") == ["Old"]
    assert ids("?status=disabled") == ["Soon"]
    assert ids(f"?city_id={city.pk}") == ["Soon"]
    assert call("get", f"{B}?status=weird", h).status_code == 422
    assert active and scheduled


# ------------------------------------------------------------ public feed


def test_schedule_boundaries_are_exact_at_start_and_end() -> None:
    _, h = ops()
    start = timezone.now().replace(microsecond=0) + timedelta(hours=1)
    end = start + timedelta(hours=2)
    live_banner(h, title="Timed", starts_at=iso(start), ends_at=iso(end))
    with time_machine.travel(start - timedelta(microseconds=1), tick=False):
        assert feed() == []
    with time_machine.travel(start, tick=False):
        assert feed() == ["Timed"]  # starts_at is inclusive
    with time_machine.travel(end - timedelta(microseconds=1), tick=False):
        assert feed() == ["Timed"]
    with time_machine.travel(end, tick=False):
        assert feed() == []  # ends_at is exclusive


def test_city_targeting_disabled_and_imageless_banners() -> None:
    _, h = ops()
    karachi, lahore = CityFactory(), CityFactory()
    live_banner(h, title="Everywhere")
    live_banner(h, title="Karachi only", city_id=karachi.pk)
    live_banner(h, title="Lahore only", city_id=lahore.pk)
    off = live_banner(h, title="Switched off")
    call(
        "patch",
        f"{B}/{off['id']}",
        h,
        {"version": Banner.objects.get(pk=off["id"]).version, "is_enabled": False},
    )
    call("post", B, h, body(title="No images"))
    assert feed() == ["Everywhere"]  # no city: only the all-city ones
    assert feed(karachi.pk) == ["Everywhere", "Karachi only"]
    assert feed(lahore.pk) == ["Everywhere", "Lahore only"]
    assert feed(987654) == ["Everywhere"]
    assert Client().get(f"{PUBLIC}?city_id=abc").status_code == 422


def test_feed_payload_has_urls_and_no_internal_fields() -> None:
    _, h = ops()
    live_banner(h, title="Shape", link_url="https://primp.pk/x")
    item = Client().get(PUBLIC).json()[0]
    assert set(item) == {
        "id",
        "title",
        "subtitle",
        "desktop_image_url",
        "mobile_image_url",
        "link_url",
    }
    assert (
        item["link_url"] == "https://primp.pk/x"
        and item["desktop_image_url"] != item["mobile_image_url"]
    )


def test_feed_is_cached_and_invalidated_by_every_change(django_assert_num_queries) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    b = live_banner(h, title="Cached")
    assert feed() == ["Cached"]
    with django_assert_num_queries(0):
        assert Client().get(PUBLIC).status_code == 200  # served from the cache
    call(
        "patch",
        f"{B}/{b['id']}",
        h,
        {"version": Banner.objects.get(pk=b["id"]).version, "title": "Renamed"},
    )
    assert feed() == ["Renamed"]
    call(
        "patch",
        f"{B}/{b['id']}",
        h,
        {"version": Banner.objects.get(pk=b["id"]).version, "is_enabled": False},
    )
    assert feed() == []
    live_banner(h, title="Fresh")
    assert feed() == ["Fresh"]
    upload(h, Banner.objects.get(title="Fresh").pk, "desktop", png_bytes((10, 10)))
    assert Client().get(PUBLIC).json()[0]["desktop_image_url"]


# ------------------------------------------------------------ tracking


def counters(banner_id: int) -> tuple[int, int]:
    return banners.pending_counts(banner_id)


def track(banner_id: int, kind: str, ip: str = "203.0.113.5") -> Any:
    return Client().post(f"{PUBLIC}/{banner_id}/{kind}", REMOTE_ADDR=ip)


def test_impressions_and_clicks_count_in_redis_then_flush_to_the_database() -> None:
    _, h = ops()
    b = live_banner(h)
    for _i in range(3):
        assert track(b["id"], "impression").status_code == 204
    assert track(b["id"], "click").status_code == 204
    assert counters(b["id"]) == (3, 1)
    row = Banner.objects.get(pk=b["id"])
    assert (row.impressions, row.clicks) == (0, 0)  # nothing in the database yet
    listing = call("get", B, h).json()[0]
    assert (listing["impressions"], listing["clicks"]) == (
        3,
        1,
    )  # the admin sees pending counts too
    assert tasks.flush_banner_counters() == 4
    row.refresh_from_db()
    assert (row.impressions, row.clicks) == (3, 1) and counters(b["id"]) == (0, 0)
    assert tasks.flush_banner_counters() == 0  # idempotent: nothing is counted twice
    row.refresh_from_db()
    assert (row.impressions, row.clicks) == (3, 1)
    track(b["id"], "click")
    assert tasks.flush_banner_counters() == 1
    row.refresh_from_db()
    assert (row.impressions, row.clicks) == (3, 2)
    assert call("get", B, h).json()[0]["clicks"] == 2


def test_only_live_banners_can_be_tracked() -> None:
    _, h = ops()
    now = timezone.now()
    off = live_banner(h, title="Off")
    call(
        "patch",
        f"{B}/{off['id']}",
        h,
        {"version": Banner.objects.get(pk=off["id"]).version, "is_enabled": False},
    )
    future = Banner.objects.create(
        title="F",
        link_url="/x",
        is_enabled=True,
        starts_at=now + timedelta(days=1),
        ends_at=now + timedelta(days=2),
    )
    for banner_id in (off["id"], future.pk, 987654):
        for kind in ("impression", "click"):
            assert track(banner_id, kind).status_code == 404
    assert counters(off["id"]) == (0, 0) and counters(future.pk) == (0, 0)
    assert track(off["id"], "view").status_code == 404


def test_tracking_is_throttled_per_ip_banner_and_kind() -> None:
    _, h = ops()
    one, two = live_banner(h, title="One"), live_banner(h, title="Two")
    codes = [track(one["id"], "impression").status_code for _i in range(31)]
    assert codes[:30] == [204] * 30 and codes[30] == 429
    assert track(one["id"], "click").status_code == 204  # clicks have their own allowance
    assert track(two["id"], "impression").status_code == 204  # so does another banner
    assert track(one["id"], "impression", ip="198.51.100.9").status_code == 204  # and another IP
    assert counters(one["id"])[0] == 31


def test_a_failed_flush_puts_the_counts_back() -> None:
    _, h = ops()
    b = live_banner(h)
    track(b["id"], "impression")
    track(b["id"], "click")
    with (
        mock.patch(
            "apps.backoffice.banners.Banner.objects.filter", side_effect=RuntimeError("db down")
        ),
        pytest.raises(RuntimeError),
    ):
        banners.flush_counters()
    assert counters(b["id"]) == (1, 1)
    assert banners.flush_counters() == 2
    row = Banner.objects.get(pk=b["id"])
    assert (row.impressions, row.clicks) == (1, 1)


def test_tracking_falls_back_to_the_database_when_redis_is_down() -> None:
    _, h = ops()
    b = live_banner(h)
    with mock.patch("apps.backoffice.banners.get_redis_connection", side_effect=RedisError("down")):
        assert track(b["id"], "impression").status_code == 204
        assert counters(b["id"]) == (0, 0)
    row = Banner.objects.get(pk=b["id"])
    assert row.impressions == 1


def test_permissions() -> None:
    _, support = admin_headers("support_agent")
    assert call("get", B, support).status_code == 403
    assert call("post", B, support, body()).status_code == 403
    assert call("get", B, {}).status_code == 401
    assert not Banner.objects.exists()

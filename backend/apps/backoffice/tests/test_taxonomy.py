import threading
from typing import Any

import pytest
from django.db import connection
from django.test import Client

from apps.backoffice.tests.helpers import A, admin_headers, call
from apps.businesses.factories import BusinessFactory, ServiceFactory
from apps.catalog.factories import CategoryFactory
from apps.catalog.models import Category
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db
C = f"{A}/categories"
PUBLIC = "/api/v1/categories"


def ops() -> tuple[Any, dict[str, str]]:
    return admin_headers("ops_admin")


def make(name: str, parent: Category | None = None, **kw: Any) -> Category:
    return CategoryFactory(name=name, slug=name.lower().replace(" ", "-"), parent=parent, **kw)


def audit(action: str) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action))


def public() -> list[dict[str, Any]]:
    r = Client().get(PUBLIC)
    assert r.status_code == 200
    return list(r.json())


# ------------------------------------------------------------ tree and usage


def test_tree_lists_inactive_nodes_and_usage_counts() -> None:
    _, h = ops()
    hair = make("Hair")
    cut = make("Haircut", hair)
    off = make("Retired", is_active=False)
    BusinessFactory(category=hair)
    BusinessFactory(category=hair)
    ServiceFactory(category=cut)
    ServiceFactory(category=cut)
    ServiceFactory(category=cut)
    r = call("get", C, h)
    assert r.status_code == 200, r.content
    tree = {c["name"]: c for c in r.json()}
    assert {"Hair", "Retired"} <= set(tree) and tree["Retired"]["is_active"] is False and off
    assert (tree["Hair"]["businesses_count"], tree["Hair"]["services_count"]) == (2, 0)
    (child,) = tree["Hair"]["children"]
    assert child["name"] == "Haircut" and (child["services_count"], child["businesses_count"]) == (
        3,
        0,
    )
    assert child["parent_id"] == hair.pk and child["version"] == 1


def test_tree_query_count_is_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    for i in range(10):
        parent = make(f"Top {i}")
        for j in range(3):
            make(f"Child {i}-{j}", parent)
    with django_assert_max_num_queries(8):
        assert call("get", C, h).status_code == 200


# ------------------------------------------------------------ create


def test_create_top_level_and_child_with_audit() -> None:
    admin, h = ops()
    r = call(
        "post",
        C,
        h,
        {"name": "Hair & Beauty", "icon": "scissors", "description": "All things hair"},
    )
    assert r.status_code == 201, r.content
    top = r.json()
    assert top["slug"] == "hair-beauty" and top["sort_order"] == 0 and top["version"] == 1
    assert top["parent_id"] is None and top["is_active"] is True
    child = call(
        "post", C, h, {"name": "Bridal", "slug": "bridal-hair", "parent_id": top["id"]}
    ).json()
    assert (
        child["slug"] == "bridal-hair"
        and child["parent_id"] == top["id"]
        and child["sort_order"] == 0
    )
    second = call("post", C, h, {"name": "Colour", "parent_id": top["id"]}).json()
    assert second["sort_order"] == 1
    rows = audit("admin.category_created")
    assert len(rows) == 3
    assert (
        rows[0].actor_id == admin.pk
        and rows[0].target_id == str(top["id"])
        and rows[0].before is None
    )
    assert rows[0].after["slug"] == "hair-beauty" and rows[0].ip_address


def test_slug_is_generated_uniquely_and_collisions_are_rejected() -> None:
    _, h = ops()
    first = call("post", C, h, {"name": "Nails"}).json()
    second = call("post", C, h, {"name": "Nails"}).json()
    assert (first["slug"], second["slug"]) == ("nails", "nails-2")
    clash = call("post", C, h, {"name": "Other", "slug": "nails"})
    assert clash.status_code == 409 and clash.json()["code"] == "slug_taken"
    assert Category.objects.filter(name="Other").count() == 0
    assert len(audit("admin.category_created")) == 2


@pytest.mark.parametrize(
    "body,field",
    [
        ({"name": ""}, "name"),
        ({"name": "   "}, "name"),
        ({"name": "n" * 101}, "name"),
        ({"name": "ok", "slug": "Has Spaces"}, "slug"),
        ({"name": "ok", "slug": "under_score"}, "slug"),
        ({"name": "ok", "slug": "s" * 51}, "slug"),
        ({"name": "ok", "slug": "-lead"}, "slug"),
        ({"name": "ok", "icon": "i" * 101}, "icon"),
        ({"name": "ok", "description": "d" * 301}, "description"),
        ({"name": "ok", "parent_id": 987654}, "parent_id"),
        ({"name": "ok", "parent_id": "x"}, "parent_id"),
    ],
)
def test_create_validation(body: dict[str, Any], field: str) -> None:
    _, h = ops()
    r = call("post", C, h, body)
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not Category.objects.exists() and not audit("admin.category_created")


def test_create_boundaries_ok_and_depth_limit() -> None:
    _, h = ops()
    top = call(
        "post",
        C,
        h,
        {"name": "n" * 100, "slug": "s" * 50, "icon": "i" * 100, "description": "d" * 300},
    )
    assert top.status_code == 201
    child = call("post", C, h, {"name": "Child", "parent_id": top.json()["id"]}).json()
    deeper = call("post", C, h, {"name": "Grandchild", "parent_id": child["id"]})
    assert deeper.status_code == 422 and "parent_id" in deeper.json()["fields"]


# ------------------------------------------------------------ update


def patch(h: dict[str, str], c: Category | dict[str, Any], **body: Any) -> Any:
    pk = c.pk if isinstance(c, Category) else c["id"]
    version = Category.objects.get(pk=pk).version
    return call("patch", f"{C}/{pk}", h, {"version": version, **body})


def test_update_fields_bumps_version_and_audits_before_after() -> None:
    admin, h = ops()
    c = make("Skin")
    r = patch(h, c, name="Skincare", slug="skincare", icon="drop", description="Facials and more")
    assert r.status_code == 200, r.content
    assert r.json()["version"] == 2 and r.json()["slug"] == "skincare"
    c.refresh_from_db()
    assert (c.name, c.icon, c.description, c.version) == ("Skincare", "drop", "Facials and more", 2)
    (row,) = audit("admin.category_updated")
    assert (
        row.actor_id == admin.pk
        and row.before["name"] == "Skin"
        and row.after["name"] == "Skincare"
    )
    assert row.before["version"] == 1 and row.after["version"] == 2 and row.ip_address


def test_stale_version_conflicts() -> None:
    _, h = ops()
    c = make("Skin")
    assert patch(h, c, name="One").status_code == 200
    stale = call("patch", f"{C}/{c.pk}", h, {"version": 1, "name": "Two"})
    assert stale.status_code == 409 and stale.json()["code"] == "stale_version"
    c.refresh_from_db()
    assert c.name == "One" and len(audit("admin.category_updated")) == 1
    assert call("patch", f"{C}/{c.pk}", h, {"name": "x"}).status_code == 422
    assert call("patch", f"{C}/{c.pk}", h, {"version": 0, "name": "x"}).status_code == 422


def test_update_validation_and_missing() -> None:
    _, h = ops()
    a, b = make("A cat"), make("B cat")
    assert patch(h, a, name=None).status_code == 422
    assert patch(h, a, slug="B cat").status_code == 422
    clash = patch(h, a, slug=b.slug)
    assert clash.status_code == 409 and clash.json()["code"] == "slug_taken"
    assert patch(h, a, slug=a.slug).status_code == 200  # keeping its own slug is fine
    assert call("patch", f"{C}/987654", h, {"version": 1, "name": "x"}).status_code == 404
    assert patch(h, a, name="n" * 101).status_code == 422


def test_reparenting_rules_prevent_cycles_and_depth() -> None:
    _, h = ops()
    top, other = make("Top"), make("Other top")
    child = make("Child", top)
    grand_try = make("Loose")
    own = patch(h, top, parent_id=top.pk)
    assert own.status_code == 422 and "parent_id" in own.json()["fields"]
    cycle = patch(h, top, parent_id=child.pk)  # a category cannot move under its own child
    assert cycle.status_code == 422
    nested_with_children = patch(h, top, parent_id=other.pk)  # top still has a child
    assert nested_with_children.status_code == 422
    too_deep = patch(h, grand_try, parent_id=child.pk)  # child is already level two
    assert too_deep.status_code == 422
    moved = patch(h, child, parent_id=other.pk)
    assert moved.status_code == 200 and moved.json()["parent_id"] == other.pk
    promoted = patch(h, child, parent_id=None)
    assert promoted.status_code == 200 and promoted.json()["parent_id"] is None
    ok = patch(h, grand_try, parent_id=top.pk)
    assert ok.status_code == 200
    unknown = patch(h, grand_try, parent_id=987654)
    assert unknown.status_code == 422
    top.refresh_from_db()
    assert top.parent_id is None


# ------------------------------------------------------------ reorder


def test_reorder_siblings_with_audit_and_public_effect() -> None:
    admin, h = ops()
    a, b, c = make("Aa"), make("Bb"), make("Cc")
    for i, cat in enumerate((a, b, c)):
        Category.objects.filter(pk=cat.pk).update(sort_order=i)
    assert [x["name"] for x in public()] == ["Aa", "Bb", "Cc"]  # warms the cache
    r = call("post", f"{C}/reorder", h, {"parent_id": None, "ids": [c.pk, a.pk, b.pk]})
    assert r.status_code == 200 and [x["name"] for x in r.json()] == ["Cc", "Aa", "Bb"]
    assert [x["name"] for x in public()] == ["Cc", "Aa", "Bb"]
    (row,) = audit("admin.categories_reordered")
    assert row.actor_id == admin.pk and row.before == {"ids": [a.pk, b.pk, c.pk]}
    assert row.after == {"ids": [c.pk, a.pk, b.pk]}
    kids = [make("K1", a), make("K2", a)]
    for i, k in enumerate(kids):
        Category.objects.filter(pk=k.pk).update(sort_order=i)
    swap = call("post", f"{C}/reorder", h, {"parent_id": a.pk, "ids": [kids[1].pk, kids[0].pk]})
    assert swap.status_code == 200
    assert [x["name"] for x in public()[1]["children"]] == ["K2", "K1"]


def test_reorder_rejects_missing_duplicate_and_foreign_ids() -> None:
    _, h = ops()
    a, b = make("Aa"), make("Bb")
    child = make("Kid", a)
    outsider = make("Zz")
    for ids in ([a.pk], [a.pk, a.pk, b.pk], [a.pk, b.pk, child.pk], [a.pk, b.pk, 987654], []):
        r = call("post", f"{C}/reorder", h, {"parent_id": None, "ids": ids})
        assert r.status_code == 422 and "ids" in r.json()["fields"], ids
    wrong_parent = call("post", f"{C}/reorder", h, {"parent_id": a.pk, "ids": [outsider.pk]})
    assert wrong_parent.status_code == 422
    assert call("post", f"{C}/reorder", h, {"parent_id": 987654, "ids": []}).status_code == 404
    assert not audit("admin.categories_reordered")
    assert [c.name for c in Category.objects.filter(parent=None).order_by("sort_order", "name")][
        :2
    ] == ["Aa", "Bb"]


# ------------------------------------------------------------ activate / deactivate


def test_deactivate_hides_publicly_keeps_history_and_reports_usage() -> None:
    admin, h = ops()
    hair = make("Hair")
    cut = make("Haircut", hair)
    business = BusinessFactory(category=hair)
    service = ServiceFactory(business=business, category=cut)
    assert [c["name"] for c in public()] == ["Hair"] and public()[0]["children"][0][
        "name"
    ] == "Haircut"
    r = call("post", f"{C}/{cut.pk}/deactivate", h, {"version": 1})
    assert r.status_code == 200, r.content
    data = r.json()
    assert data["is_active"] is False and data["version"] == 2 and data["services_count"] == 1
    assert public()[0]["children"] == []  # gone from the public filter list at once
    service.refresh_from_db()
    assert service.category_id == cut.pk and service.is_active  # the existing service stays valid
    page = Client().get(f"/api/v1/businesses/{business.public_id}").json()
    assert [s["name"] for g in page["services"] for s in g["services"]] == [service.name]
    (row,) = audit("admin.category_deactivated")
    assert (
        row.actor_id == admin.pk
        and row.before["is_active"] is True
        and row.after["is_active"] is False
    )
    top = call("post", f"{C}/{hair.pk}/deactivate", h, {"version": 1}).json()
    assert top["businesses_count"] == 1 and public() == []


def test_new_services_cannot_pick_a_deactivated_category() -> None:
    from apps.businesses.tests.provider_helpers import P, owner_headers

    _, h = ops()
    cat = make("Retired")
    owner, oh = owner_headers()
    biz = BusinessFactory(owner=owner)
    body = {"name": "Facial", "price": 100, "duration_minutes": 30, "category_id": cat.pk}
    assert call("post", f"{P}/businesses/{biz.public_id}/services", oh, body).status_code == 201
    call("post", f"{C}/{cat.pk}/deactivate", h, {"version": 1})
    refused = call("post", f"{P}/businesses/{biz.public_id}/services", oh, body)
    assert refused.status_code == 422 and "category_id" in refused.json()["fields"]


def test_activate_and_state_conflicts() -> None:
    _, h = ops()
    parent = make("Parent")
    child = make("Child", parent)
    assert (
        call("post", f"{C}/{child.pk}/activate", h, {"version": 1}).json()["code"]
        == "invalid_state"
    )
    assert call("post", f"{C}/{parent.pk}/deactivate", h, {"version": 1}).status_code == 200
    assert call("post", f"{C}/{child.pk}/deactivate", h, {"version": 1}).status_code == 200
    blocked = call("post", f"{C}/{child.pk}/activate", h, {"version": 2})
    assert blocked.status_code == 409 and blocked.json()["code"] == "parent_inactive"
    assert call("post", f"{C}/{parent.pk}/activate", h, {"version": 2}).status_code == 200
    assert call("post", f"{C}/{child.pk}/activate", h, {"version": 2}).json()["is_active"] is True
    again = call("post", f"{C}/{parent.pk}/deactivate", h, {"version": 1})
    assert again.status_code == 409 and again.json()["code"] == "stale_version"
    assert call("post", f"{C}/{parent.pk}/deactivate", h, {}).status_code == 422
    assert call("post", f"{C}/987654/deactivate", h, {"version": 1}).status_code == 404
    assert (
        len(audit("admin.category_activated")) == 2
        and len(audit("admin.category_deactivated")) == 2
    )


# ------------------------------------------------------------ public cache


def test_public_taxonomy_is_cached_and_invalidated_on_every_change(
    django_assert_num_queries,
) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    cat = make("Hair")
    assert [c["name"] for c in public()] == ["Hair"]
    with django_assert_num_queries(0):
        assert Client().get(PUBLIC).status_code == 200  # served from the cache
    assert patch(h, cat, name="Hair care").status_code == 200
    assert [c["name"] for c in public()] == ["Hair care"]
    assert call("post", C, h, {"name": "Makeup"}).status_code == 201
    assert [c["name"] for c in public()] == ["Hair care", "Makeup"]
    assert (
        call(
            "post",
            f"{C}/{cat.pk}/deactivate",
            h,
            {"version": Category.objects.get(pk=cat.pk).version},
        ).status_code
        == 200
    )
    assert [c["name"] for c in public()] == ["Makeup"]


def test_public_taxonomy_needs_no_login_and_hides_inactive_children() -> None:
    top = make("Top")
    make("Visible", top)
    make("Hidden", top, is_active=False)
    r = Client().get(PUBLIC)
    assert r.status_code == 200
    assert [k["name"] for k in r.json()[0]["children"]] == ["Visible"]


# ------------------------------------------------------------ concurrency


@pytest.mark.django_db(transaction=True)
def test_two_admins_editing_with_the_same_version_one_wins(frozen, long_tokens) -> None:  # type: ignore[no-untyped-def]
    _, h1 = ops()
    _, h2 = ops()
    cat = make("Hair")
    barrier = threading.Barrier(2)
    codes: list[int] = []

    def worker(headers: dict[str, str], name: str) -> None:
        try:
            barrier.wait()
            codes.append(
                call("patch", f"{C}/{cat.pk}", headers, {"version": 1, "name": name}).status_code
            )
        finally:
            connection.close()

    threads = [
        threading.Thread(target=worker, args=(h1, "One")),
        threading.Thread(target=worker, args=(h2, "Two")),
    ]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 409]
    cat.refresh_from_db()
    assert (
        cat.version == 2
        and cat.name in ("One", "Two")
        and len(audit("admin.category_updated")) == 1
    )

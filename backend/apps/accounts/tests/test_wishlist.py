import threading
import uuid
from datetime import timedelta

import pytest
import time_machine
from django.db import connection
from django.test import Client

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import Role, Wishlist
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings.tests.helpers import NOW
from apps.businesses.factories import (
    BusinessFactory,
    BusinessPhotoFactory,
    ServiceFactory,
    open_all_week,
)
from apps.geo.factories import AreaFactory, CityFactory

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("frozen", "long_tokens")]
BASE = "/api/v1/users/me/wishlist"


def session():  # type: ignore[no-untyped-def]
    user = make_customer()
    _, s = login_client(user)
    return user, bearer(s)


def add(headers, business):  # type: ignore[no-untyped-def]
    ref = business.public_id if hasattr(business, "public_id") else business
    return Client().post(f"{BASE}/{ref}", **headers)


def remove(headers, business):  # type: ignore[no-untyped-def]
    ref = business.public_id if hasattr(business, "public_id") else business
    return Client().delete(f"{BASE}/{ref}", **headers)


def listing(headers, **params):  # type: ignore[no-untyped-def]
    return Client().get(BASE, params, **headers)


def names(r):  # type: ignore[no-untyped-def]
    assert r.status_code == 200, r.content
    return [i["name"] for i in r.json()["items"]]


# ---------------- add ----------------


def test_add_creates_entry() -> None:
    user, h = session()
    b = BusinessFactory()
    r = add(h, b)
    assert r.status_code == 201
    assert r.json() == {"business_id": str(b.public_id), "wishlisted": True}
    assert Wishlist.objects.get().user_id == user.pk


def test_second_add_is_idempotent_200_no_duplicate() -> None:
    user, h = session()
    b = BusinessFactory()
    assert add(h, b).status_code == 201
    r = add(h, b)
    assert r.status_code == 200 and r.json()["wishlisted"] is True
    assert Wishlist.objects.filter(user=user).count() == 1


@pytest.mark.django_db(transaction=True)
def test_concurrent_adds_create_one_row() -> None:
    user, h = session()
    b = BusinessFactory()
    codes: list[int] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(add(h, b).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert (set(codes) <= {200, 201} and codes.count(201) <= 1 and 201 in codes) or codes.count(
        200
    ) >= 5
    assert Wishlist.objects.filter(user=user).count() == 1


def test_unlisted_business_cannot_be_added() -> None:
    _, h = session()
    b = BusinessFactory(is_listed=False)
    r = add(h, b)
    assert r.status_code == 404 and r.json()["code"] == "not_found"
    assert Wishlist.objects.count() == 0


def test_hidden_city_and_unknown_business_cannot_be_added() -> None:
    _, h = session()
    hidden = BusinessFactory(area=AreaFactory(city=CityFactory(visibility="hidden")))
    assert add(h, hidden).status_code == 404
    assert add(h, uuid.uuid4()).status_code == 404
    assert Client().post(f"{BASE}/not-a-uuid", **h).status_code == 422
    assert Wishlist.objects.count() == 0


def test_each_user_has_their_own_list() -> None:
    (u1, h1), (u2, h2) = session(), session()
    b = BusinessFactory()
    add(h1, b)
    assert names(listing(h2)) == [] and names(listing(h1)) == [b.name]
    add(h2, b)
    assert Wishlist.objects.count() == 2 and u1.pk != u2.pk


# ---------------- remove ----------------


def test_remove_deletes_and_is_idempotent() -> None:
    _, h = session()
    b = BusinessFactory()
    add(h, b)
    assert remove(h, b).status_code == 204
    assert Wishlist.objects.count() == 0
    assert remove(h, b).status_code == 204  # already gone
    assert remove(h, uuid.uuid4()).status_code == 204  # unknown id


def test_remove_works_for_business_that_became_unlisted() -> None:
    _, h = session()
    b = BusinessFactory()
    add(h, b)
    b.is_listed = False
    b.save()
    assert remove(h, b).status_code == 204 and Wishlist.objects.count() == 0


def test_remove_only_affects_own_entry() -> None:
    (_, h1), (_, h2) = session(), session()
    b = BusinessFactory()
    add(h1, b)
    add(h2, b)
    remove(h1, b)
    assert Wishlist.objects.count() == 1 and names(listing(h2)) == [b.name]


# ---------------- listing ----------------


def test_list_newest_first_with_card_fields() -> None:
    _, h = session()
    area = AreaFactory(name="Clifton")
    first = BusinessFactory(name="First", area=area, rating_avg="4.50", rating_count=10)
    second = BusinessFactory(name="Second", verification_status="pending")
    ServiceFactory(business=first, price=1500)
    ServiceFactory(business=first, price=900)
    BusinessPhotoFactory(business=first, image="b/cover.jpg", is_cover=True)
    with time_machine.travel(NOW, tick=False):
        open_all_week(first)
        add(h, first)
    with time_machine.travel(NOW + timedelta(minutes=1), tick=False):
        add(h, second)
    with time_machine.travel(NOW + timedelta(minutes=2), tick=False):
        r = listing(h)
    items = r.json()["items"]
    assert [i["name"] for i in items] == ["Second", "First"]
    card = items[1]
    assert card["id"] == str(first.public_id) and card["slug"] == first.slug
    assert card["area"] == "Clifton" and card["rating_avg"] == 4.5 and card["rating_count"] == 10
    assert card["starting_price"] == 900 and card["is_verified"] is True
    assert card["cover_photo_url"].endswith("b/cover.jpg")
    assert card["next_available_slot"] == "2026-11-02T10:00:00+05:00"
    assert card["added_at"].startswith("2026-11-02T04:00:00")
    assert items[0]["is_verified"] is False and items[0]["next_available_slot"] is None
    assert items[0]["starting_price"] is None and items[0]["cover_photo_url"] is None


def test_list_hides_businesses_no_longer_public_but_keeps_rows() -> None:
    user, h = session()
    keep, gone = BusinessFactory(name="Keep"), BusinessFactory(name="Gone")
    add(h, keep)
    add(h, gone)
    gone.is_listed = False
    gone.save()
    assert names(listing(h)) == ["Keep"]
    assert Wishlist.objects.filter(user=user).count() == 2
    gone.is_listed = True
    gone.save()
    assert set(names(listing(h))) == {"Keep", "Gone"}


def test_list_empty() -> None:
    _, h = session()
    assert listing(h).json() == {"items": [], "next_cursor": None}


def test_list_cursor_pagination() -> None:
    _, h = session()
    created = []
    for i in range(7):
        b = BusinessFactory(name=f"B{i}")
        with time_machine.travel(NOW + timedelta(minutes=i), tick=False):
            add(h, b)
        created.append(b.name)
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        data = listing(h, **params).json()
        seen += [i["name"] for i in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert seen == list(reversed(created))
    assert listing(h, cursor="junk").status_code == 400
    assert listing(h, limit=0).status_code == 422


def test_list_query_count_bounded_when_warm(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    _, h = session()
    for i in range(12):
        b = BusinessFactory(name=f"B{i}")
        BusinessPhotoFactory(business=b)
        add(h, b)
    listing(h, limit=12)  # warm the next-slot cache
    with django_assert_max_num_queries(10):
        r = listing(h, limit=12)
    assert len(r.json()["items"]) == 12


# ---------------- auth ----------------


def test_wishlist_requires_auth_and_customer_role() -> None:
    b = BusinessFactory()
    assert Client().get(BASE).status_code == 401
    assert Client().post(f"{BASE}/{b.public_id}").status_code == 401
    assert Client().delete(f"{BASE}/{b.public_id}").status_code == 401
    _, s = login_client(UserFactory(role=Role.BUSINESS_OWNER))
    for call in (
        lambda: Client().get(BASE, **bearer(s)),
        lambda: Client().post(f"{BASE}/{b.public_id}", **bearer(s)),
        lambda: Client().delete(f"{BASE}/{b.public_id}", **bearer(s)),
    ):
        assert call().status_code == 403


def test_search_reflects_wishlist_state() -> None:
    _, h = session()
    b = BusinessFactory(name="Liked")
    BusinessFactory(name="Other")
    add(h, b)
    items = {
        i["name"]: i["is_wishlisted"]
        for i in Client().get("/api/v1/search/businesses", **h).json()["items"]
    }
    assert items == {"Liked": True, "Other": False}
    remove(h, b)
    items = {
        i["name"]: i["is_wishlisted"]
        for i in Client().get("/api/v1/search/businesses", **h).json()["items"]
    }
    assert items["Liked"] is False

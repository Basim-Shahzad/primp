import uuid
from datetime import timedelta

import pytest
from django.test import Client
from django.utils import timezone

from apps.accounts.factories import make_customer
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import AppointmentStatus
from apps.businesses.factories import BusinessFactory
from apps.reviews.factories import ReviewFactory, ReviewReplyFactory
from apps.reviews.models import ReviewStatus

pytestmark = pytest.mark.django_db


def url(b) -> str:  # type: ignore[no-untyped-def]
    return f"/api/v1/businesses/{b.public_id}/reviews"


def review(b, **kw):  # type: ignore[no-untyped-def]
    customer = kw.pop("customer", None) or make_customer(name=kw.pop("name", "Sana Malik"))
    appt = AppointmentFactory(business=b, customer=customer, status=AppointmentStatus.COMPLETED)
    return ReviewFactory(appointment=appt, **kw)


def test_newest_first_and_item_shape() -> None:
    b = BusinessFactory()
    old = review(b, comment="old")
    old.created_at = timezone.now() - timedelta(days=2)
    old.save()
    new = review(b, comment="new", rating=4, photos=["reviews/a.jpg"])
    r = Client().get(url(b))
    assert r.status_code == 200
    items = r.json()["items"]
    assert [i["comment"] for i in items] == ["new", "old"]
    assert items[0]["id"] == str(new.public_id) and items[0]["rating"] == 4
    assert items[0]["author_name"] == "Sana M." and items[0]["photos"][0].endswith("reviews/a.jpg")
    assert items[0]["reply"] is None and items[0]["edited_at"] is None


def test_hidden_removed_and_other_business_excluded() -> None:
    b = BusinessFactory()
    review(b, comment="shown")
    review(b, status=ReviewStatus.HIDDEN)
    review(b, status=ReviewStatus.REMOVED)
    review(BusinessFactory())
    assert [i["comment"] for i in Client().get(url(b)).json()["items"]] == ["shown"]


def test_rating_filter() -> None:
    b = BusinessFactory()
    for rating in (5, 5, 3, 1):
        review(b, rating=rating)
    assert len(Client().get(url(b), {"rating": 5}).json()["items"]) == 2
    assert len(Client().get(url(b), {"rating": 3}).json()["items"]) == 1
    assert Client().get(url(b), {"rating": 2}).json()["items"] == []


@pytest.mark.parametrize(
    "params", [{"rating": 0}, {"rating": 6}, {"rating": "x"}, {"limit": 0}, {"limit": 101}]
)
def test_validation(params: dict[str, object]) -> None:
    assert Client().get(url(BusinessFactory()), params).status_code == 422


def test_reply_included() -> None:
    b = BusinessFactory()
    r = review(b)
    ReviewReplyFactory(review=r, body="Thank you for visiting")
    item = Client().get(url(b)).json()["items"][0]
    assert item["reply"]["body"] == "Thank you for visiting"


def test_cursor_pagination_walks_all_once() -> None:
    b = BusinessFactory()
    for i in range(7):
        review(b, comment=f"c{i}")
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        data = Client().get(url(b), params).json()
        seen += [i["id"] for i in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 7 == len(set(seen))
    assert Client().get(url(b), {"cursor": "bad"}).status_code == 400


def test_unlisted_and_unknown_business_404() -> None:
    b = BusinessFactory(is_listed=False)
    assert Client().get(url(b)).status_code == 404
    assert Client().get(f"/api/v1/businesses/{uuid.uuid4()}/reviews").status_code == 404


def test_query_count_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    b = BusinessFactory()
    for _ in range(10):
        ReviewReplyFactory(review=review(b))
    with django_assert_max_num_queries(5):
        assert len(Client().get(url(b)).json()["items"]) == 10


def test_author_name_variants() -> None:
    b = BusinessFactory()
    review(b, name="Hira")
    review(b, name="Ali Raza Khan")
    names = {i["author_name"] for i in Client().get(url(b)).json()["items"]}
    assert names == {"Hira", "Ali K."}

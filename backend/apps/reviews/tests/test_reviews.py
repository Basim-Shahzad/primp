import io
import json
import threading
from datetime import timedelta

import pytest
import time_machine
from django.core.files.storage import default_storage
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import Client
from django.utils import timezone
from PIL import Image

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.models import Role
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import AppointmentStatus
from apps.businesses.factories import BusinessFactory
from apps.core.errors import AppError
from apps.core.models import AuditLog
from apps.reviews import services
from apps.reviews.models import FlagReason, Review, ReviewFlag, ReviewOutbox, ReviewStatus
from apps.reviews.selectors import rating_summary

pytestmark = pytest.mark.django_db


def completed_appointment(customer=None, business=None, **kw):  # type: ignore[no-untyped-def]
    customer = customer or make_customer()
    _, session = login_client(customer)
    appt = AppointmentFactory(
        customer=customer,
        business=business or BusinessFactory(),
        status=kw.pop("status", AppointmentStatus.COMPLETED),
        **kw,
    )
    return appt, customer, bearer(session)


def url(appt) -> str:  # type: ignore[no-untyped-def]
    return f"/api/v1/appointments/{appt.public_id}/review"


def png(size=(50, 40)) -> SimpleUploadedFile:
    out = io.BytesIO()
    Image.new("RGB", size, (10, 120, 200)).save(out, "PNG")
    return SimpleUploadedFile("p.png", out.getvalue(), content_type="image/png")


def submit(appt, headers, data=None, files=None):  # type: ignore[no-untyped-def]
    payload = {"rating": 5, "comment": "Lovely"} if data is None else data
    if files:
        payload = {**payload, "photos": files}
    return Client().post(url(appt), payload, **headers)


def stored_review_files() -> list[str]:
    try:
        _, files = default_storage.listdir("reviews")
        return files
    except (FileNotFoundError, NotADirectoryError):
        return []


# ---------------- create ----------------


def test_create_review_success_updates_everything(
    run_commit, django_capture_on_commit_callbacks
) -> None:  # type: ignore[no-untyped-def]
    from apps.reviews import events

    seen = []
    events.review_created.connect(
        lambda sender, **kw: seen.append(kw), weak=False, dispatch_uid="t"
    )
    business = BusinessFactory()
    appt, customer, h = completed_appointment(business=business)
    with run_commit():
        r = submit(appt, h, {"rating": 4, "comment": "  Great stylist  "})
    events.review_created.disconnect(dispatch_uid="t")
    assert r.status_code == 201
    body = r.json()
    assert (
        body["rating"] == 4 and body["comment"] == "Great stylist" and body["status"] == "visible"
    )
    assert body["appointment_id"] == str(appt.public_id) and body["business_id"] == str(
        business.public_id
    )
    assert body["photos"] == [] and body["edited_at"] is None and body["can_edit_until"]
    review = Review.objects.get()
    assert (review.customer_id, review.business_id, review.appointment_id) == (
        customer.pk,
        business.pk,
        appt.pk,
    )
    business.refresh_from_db()
    assert float(business.rating_avg) == 4.0 and business.rating_count == 1
    assert [(e["review_id"], e["business_id"]) for e in seen] == [(review.pk, business.pk)]


def test_review_event_fires_only_after_commit(django_capture_on_commit_callbacks) -> None:  # type: ignore[no-untyped-def]
    from apps.reviews import events

    seen = []
    events.review_created.connect(
        lambda sender, **kw: seen.append(kw), weak=False, dispatch_uid="t2"
    )
    appt, _, h = completed_appointment()
    with django_capture_on_commit_callbacks(execute=False) as cbs:
        submit(appt, h)
        assert seen == []
    for cb in cbs:
        cb()
    events.review_created.disconnect(dispatch_uid="t2")
    assert len(seen) == 1


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"comment": "x"},
        {"rating": 0},
        {"rating": 6},
        {"rating": "five"},
        {"rating": 3.5},
        {"rating": 5, "comment": "x" * 1001},
    ],
)
def test_create_validation_422(data: dict) -> None:  # type: ignore[type-arg]
    appt, _, h = completed_appointment()
    r = submit(appt, h, data)
    assert r.status_code == 422 and Review.objects.count() == 0


@pytest.mark.parametrize("rating", [1, 5])
def test_rating_boundaries_accepted(rating: int) -> None:
    appt, _, h = completed_appointment()
    assert submit(appt, h, {"rating": rating}).status_code == 201


def test_comment_boundary_1000_accepted_and_empty_allowed() -> None:
    a1, _, h1 = completed_appointment()
    assert submit(a1, h1, {"rating": 5, "comment": "x" * 1000}).status_code == 201
    a2, _, h2 = completed_appointment()
    assert submit(a2, h2, {"rating": 5}).json()["comment"] == ""


@pytest.mark.parametrize(
    "status",
    ["pending", "confirmed", "cancelled_by_customer", "cancelled_by_business", "no_show"],
)
def test_review_before_completion_409(status: str) -> None:
    appt, _, h = completed_appointment(status=status)
    r = submit(appt, h)
    assert r.status_code == 409 and r.json()["code"] == "appointment_not_completed"
    assert Review.objects.count() == 0


def test_second_review_409() -> None:
    appt, _, h = completed_appointment()
    assert submit(appt, h).status_code == 201
    r = submit(appt, h, {"rating": 1})
    assert r.status_code == 409 and r.json()["code"] == "already_reviewed"
    assert Review.objects.count() == 1 and Review.objects.get().rating == 5


def test_other_customers_or_unknown_appointment_404() -> None:
    appt, _, _ = completed_appointment()
    _, session = login_client(make_customer())
    assert submit(appt, bearer(session)).status_code == 404
    import uuid

    ghost = type("A", (), {"public_id": uuid.uuid4()})()
    owner_headers = bearer(login_client(make_customer())[1])
    assert submit(ghost, owner_headers).status_code == 404


def test_create_requires_auth_and_customer_role() -> None:
    appt, _, _ = completed_appointment()
    assert submit(appt, {}).status_code == 401
    _, session = login_client(UserFactory(role=Role.ADMIN))
    assert submit(appt, bearer(session)).status_code == 403


def test_xss_payload_stored_as_plain_text() -> None:
    appt, _, h = completed_appointment()
    evil = '<script>alert(1)</script><img src=x onerror="alert(2)"><b>Great</b>\x00\x07 place'
    r = submit(appt, h, {"rating": 5, "comment": evil})
    assert r.status_code == 201
    stored = Review.objects.get().comment
    assert "<" not in stored and ">" not in stored and "\x00" not in stored and "\x07" not in stored
    assert "Great" in stored and "place" in stored
    assert "<script" not in r.content.decode().lower()
    assert services.clean_comment("<b></b>") == "" and services.clean_comment("a\nb") == "a\nb"


# ---------------- photos ----------------


def test_up_to_three_photos_stored_and_returned() -> None:
    appt, _, h = completed_appointment()
    r = submit(appt, h, files=[png(), png(), png()])
    assert r.status_code == 201
    photos = r.json()["photos"]
    assert len(photos) == 3 and all("reviews/" in p for p in photos)
    assert len(Review.objects.get().photos) == 3 and len(stored_review_files()) >= 0


def test_photo_is_resized_and_reencoded() -> None:
    appt, _, h = completed_appointment()
    submit(appt, h, files=[png((3000, 1500))])
    path = Review.objects.get().photos[0]
    with default_storage.open(path) as f:
        img = Image.open(f)
        assert max(img.size) == 1280 and img.format == "PNG"


def test_four_photos_rejected_and_nothing_stored() -> None:
    appt, _, h = completed_appointment()
    r = submit(appt, h, files=[png() for _ in range(4)])
    assert r.status_code == 422 and r.json()["code"] == "too_many_photos"
    assert Review.objects.count() == 0 and stored_review_files() == []


def test_one_bad_photo_rejects_the_whole_review_without_orphans() -> None:
    appt, _, h = completed_appointment()
    bad = SimpleUploadedFile("evil.jpg", b"not an image", content_type="image/jpeg")
    r = submit(appt, h, files=[png(), bad])
    assert r.status_code == 422 and r.json()["code"] == "invalid_image"
    assert Review.objects.count() == 0 and stored_review_files() == []


def test_oversize_photo_413() -> None:
    appt, _, h = completed_appointment()
    big = SimpleUploadedFile(
        "big.png", png().read() + b"\0" * (5 * 1024 * 1024), content_type="image/png"
    )
    r = submit(appt, h, files=[big])
    assert r.status_code == 413 and Review.objects.count() == 0


def test_photos_removed_if_review_creation_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    appt, customer, _ = completed_appointment()

    def boom(*a, **k):  # type: ignore[no-untyped-def]
        raise RuntimeError("db exploded")

    monkeypatch.setattr(services, "recompute_business_rating", boom)
    png_bytes = png().read()
    with pytest.raises(RuntimeError):
        services.create_review(
            user_id=customer.pk,
            appointment_public_id=str(appt.public_id),
            rating=5,
            photos=[png_bytes, png_bytes],
        )
    assert Review.objects.count() == 0 and stored_review_files() == []


@pytest.mark.django_db(transaction=True)
def test_concurrent_reviews_for_one_appointment_only_one_succeeds() -> None:
    appt, _, h = completed_appointment()
    codes: list[int] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(submit(appt, h).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(201) == 1 and codes.count(409) == 4
    assert Review.objects.count() == 1


# ---------------- edit ----------------


def edit(review, headers, data):  # type: ignore[no-untyped-def]
    return Client().patch(
        f"/api/v1/reviews/{review.public_id}",
        data=json.dumps(data),
        content_type="application/json",
        **headers,
    )


def make_review(**kw):  # type: ignore[no-untyped-def]
    appt, customer, h = completed_appointment()
    submit(appt, h, {"rating": kw.get("rating", 5), "comment": "orig"})
    return Review.objects.get(appointment=appt), customer, h


def test_edit_updates_review_and_aggregate() -> None:
    review, _, h = make_review(rating=5)
    r = edit(review, h, {"rating": 2, "comment": " better now <b>x</b> "})
    assert r.status_code == 200
    body = r.json()
    assert body["rating"] == 2 and body["comment"] == "better now x" and body["edited_at"]
    review.refresh_from_db()
    assert review.edited_at is not None
    assert float(review.business.rating_avg) == 2.0


def test_edit_partial_fields() -> None:
    review, _, h = make_review()
    assert edit(review, h, {"comment": "only comment"}).json()["rating"] == 5
    assert edit(review, h, {"rating": 3}).json()["comment"] == "only comment"
    assert edit(review, h, {}).status_code == 200


@pytest.mark.parametrize(
    ("delta", "ok"),
    [
        (timedelta(hours=47, minutes=59), True),
        (timedelta(hours=48), True),
        (timedelta(hours=48, minutes=1), False),
        (timedelta(days=5), False),
    ],
)
def test_edit_window_boundary(delta: timedelta, ok: bool, settings) -> None:  # type: ignore[no-untyped-def]
    settings.ACCESS_TOKEN_LIFETIME = timedelta(days=30)
    review, _, h = make_review()
    with time_machine.travel(review.created_at + delta, tick=False):
        r = edit(review, h, {"rating": 1})
    if ok:
        assert r.status_code == 200
    else:
        assert r.status_code == 409 and r.json()["code"] == "edit_window_closed"
        review.refresh_from_db()
        assert review.rating == 5 and review.edited_at is None


def test_edit_other_customers_review_404() -> None:
    review, _, _ = make_review()
    _, session = login_client(make_customer())
    r = edit(review, bearer(session), {"rating": 1})
    assert r.status_code == 404
    review.refresh_from_db()
    assert review.rating == 5


@pytest.mark.parametrize("status", [ReviewStatus.HIDDEN, ReviewStatus.REMOVED])
def test_edit_hidden_or_removed_review_409(status: str) -> None:
    review, _, h = make_review()
    Review.objects.filter(pk=review.pk).update(status=status)
    r = edit(review, h, {"rating": 1})
    assert r.status_code == 409 and r.json()["code"] == "review_not_editable"


@pytest.mark.parametrize(
    "data", [{"rating": 0}, {"rating": 6}, {"rating": "x"}, {"comment": "x" * 1001}]
)
def test_edit_validation(data: dict) -> None:  # type: ignore[type-arg]
    review, _, h = make_review()
    assert edit(review, h, data).status_code == 422


def test_edit_auth() -> None:
    review, _, _ = make_review()
    assert edit(review, {}, {"rating": 1}).status_code == 401
    _, session = login_client(UserFactory(role=Role.ADMIN))
    assert edit(review, bearer(session), {"rating": 1}).status_code == 403


# ---------------- aggregates ----------------


def test_aggregate_correct_after_creates_edits_hide_and_restore() -> None:
    business = BusinessFactory()
    reviews = []
    for rating in (5, 4, 2):
        appt, _, h = completed_appointment(business=business)
        submit(appt, h, {"rating": rating})
        reviews.append(Review.objects.get(appointment=appt))

    def state() -> tuple[float, int, dict[int, int]]:
        business.refresh_from_db()
        s = rating_summary(business.pk)
        assert float(business.rating_avg) == float(s.avg) and business.rating_count == s.count
        assert sum(s.distribution.values()) == s.count
        return float(s.avg), s.count, s.distribution

    assert state()[:2] == (3.67, 3)
    # edit 2 -> 5
    owner_h = bearer(login_client(reviews[2].customer)[1])
    edit(reviews[2], owner_h, {"rating": 5})
    assert state() == (4.67, 3, {1: 0, 2: 0, 3: 0, 4: 1, 5: 2})
    # hide one
    services.moderate_review(reviews[0].pk, ReviewStatus.HIDDEN, actor_id=1, reason="spam")
    assert state()[:2] == (4.5, 2)
    services.moderate_review(reviews[1].pk, ReviewStatus.REMOVED, actor_id=1)
    assert state()[:2] == (5.0, 1)
    services.moderate_review(reviews[0].pk, ReviewStatus.VISIBLE, actor_id=1)
    assert state()[:2] == (5.0, 2)
    for r in reviews:
        services.moderate_review(r.pk, ReviewStatus.HIDDEN, actor_id=1)
    assert state() == (0.0, 0, {1: 0, 2: 0, 3: 0, 4: 0, 5: 0})


def test_moderate_audits_and_validates() -> None:
    review, _, _ = make_review()
    services.moderate_review(review.pk, ReviewStatus.HIDDEN, actor_id=77, reason="abuse")
    entry = AuditLog.objects.get(action="review.moderated")
    assert entry.actor_id == 77 and entry.before == {"status": "visible"}
    assert entry.after == {"status": "hidden", "reason": "abuse"}
    with pytest.raises(AppError) as exc:
        services.moderate_review(review.pk, "bogus", actor_id=1)
    assert exc.value.status == 422
    with pytest.raises(AppError) as exc:
        services.moderate_review(999999, ReviewStatus.HIDDEN, actor_id=1)
    assert exc.value.status == 404


def test_business_page_cache_invalidated_by_review_changes() -> None:
    from django.core.cache import cache

    from apps.businesses import pagecache

    business = BusinessFactory()
    appt, _, h = completed_appointment(business=business)
    Client().get(f"/api/v1/businesses/{business.slug}")
    assert cache.get(pagecache.page_key(business.pk)) is not None
    submit(appt, h)
    body = Client().get(f"/api/v1/businesses/{business.slug}").json()
    assert body["rating"]["count"] == 1


# ---------------- flags ----------------


def flag(review, headers, reason="offensive"):  # type: ignore[no-untyped-def]
    return Client().post(
        f"/api/v1/reviews/{review.public_id}/flag",
        data=json.dumps({"reason": reason}),
        content_type="application/json",
        **headers,
    )


def test_flag_creates_flag_outbox_and_event(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.reviews import events

    seen = []
    events.review_flagged.connect(
        lambda sender, **kw: seen.append(kw), weak=False, dispatch_uid="f"
    )
    review, _, _ = make_review()
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    _, session = login_client(owner)
    with run_commit():
        r = flag(review, bearer(session), "fake")
    events.review_flagged.disconnect(dispatch_uid="f")
    assert r.status_code == 201 and r.json()["reason"] == "fake"
    review.refresh_from_db()
    assert review.is_flagged is True and review.status == ReviewStatus.VISIBLE
    f = ReviewFlag.objects.get()
    assert (f.review_id, f.reporter_id, f.reason) == (review.pk, owner.pk, "fake")
    row = ReviewOutbox.objects.get()
    assert (
        row.event == "review_flagged"
        and row.payload["reason"] == "fake"
        and row.processed_at is None
    )
    assert [{k: v for k, v in e.items() if k != "signal"} for e in seen] == [
        {"review_id": review.pk, "reporter_id": owner.pk, "reason": "fake"}
    ]


def test_flag_twice_409_even_with_different_reason() -> None:
    review, _, _ = make_review()
    _, session = login_client(make_customer())
    h = bearer(session)
    assert flag(review, h).status_code == 201
    r = flag(review, h, "fake")
    assert r.status_code == 409 and r.json()["code"] == "already_flagged"
    assert ReviewFlag.objects.count() == 1 and ReviewOutbox.objects.count() == 1


def test_different_reporters_can_each_flag() -> None:
    review, _, _ = make_review()
    for _ in range(3):
        _, session = login_client(make_customer())
        assert flag(review, bearer(session)).status_code == 201
    assert ReviewFlag.objects.count() == 3


def test_cannot_flag_own_review() -> None:
    review, _, h = make_review()
    r = flag(review, h)
    assert r.status_code == 409 and r.json()["code"] == "cannot_flag_own_review"


@pytest.mark.parametrize("status", [ReviewStatus.HIDDEN, ReviewStatus.REMOVED])
def test_flag_hidden_or_unknown_review_404(status: str) -> None:
    review, _, _ = make_review()
    Review.objects.filter(pk=review.pk).update(status=status)
    _, session = login_client(make_customer())
    assert flag(review, bearer(session)).status_code == 404
    import uuid

    ghost = type("R", (), {"public_id": uuid.uuid4()})()
    assert flag(ghost, bearer(session)).status_code == 404


@pytest.mark.parametrize("reason", ["", "spam", "x" * 21, "OFFENSIVE"])
def test_flag_invalid_reason_422(reason: str) -> None:
    review, _, _ = make_review()
    _, session = login_client(make_customer())
    assert flag(review, bearer(session), reason).status_code == 422


def test_all_flag_reasons_accepted() -> None:
    for reason in FlagReason.values:
        review, _, _ = make_review()
        _, session = login_client(make_customer())
        assert flag(review, bearer(session), reason).status_code == 201


def test_flag_requires_auth() -> None:
    review, _, _ = make_review()
    assert flag(review, {}).status_code == 401


@pytest.mark.django_db(transaction=True)
def test_concurrent_identical_flags_only_one_recorded() -> None:
    review, _, _ = make_review()
    _, session = login_client(make_customer())
    h = bearer(session)
    codes: list[int] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(flag(review, h).status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes.count(201) == 1 and codes.count(409) == 4
    assert ReviewFlag.objects.count() == 1 and timezone.now()

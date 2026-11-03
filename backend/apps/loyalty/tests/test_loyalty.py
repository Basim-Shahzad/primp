import io
import itertools
import json
import logging
import threading
from datetime import timedelta

import pytest
import time_machine
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import IntegrityError, connection, transaction
from django.test import Client
from django.utils import timezone
from hypothesis import given, settings
from hypothesis import strategies as st
from PIL import Image

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings import services as booking_services
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import Appointment
from apps.bookings.tests.helpers import NOW
from apps.businesses.factories import BusinessFactory
from apps.core.models import ImmutableRecordError
from apps.geo.factories import CityFactory
from apps.loyalty import services
from apps.loyalty.models import LoyaltyAccount, PointRule, PointTransaction, RuleKind
from apps.loyalty.tasks import reconcile_accounts

pytestmark = pytest.mark.django_db
ACTOR = booking_services.Actor(1, "business_owner")


def booking_rule() -> PointRule:
    return PointRule.objects.get(action="booking")


def complete_booking(customer, price: int = 2000, when=NOW):  # type: ignore[no-untyped-def]
    a = AppointmentFactory(
        customer=customer,
        business=BusinessFactory(),
        status="confirmed",
        total_price=price,
        scheduled_start=when - timedelta(hours=2),
        scheduled_end=when - timedelta(hours=1),
    )
    booking_services.complete(a.pk, ACTOR, now=when)
    return a


def balance(user) -> int:  # type: ignore[no-untyped-def]
    return services.summary(user.pk)[0]


def ledger_total(user) -> int:  # type: ignore[no-untyped-def]
    return sum(PointTransaction.objects.filter(customer=user).values_list("delta", flat=True))


# ---------------- rules ----------------


def test_default_rules_seeded() -> None:
    rules = {r.action: r for r in PointRule.objects.all()}
    assert set(rules) == {
        "booking",
        "review",
        "first_booking",
        "fifth_booking",
        "tenth_booking",
        "profile_completion",
    }
    assert (rules["booking"].kind, rules["booking"].points, rules["booking"].unit_amount) == (
        "per_amount",
        10,
        1000,
    )
    assert [
        rules[k].points
        for k in ("review", "first_booking", "fifth_booking", "tenth_booking", "profile_completion")
    ] == [25, 50, 100, 200, 20]


@pytest.mark.parametrize(
    ("amount", "expected"),
    [
        (0, 0),
        (-500, 0),
        (1, 0),
        (99, 0),
        (100, 1),
        (999, 9),
        (1000, 10),
        (1001, 10),
        (1999, 19),
        (2000, 20),
        (35000, 350),
    ],
)
def test_booking_points_floor(amount: int, expected: int) -> None:
    assert services.points_for(booking_rule(), amount) == expected


@settings(max_examples=200, deadline=None)
@given(amount=st.integers(0, 10_000_000), points=st.integers(1, 100), unit=st.integers(1, 100_000))
def test_points_for_properties(amount: int, points: int, unit: int) -> None:
    rule = PointRule(kind=RuleKind.PER_AMOUNT, points=points, unit_amount=unit)
    result = services.points_for(rule, amount)
    assert result == (amount * points) // unit  # floor, never rounds up
    assert result >= 0 and result * unit <= amount * points < (result + 1) * unit
    assert services.points_for(rule, amount + unit) >= result  # monotonic
    assert services.points_for(rule, unit) == points  # exact multiples are exact


def test_flat_rule_ignores_amount() -> None:
    assert services.points_for(PointRule(kind="flat", points=25), 99999) == 25


def test_inactive_expired_future_rules_are_ignored() -> None:
    user = make_customer()
    rule = booking_rule()
    rule.is_active = False
    rule.save()
    assert (
        services.credit(
            customer_id=user.pk,
            action="booking",
            reference_type="appointment",
            reference_id=1,
            amount=5000,
        )
        is None
    )
    rule.is_active = True
    rule.valid_to = timezone.now() - timedelta(seconds=1)
    rule.save()
    assert services.active_rule("booking") is None
    rule.valid_to = None
    rule.valid_from = timezone.now() + timedelta(days=1)
    rule.save()
    assert services.active_rule("booking") is None
    rule.valid_from = timezone.now() - timedelta(days=1)
    rule.valid_to = timezone.now() + timedelta(days=1)
    rule.save()
    assert services.active_rule("booking") == rule
    assert balance(user) == 0


def test_rule_window_boundaries() -> None:
    rule = booking_rule()
    start = NOW
    rule.valid_from, rule.valid_to = start, start + timedelta(days=1)
    rule.save()
    assert services.active_rule("booking", start) == rule  # valid_from inclusive
    assert services.active_rule("booking", start - timedelta(seconds=1)) is None
    assert services.active_rule("booking", start + timedelta(days=1)) is None  # valid_to exclusive


def test_newest_active_rule_wins_and_is_editable_without_deploy() -> None:
    user = make_customer()
    PointRule.objects.create(action="booking", kind="per_amount", points=20, unit_amount=1000)
    entry = services.credit(
        customer_id=user.pk,
        action="booking",
        reference_type="appointment",
        reference_id=1,
        amount=1000,
    )
    assert entry is not None and entry.delta == 20
    rule = PointRule.objects.filter(action="booking").latest("id")
    rule.points = 30
    rule.save()  # rate change takes effect immediately
    entry = services.credit(
        customer_id=user.pk,
        action="booking",
        reference_type="appointment",
        reference_id=2,
        amount=1000,
    )
    assert entry is not None and entry.delta == 30


def test_rule_constraint_requires_unit_for_per_amount() -> None:
    with pytest.raises(IntegrityError), transaction.atomic():
        PointRule.objects.create(action="x", kind="per_amount", points=1)


def test_zero_point_result_creates_no_transaction() -> None:
    user = make_customer()
    assert (
        services.credit(
            customer_id=user.pk,
            action="booking",
            reference_type="appointment",
            reference_id=1,
            amount=99,
        )
        is None
    )
    assert PointTransaction.objects.count() == 0 and not LoyaltyAccount.objects.exists()


# ---------------- crediting ----------------


def test_credit_sets_expiry_remaining_and_updates_account() -> None:
    user = make_customer()
    entry = services.credit(
        customer_id=user.pk, action="review", reference_type="review", reference_id=7, now=NOW
    )
    assert entry is not None
    assert (
        entry.delta,
        entry.remaining,
        entry.reason,
        entry.reference_type,
        entry.reference_id,
    ) == (25, 25, "review", "review", "7")
    assert entry.expires_at == NOW + timedelta(days=365)
    account = LoyaltyAccount.objects.get(customer=user)
    assert (account.balance, account.lifetime_earned) == (25, 25)


def test_double_event_single_credit() -> None:
    user = make_customer()
    first = services.credit(
        customer_id=user.pk, action="review", reference_type="review", reference_id=7
    )
    second = services.credit(
        customer_id=user.pk, action="review", reference_type="review", reference_id=7
    )
    assert first is not None and second is None
    assert PointTransaction.objects.count() == 1 and balance(user) == 25


def test_same_reference_different_actions_both_credit() -> None:
    user = make_customer()
    services.credit(customer_id=user.pk, action="review", reference_type="x", reference_id=1)
    services.credit(customer_id=user.pk, action="first_booking", reference_type="x", reference_id=1)
    assert balance(user) == 75


@pytest.mark.django_db(transaction=True)
def test_concurrent_credits_same_reference_credit_once() -> None:
    user = make_customer()
    results: list[bool] = []
    barrier = threading.Barrier(6)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(
                services.credit(
                    customer_id=user.pk, action="review", reference_type="review", reference_id=1
                )
                is not None
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(6)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count(True) == 1 and balance(user) == 25 == ledger_total(user)


@pytest.mark.django_db(transaction=True)
def test_concurrent_credits_distinct_references_all_land() -> None:
    user = make_customer()
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        try:
            barrier.wait()
            services.credit(
                customer_id=user.pk, action="review", reference_type="review", reference_id=i
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert balance(user) == 8 * 25 == ledger_total(user)


# ---------------- booking completion ----------------


def test_points_credited_on_completion_not_creation(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings.tests.api_helpers import (
        book,
        capacity_business,
        customer_session,
        place_hold,
    )

    with time_machine.travel(NOW, tick=False):
        b, svc = capacity_business()
        user, h = customer_session()
        token = place_hold(h, b, [svc]).json()["token"]
        with run_commit():
            book(h, token)
    assert PointTransaction.objects.count() == 0 and balance(user) == 0
    a = Appointment.objects.get()
    with run_commit():
        booking_services.complete(a.pk, ACTOR, now=a.scheduled_start)
    assert balance(user) == 20 + 50  # PKR 2,000 + first booking bonus


def test_completion_credit_amounts_and_references(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    with run_commit():
        a = complete_booking(user, price=1999)
    rows = {t.reason: t for t in PointTransaction.objects.filter(customer=user)}
    assert rows["booking"].delta == 19 and rows["booking"].reference_type == "appointment"
    assert rows["booking"].reference_id == str(a.pk)
    assert rows["first_booking"].delta == 50 and rows["first_booking"].reference_type == "user"


def test_zero_priced_booking_still_earns_first_booking_bonus(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    with run_commit():
        complete_booking(user, price=0)
    assert [t.reason for t in PointTransaction.objects.all()] == ["first_booking"]


def test_milestones_stack_at_exactly_fifth_and_tenth(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    totals = []
    for _ in range(11):
        with run_commit():
            complete_booking(user, price=1000)
        totals.append(balance(user))
    gains = [totals[0], *(b - a for a, b in itertools.pairwise(totals))]
    base = 10
    assert gains == [base + 50] + [base] * 3 + [base + 100] + [base] * 4 + [base + 200] + [base]
    reasons = list(PointTransaction.objects.filter(customer=user).values_list("reason", flat=True))
    assert (
        reasons.count("first_booking")
        == reasons.count("fifth_booking")
        == reasons.count("tenth_booking")
        == 1
    )
    assert balance(user) == ledger_total(user) == 11 * base + 350


def test_completion_event_delivered_twice_credits_once(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings import events

    user = make_customer()
    with run_commit():
        a = complete_booking(user)
    before = balance(user)
    events.booking_completed.send(sender=None, appointment_id=a.pk)
    events.booking_completed.send(sender=None, appointment_id=a.pk)
    assert balance(user) == before


def test_other_customers_are_independent(run_commit) -> None:  # type: ignore[no-untyped-def]
    a, b = make_customer(), make_customer()
    with run_commit():
        complete_booking(a)
    assert balance(a) > 0 and balance(b) == 0


# ---------------- reversal ----------------


def test_reverse_points_restores_balance_and_is_idempotent(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    with run_commit():
        a = complete_booking(user, price=3000)
    assert balance(user) == 30 + 50
    reversed_ = services.reverse_points("appointment", a.pk)
    assert [t.delta for t in reversed_] == [-30]
    assert balance(user) == 50  # milestone bonus is tied to the customer, not the booking
    assert services.reverse_points("appointment", a.pk) == []
    assert balance(user) == 50 == ledger_total(user)
    account = LoyaltyAccount.objects.get(customer=user)
    assert account.lifetime_earned == 50
    rev = PointTransaction.objects.get(reason="booking_reversal")
    assert rev.reverses.reason == "booking" and rev.reference_id == str(a.pk)


def test_reverse_unknown_reference_is_noop() -> None:
    assert services.reverse_points("appointment", 999) == []


def test_refund_webhook_reverses_booking_points(run_commit) -> None:  # type: ignore[no-untyped-def]
    from apps.payments.gateways import FakeGateway

    user = make_customer()
    with run_commit():
        a = complete_booking(user, price=5000)
    assert balance(user) == 50 + 50
    body = json.dumps(
        {"event_id": "r1", "type": "refund.succeeded", "appointment_id": str(a.public_id)}
    ).encode()
    with run_commit():
        r = Client().post(
            "/api/v1/payments/webhooks/fake",
            data=body,
            content_type="application/json",
            HTTP_X_FAKE_SIGNATURE=FakeGateway.sign(body),
        )
    assert r.json()["status"] == "processed"
    assert balance(user) == 50  # booking points reversed, first-booking bonus kept


# ---------------- review & profile ----------------


def png_file() -> SimpleUploadedFile:
    out = io.BytesIO()
    Image.new("RGB", (40, 40), (1, 2, 3)).save(out, "PNG")
    return SimpleUploadedFile("a.png", out.getvalue(), content_type="image/png")


def test_review_earns_points_once(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    _, session = login_client(user)
    with run_commit():
        a = complete_booking(user, price=0)
    before = balance(user)
    with run_commit():
        r = Client().post(
            f"/api/v1/appointments/{a.public_id}/review",
            {"rating": 5, "comment": "ok"},
            **bearer(session),
        )
    assert r.status_code == 201
    assert balance(user) == before + 25
    row = PointTransaction.objects.get(reason="review")
    assert (row.reference_type, row.delta) == ("review", 25)
    with run_commit():
        r = Client().post(
            f"/api/v1/appointments/{a.public_id}/review", {"rating": 5}, **bearer(session)
        )
    assert r.status_code == 409 and balance(user) == before + 25


def test_profile_completion_bonus_once(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer(name="Sana", is_phone_verified=True)
    _, session = login_client(user)
    city = CityFactory()
    with run_commit():
        Client().patch(
            "/api/v1/users/me",
            data=json.dumps({"default_city_id": city.pk}),
            content_type="application/json",
            **bearer(session),
        )
    assert balance(user) == 0  # still no photo
    with run_commit():
        r = Client().post("/api/v1/users/me/photo", {"file": png_file()}, **bearer(session))
    assert r.status_code == 200 and balance(user) == 20
    with run_commit():
        Client().patch(
            "/api/v1/users/me",
            data=json.dumps({"name": "Sana K"}),
            content_type="application/json",
            **bearer(session),
        )
        Client().post("/api/v1/users/me/photo", {"file": png_file()}, **bearer(session))
    assert (
        balance(user) == 20
        and PointTransaction.objects.filter(reason="profile_completion").count() == 1
    )


def test_profile_not_complete_without_verified_contact() -> None:
    from apps.accounts import selectors

    user = make_customer(name="X", is_phone_verified=False, is_email_verified=False)
    user.profile.photo = "p.png"
    user.profile.default_city = CityFactory()
    user.profile.save()
    assert selectors.profile_is_complete(user.pk) is False
    assert selectors.profile_is_complete(999999) is False
    user.is_email_verified = True
    user.save()
    assert selectors.profile_is_complete(user.pk) is True


# ---------------- ledger integrity ----------------


def test_ledger_is_append_only() -> None:
    user = make_customer()
    entry = services.credit(
        customer_id=user.pk, action="review", reference_type="review", reference_id=1
    )
    assert entry is not None
    entry.delta = 999
    with pytest.raises(ImmutableRecordError):
        entry.save()
    with pytest.raises(ImmutableRecordError):
        entry.delete()
    with pytest.raises(ImmutableRecordError):
        PointTransaction.objects.all().update(delta=1)
    with pytest.raises(ImmutableRecordError):
        PointTransaction.objects.all().delete()
    assert PointTransaction.objects.get().delta == 25


def test_zero_delta_rejected_by_database() -> None:
    user = UserFactory()
    with pytest.raises(IntegrityError), transaction.atomic():
        PointTransaction.objects.create(
            customer=user, delta=0, reason="x", reference_type="t", reference_id="1"
        )


@settings(max_examples=40, deadline=None)
@given(
    ops=st.lists(
        st.tuples(
            st.sampled_from(["credit", "reverse"]), st.integers(1, 6), st.integers(0, 20_000)
        ),
        max_size=25,
    )
)
def test_balance_always_equals_ledger_sum(ops) -> None:  # type: ignore[no-untyped-def]
    with transaction.atomic():
        user = UserFactory(email=None, phone=f"+92300{PointTransaction.objects.count():07d}")
        sid = transaction.savepoint()
        for op, ref, amount in ops:
            if op == "credit":
                services.credit(
                    customer_id=user.pk,
                    action="booking",
                    reference_type="appointment",
                    reference_id=ref,
                    amount=amount,
                )
            else:
                services.reverse_points("appointment", ref)
        assert balance(user) == ledger_total(user)
        assert balance(user) >= 0
        transaction.savepoint_rollback(sid)
        transaction.set_rollback(True)


# ---------------- reconciliation ----------------


def test_reconcile_clean_ledger_has_no_mismatch() -> None:
    user = make_customer()
    services.credit(customer_id=user.pk, action="review", reference_type="review", reference_id=1)
    assert services.reconcile() == [] and reconcile_accounts() == 0


def test_reconcile_detects_tampered_summary_and_can_repair(
    caplog: pytest.LogCaptureFixture,
) -> None:
    user = make_customer()
    services.credit(customer_id=user.pk, action="review", reference_type="review", reference_id=1)
    LoyaltyAccount.objects.filter(customer=user).update(balance=9999)
    with caplog.at_level(logging.ERROR):
        found = services.reconcile()
    assert len(found) == 1 and (
        found[0].customer_id,
        found[0].balance,
        found[0].ledger_balance,
    ) == (user.pk, 9999, 25)
    assert "does not match ledger" in caplog.text
    assert reconcile_accounts() == 1
    services.reconcile(repair=True)
    assert balance(user) == 25 and services.reconcile() == []


def test_reconcile_detects_tampered_lifetime_and_missing_account() -> None:
    user, ghost = make_customer(), make_customer()
    services.credit(customer_id=user.pk, action="review", reference_type="review", reference_id=1)
    LoyaltyAccount.objects.filter(customer=user).update(lifetime_earned=1)
    services.credit(customer_id=ghost.pk, action="review", reference_type="review", reference_id=2)
    LoyaltyAccount.objects.filter(customer=ghost).delete()
    assert {m.customer_id for m in services.reconcile()} == {user.pk, ghost.pk}
    services.reconcile(repair=True)
    assert services.reconcile() == []


def test_nightly_reconcile_is_scheduled() -> None:
    from django.conf import settings as dj_settings

    entry = dj_settings.CELERY_BEAT_SCHEDULE["loyalty-reconcile"]
    assert entry["task"] == "apps.loyalty.tasks.reconcile_accounts"


# ---------------- API ----------------


def test_points_endpoints(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    _, session = login_client(user)
    other = make_customer()
    with run_commit():
        complete_booking(user, price=2000)
        complete_booking(other, price=9000)
    h = bearer(session)
    r = Client().get("/api/v1/users/me/points", **h)
    assert r.status_code == 200 and r.json() == {"balance": 70, "lifetime_earned": 70}
    r = Client().get("/api/v1/users/me/points/transactions", **h)
    items = r.json()["items"]
    assert {i["reason"] for i in items} == {"booking", "first_booking"}
    assert all(i["expires_at"] for i in items) and r.json()["next_cursor"] is None


def test_new_customer_has_zero_points() -> None:
    _, session = login_client(make_customer())
    assert Client().get("/api/v1/users/me/points", **bearer(session)).json() == {
        "balance": 0,
        "lifetime_earned": 0,
    }


def test_transactions_cursor_pagination(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    _, session = login_client(user)
    for i in range(7):
        services.credit(
            customer_id=user.pk, action="review", reference_type="review", reference_id=i
        )
    seen, cursor = [], None
    for _ in range(10):
        params = {"limit": 3, **({"cursor": cursor} if cursor else {})}
        data = (
            Client().get("/api/v1/users/me/points/transactions", params, **bearer(session)).json()
        )
        seen += [i["id"] for i in data["items"]]
        cursor = data["next_cursor"]
        if not cursor:
            break
    assert len(seen) == 7 == len(set(seen)) and seen == sorted(seen, reverse=True)
    assert (
        Client()
        .get("/api/v1/users/me/points/transactions", {"cursor": "bad"}, **bearer(session))
        .status_code
        == 400
    )


def test_points_endpoints_require_auth() -> None:
    assert Client().get("/api/v1/users/me/points").status_code == 401
    assert Client().get("/api/v1/users/me/points/transactions").status_code == 401


def test_transactions_query_count_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    user = make_customer()
    _, session = login_client(user)
    for i in range(15):
        services.credit(
            customer_id=user.pk, action="review", reference_type="review", reference_id=i
        )
    with django_assert_max_num_queries(4):
        r = Client().get("/api/v1/users/me/points/transactions", {"limit": 15}, **bearer(session))
    assert len(r.json()["items"]) == 15

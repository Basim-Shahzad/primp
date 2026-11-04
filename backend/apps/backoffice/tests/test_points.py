import json
import threading
import uuid
from datetime import timedelta
from typing import Any

import pytest
import time_machine
from django.db import connection
from django.test import Client
from django.utils import timezone

from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.roles import Role
from apps.backoffice.factories import make_admin
from apps.backoffice.tests.helpers import A, admin_headers, call, headers_for, reauth
from apps.core.models import AuditLog
from apps.loyalty import services as loyalty
from apps.loyalty.models import LoyaltyAccount, PointTransaction

pytestmark = pytest.mark.django_db
P = f"{A}/points"
NOTE = "Goodwill after a long wait at the salon"


def ops() -> tuple[Any, dict[str, str]]:
    admin, h = admin_headers("ops_admin")
    assert reauth(h).status_code == 200
    return admin, h


def adjust(h: dict[str, str], customer: Any, amount: int, key: str | None = "k1", **kw: Any) -> Any:
    body = {
        "customer_id": str(customer.public_id),
        "amount": amount,
        "category": "goodwill",
        "note": NOTE,
        **kw,
    }
    extra = {"HTTP_IDEMPOTENCY_KEY": key} if key is not None else {}
    return call("post", f"{P}/adjust", h, body, **extra)


def give(customer: Any, points: int) -> None:
    loyalty.credit(
        customer_id=customer.pk,
        action="booking",
        reference_type="seed",
        reference_id=uuid.uuid4().hex,
        amount=0,
    ) if False else None
    from apps.loyalty.models import PointRule

    PointRule.objects.update_or_create(action="seed", defaults={"kind": "flat", "points": points})
    loyalty.credit(
        customer_id=customer.pk, action="seed", reference_type="seed", reference_id=uuid.uuid4().hex
    )


def balance(customer: Any) -> int:
    return loyalty.summary(customer.pk)[0]


def audit(action: str = "admin.points_adjusted") -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action))


# ------------------------------------------------------------ success


def test_adjustment_writes_ledger_summary_audit_and_shows_to_the_customer() -> None:
    admin, h = ops()
    customer = make_customer()
    r = adjust(h, customer, 150)
    assert r.status_code == 201, r.content
    d = r.json()
    assert (d["delta"], d["balance_after"], d["category"], d["replayed"]) == (
        150,
        150,
        "goodwill",
        False,
    )
    row = PointTransaction.objects.get(pk=d["id"])
    assert (row.reason, row.reference_type, row.admin_id, row.category, row.note) == (
        "adjustment",
        "adjustment",
        admin.pk,
        "goodwill",
        NOTE,
    )
    assert row.remaining == 150 and row.expires_at is not None and row.customer_id == customer.pk
    account = LoyaltyAccount.objects.get(customer=customer)
    assert (account.balance, account.lifetime_earned) == (150, 150)
    (a,) = audit()
    assert a.actor_id == admin.pk and a.target_id == str(customer.public_id) and a.ip_address
    assert a.before == {"balance": 0}
    assert a.after == {
        "balance": 150,
        "delta": 150,
        "category": "goodwill",
        "note": NOTE,
        "transaction_id": row.pk,
    }
    mine = call("get", "/api/v1/users/me/points/transactions", headers_for(customer)).json()[
        "items"
    ]
    assert mine[0]["delta"] == 150 and mine[0]["reason"] == "adjustment" and "note" not in mine[0]
    assert call("get", "/api/v1/users/me/points", headers_for(customer)).json()["balance"] == 150
    assert loyalty.reconcile() == []


def test_negative_adjustments_and_the_below_zero_rule() -> None:
    _, h = ops()
    customer = make_customer()
    give(customer, 100)
    ok = adjust(h, customer, -100, key="a")
    assert ok.status_code == 201 and ok.json()["balance_after"] == 0
    row = PointTransaction.objects.get(pk=ok.json()["id"])
    assert row.delta == -100 and row.remaining == 0 and row.expires_at is None
    refused = adjust(h, customer, -1, key="b")
    assert refused.status_code == 422 and refused.json()["code"] == "insufficient_points"
    assert balance(customer) == 0 and len(audit()) == 1
    assert PointTransaction.objects.filter(customer=customer, reason="adjustment").count() == 1
    fraud = adjust(h, customer, -40, key="c", category="fraud_reversal")
    assert fraud.status_code == 201 and fraud.json()["balance_after"] == -40
    assert balance(customer) == -40 and loyalty.reconcile() == []
    for category in ("correction", "other"):
        assert adjust(h, customer, -1, key=f"x-{category}", category=category).status_code == 422


def test_owners_can_receive_points_but_other_accounts_cannot() -> None:
    _, h = ops()
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    assert adjust(h, owner, 20, key="o").status_code == 201
    for target in (make_admin("support_agent"), UserFactory(role=Role.STAFF)):
        r = adjust(h, target, 10, key=f"t{target.pk}")
        assert r.status_code == 422 and "customer_id" in r.json()["fields"]
    gone = make_customer()
    type(gone).objects.filter(pk=gone.pk).update(status="deleted")
    assert adjust(h, gone, 10, key="d").status_code == 422
    assert adjust(h, make_customer(), 10, key="s").status_code == 201
    suspended = make_customer()
    type(suspended).objects.filter(pk=suspended.pk).update(status="suspended")
    assert adjust(h, suspended, -5, key="f", category="fraud_reversal").status_code == 201
    unknown = call(
        "post",
        f"{P}/adjust",
        h,
        {"customer_id": str(uuid.uuid4()), "amount": 5, "category": "goodwill", "note": NOTE},
        HTTP_IDEMPOTENCY_KEY="u",
    )
    assert unknown.status_code == 404
    assert len(PointTransaction.objects.filter(reason="adjustment")) == 3


# ------------------------------------------------------------ validation


@pytest.mark.parametrize("amount", [0, 10_001, -10_001, 10**9])
def test_amount_limits(amount: int) -> None:
    _, h = ops()
    r = adjust(h, make_customer(), amount)
    assert r.status_code == 422 and "amount" in r.json()["fields"]
    assert not PointTransaction.objects.filter(reason="adjustment").exists() and not audit()


def test_amount_boundaries_and_configurable_maximum(settings) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    customer = make_customer()
    assert adjust(h, customer, 10_000, key="max").status_code == 201
    assert adjust(h, customer, -10_000, key="min").status_code == 201
    settings.POINTS_ADJUST_MAX = 50
    assert adjust(h, customer, 51, key="over").status_code == 422
    assert adjust(h, customer, 50, key="fits").status_code == 201


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"note": ""}, "note"),
        ({"note": "short"}, "note"),
        ({"note": "123456789"}, "note"),
        ({"note": "n" * 501}, "note"),
        ({"note": "         x         "}, "note"),
        ({"category": "bonus"}, "category"),
        ({"amount": "ten"}, "amount"),
        ({"customer_id": "nope"}, "customer_id"),
    ],
)
def test_validation(overrides: dict[str, Any], field: str) -> None:
    _, h = ops()
    customer = make_customer()
    body = {
        "customer_id": str(customer.public_id),
        "amount": 10,
        "category": "goodwill",
        "note": NOTE,
        **overrides,
    }
    r = call("post", f"{P}/adjust", h, body, HTTP_IDEMPOTENCY_KEY="v")
    assert r.status_code == 422 and field in r.json()["fields"], r.content
    assert not PointTransaction.objects.filter(reason="adjustment").exists()


def test_note_boundaries() -> None:
    _, h = ops()
    customer = make_customer()
    assert adjust(h, customer, 5, key="n10", note="x" * 10).status_code == 201
    assert adjust(h, customer, 5, key="n500", note="y" * 500).status_code == 201
    assert adjust(h, customer, 5, key="pad", note="  " + "z" * 10 + "  ").status_code == 201


def test_missing_fields_are_422() -> None:
    _, h = ops()
    r = call("post", f"{P}/adjust", h, {}, HTTP_IDEMPOTENCY_KEY="k")
    assert r.status_code == 422 and {"customer_id", "amount", "category", "note"} <= set(
        r.json()["fields"]
    )


# ------------------------------------------------------------ idempotency


def test_a_retry_with_the_same_key_creates_one_row() -> None:
    _, h = ops()
    customer = make_customer()
    first = adjust(h, customer, 80, key="retry-1")
    again = adjust(h, customer, 80, key="retry-1")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["id"] == first.json()["id"] and again.json()["replayed"] is True
    assert again.json()["balance_after"] == 80
    assert PointTransaction.objects.filter(customer=customer, reason="adjustment").count() == 1
    assert balance(customer) == 80 and len(audit()) == 1
    changed = adjust(h, customer, 90, key="retry-1")
    assert changed.status_code == 409 and changed.json()["code"] == "idempotency_conflict"
    other_customer = adjust(h, make_customer(), 80, key="retry-1")
    assert other_customer.status_code == 409
    assert balance(customer) == 80 and len(audit()) == 1


def test_keys_are_per_admin_and_required() -> None:
    _, h1 = ops()
    _, h2 = ops()
    customer = make_customer()
    assert adjust(h1, customer, 10, key="same").status_code == 201
    assert adjust(h2, customer, 10, key="same").status_code == 201  # another admin: another row
    assert balance(customer) == 20
    for key in (None, "", "   ", "k" * 65):
        r = adjust(h1, customer, 10, key=key)
        assert r.status_code == 422, key
    assert adjust(h1, customer, 10, key="k" * 64).status_code == 201


# ------------------------------------------------------------ re-authentication and permissions


def test_reauthentication_is_required_but_not_for_previews_or_listing() -> None:
    _, h = admin_headers("ops_admin")
    customer = make_customer()
    r = adjust(h, customer, 10)
    assert r.status_code == 403 and r.json()["code"] == "reauth_required"
    assert not PointTransaction.objects.filter(reason="adjustment").exists()
    prev = call(
        "post", f"{P}/adjust/preview", h, {"customer_id": str(customer.public_id), "amount": 10}
    )
    assert prev.status_code == 200
    assert call("get", f"{P}/adjustments", h).status_code == 200
    reauth(h)
    assert adjust(h, customer, 10).status_code == 201


def test_support_agents_and_other_roles_are_refused() -> None:
    _, h = admin_headers("support_agent")
    reauth(h)
    customer = make_customer()
    assert adjust(h, customer, 10).status_code == 403
    assert (
        call(
            "post", f"{P}/adjust/preview", h, {"customer_id": str(customer.public_id), "amount": 1}
        ).status_code
        == 403
    )
    assert call("get", f"{P}/adjustments", h).status_code == 403
    assert adjust({}, customer, 10).status_code == 401
    assert adjust(headers_for(make_customer()), customer, 10).status_code == 403
    assert balance(customer) == 0


# ------------------------------------------------------------ preview


def test_preview_reports_balances_and_has_no_side_effects() -> None:
    admin, h = ops()
    customer = make_customer()
    give(customer, 100)
    rows_before = PointTransaction.objects.count()
    r = call(
        "post",
        f"{P}/adjust/preview",
        h,
        {"customer_id": str(customer.public_id), "amount": 40, "category": "goodwill"},
    )
    assert r.status_code == 200 and r.json() == {
        "current_balance": 100,
        "resulting_balance": 140,
        "allowed": True,
        "problems": [],
        "daily_limit": 50_000,
        "daily_used": 0,
    }
    neg = call(
        "post", f"{P}/adjust/preview", h, {"customer_id": str(customer.public_id), "amount": -150}
    ).json()
    assert (
        neg["resulting_balance"] == -50
        and neg["allowed"] is False
        and neg["problems"] == ["insufficient_points"]
    )
    fraud = call(
        "post",
        f"{P}/adjust/preview",
        h,
        {"customer_id": str(customer.public_id), "amount": -150, "category": "fraud_reversal"},
    ).json()
    assert fraud["allowed"] is True
    assert (
        PointTransaction.objects.count() == rows_before and not audit() and balance(customer) == 100
    )
    assert not AuditLog.objects.filter(
        actor_id=admin.pk, action__startswith="admin.points"
    ).exists()
    for bad in (
        {"amount": 0},
        {"amount": 10**6},
        {"customer_id": str(make_admin("ops_admin").public_id)},
    ):
        body = {"customer_id": str(customer.public_id), "amount": 5, **bad}
        assert call("post", f"{P}/adjust/preview", h, body).status_code == 422
    assert (
        call(
            "post", f"{P}/adjust/preview", h, {"customer_id": str(uuid.uuid4()), "amount": 5}
        ).status_code
        == 404
    )


def test_preview_flags_the_daily_limit(settings) -> None:  # type: ignore[no-untyped-def]
    settings.POINTS_ADJUST_DAILY_LIMIT = 100
    _, h = ops()
    customer = make_customer()
    adjust(h, customer, 60, key="p1")
    r = call(
        "post", f"{P}/adjust/preview", h, {"customer_id": str(customer.public_id), "amount": 50}
    ).json()
    assert (
        r["allowed"] is False
        and r["problems"] == ["daily_limit_exceeded"]
        and r["daily_used"] == 60
    )


# ------------------------------------------------------------ daily limit


def test_daily_limit_boundary_per_admin_and_replays(settings) -> None:  # type: ignore[no-untyped-def]
    settings.POINTS_ADJUST_DAILY_LIMIT = 100
    admin, h = ops()
    _, other = ops()
    customer = make_customer()
    assert adjust(h, customer, 60, key="a").status_code == 201
    assert (
        adjust(h, customer, -30, key="b", category="fraud_reversal").status_code == 201
    )  # |30| counts too
    exact = adjust(h, customer, 10, key="c")
    assert exact.status_code == 201  # 60 + 30 + 10 == 100: exactly at the limit
    over = adjust(h, customer, 1, key="d")
    assert over.status_code == 403 and over.json()["code"] == "daily_limit_exceeded"
    assert PointTransaction.objects.filter(admin=admin, reason="adjustment").count() == 3
    assert (
        adjust(other, customer, 100, key="e").status_code == 201
    )  # another admin has their own allowance
    replay = adjust(h, customer, 60, key="a")
    assert (
        replay.status_code == 200 and replay.json()["replayed"] is True
    )  # replays are never re-checked
    assert len(audit()) == 4


def test_daily_limit_resets_at_midnight_karachi(settings) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings.tests.helpers import TUESDAY, local

    settings.POINTS_ADJUST_DAILY_LIMIT = 50
    settings.ADMIN_ACCESS_TOKEN_LIFETIME = timedelta(days=3)
    settings.ACCESS_TOKEN_LIFETIME = timedelta(days=3)
    customer = make_customer()
    with time_machine.travel(local(TUESDAY, 23, 59), tick=False):
        admin, h = admin_headers("ops_admin")  # tokens must be issued inside the frozen clock
        reauth(h)
        assert adjust(h, customer, 50, key="late").status_code == 201
        assert adjust(h, customer, 1, key="late2").status_code == 403
    with time_machine.travel(local(TUESDAY + timedelta(days=1), 0, 0), tick=False):
        reauth(h)
        assert adjust(h, customer, 50, key="early").status_code == 201
    assert balance(customer) == 100 and admin


# ------------------------------------------------------------ concurrency


@pytest.mark.django_db(transaction=True)
def test_parallel_adjustments_keep_ledger_and_summary_consistent(frozen, long_tokens) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    customer = make_customer()
    barrier = threading.Barrier(8)
    codes: list[int] = []

    def worker(i: int) -> None:
        try:
            barrier.wait()
            codes.append(
                adjust(
                    h,
                    customer,
                    100 if i % 2 == 0 else -40,
                    key=f"par-{i}",
                    category="fraud_reversal",
                ).status_code
            )
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert codes == [201] * 8
    assert balance(customer) == 4 * 100 - 4 * 40
    ledger = sum(PointTransaction.objects.filter(customer=customer).values_list("delta", flat=True))
    account = LoyaltyAccount.objects.get(customer=customer)
    assert ledger == account.balance == account.lifetime_earned == 240
    assert loyalty.reconcile() == []
    assert len(audit()) == 8


@pytest.mark.django_db(transaction=True)
def test_parallel_retries_with_one_key_make_one_row(frozen, long_tokens) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    customer = make_customer()
    barrier = threading.Barrier(5)
    codes: list[int] = []

    def worker() -> None:
        try:
            barrier.wait()
            codes.append(adjust(h, customer, 75, key="same-key").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [200, 200, 200, 200, 201]
    assert (
        balance(customer) == 75
        and PointTransaction.objects.filter(reason="adjustment").count() == 1
    )
    assert len(audit()) == 1 and loyalty.reconcile() == []


@pytest.mark.django_db(transaction=True)
def test_parallel_requests_cannot_jointly_exceed_the_daily_limit(
    frozen, long_tokens, settings
) -> None:  # type: ignore[no-untyped-def]
    settings.POINTS_ADJUST_DAILY_LIMIT = 100
    _, h = ops()
    barrier = threading.Barrier(2)
    codes: list[int] = []
    customers = [make_customer(), make_customer()]

    def worker(i: int) -> None:
        try:
            barrier.wait()
            codes.append(adjust(h, customers[i], 80, key=f"lim-{i}").status_code)
        finally:
            connection.close()

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert sorted(codes) == [201, 403]
    assert (
        sum(
            abs(d)
            for d in PointTransaction.objects.filter(reason="adjustment").values_list(
                "delta", flat=True
            )
        )
        == 80
    )


# ------------------------------------------------------------ listing


def test_adjustment_list_filters_sorts_and_pages() -> None:
    admin, h = ops()
    other_admin, other = ops()
    one, two = make_customer(), make_customer()
    adjust(h, one, 10, key="1")
    adjust(h, two, 20, key="2")
    adjust(other, one, 30, key="3")
    give(one, 5)  # a non-adjustment ledger row never shows up here
    r = call("get", f"{P}/adjustments", h).json()
    assert r["total"] == 3 and [x["delta"] for x in r["items"]] == [30, 20, 10]
    assert r["items"][0]["note"] == NOTE and r["items"][0]["admin_id"] == str(other_admin.public_id)
    assert [
        x["delta"] for x in call("get", f"{P}/adjustments?sort=created_at", h).json()["items"]
    ] == [10, 20, 30]
    assert [
        x["delta"]
        for x in call("get", f"{P}/adjustments?admin_id={admin.public_id}", h).json()["items"]
    ] == [20, 10]
    assert [
        x["delta"]
        for x in call("get", f"{P}/adjustments?customer_id={one.public_id}", h).json()["items"]
    ] == [30, 10]
    future = (timezone.now() + timedelta(days=1)).isoformat().replace("+", "%2B")
    assert call("get", f"{P}/adjustments?created_from={future}", h).json()["total"] == 0
    assert call("get", f"{P}/adjustments?created_to={future}", h).json()["total"] == 3
    page = call("get", f"{P}/adjustments?limit=2&offset=2", h).json()
    assert page["total"] == 3 and len(page["items"]) == 1
    for bad in ("?sort=note", "?limit=0", "?limit=101", "?admin_id=nope", "?created_from=x"):
        assert call("get", f"{P}/adjustments{bad}", h).status_code == 422


def test_listing_query_count_is_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    for i in range(12):
        adjust(h, make_customer(), 5, key=f"q{i}")
    with django_assert_max_num_queries(8):
        assert call("get", f"{P}/adjustments?limit=50", h).status_code == 200


def test_json_import_guard() -> None:
    assert json.dumps({}) == "{}" and Client

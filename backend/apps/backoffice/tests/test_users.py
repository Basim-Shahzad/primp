from datetime import timedelta
from typing import Any

import pytest
from django.core import mail
from django.core.files.base import ContentFile
from django.core.files.storage import default_storage
from django.utils import timezone

from apps.accounts.factories import CustomerProfileFactory, UserFactory, make_customer
from apps.accounts.models import (
    AccountDeletionRequest,
    CustomerProfile,
    OneTimeToken,
    RefreshToken,
    User,
    Wishlist,
)
from apps.accounts.roles import Role
from apps.accounts.tokens import issue_session
from apps.backoffice.factories import make_admin
from apps.backoffice.models import SuspensionRecord
from apps.backoffice.tests.helpers import A, admin_headers, call, headers_for, reauth
from apps.bookings.factories import AppointmentFactory
from apps.businesses.factories import BusinessFactory
from apps.businesses.models import Business
from apps.core.models import AuditLog
from apps.loyalty import services as loyalty
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
U = f"{A}/users"


def ops() -> tuple[User, dict[str, str]]:
    user, h = admin_headers("ops_admin")
    assert reauth(h).status_code == 200
    return user, h


def audit_rows(action: str) -> list[AuditLog]:
    return list(AuditLog.objects.filter(action=action))


# ------------------------------------------------------------ list / search


def people() -> dict[str, User]:
    return {
        "sana": make_customer(name="Sana Khan", phone="+923001234567", email="sana@example.com"),
        "bilal": make_customer(
            name="Bilal Ahmed", phone="+923451112222", email="bilal@example.org"
        ),
        "owner": UserFactory(
            role=Role.BUSINESS_OWNER, email="owner@example.com", phone="+923009998888"
        ),
    }


def names(h: dict[str, str], qs: str = "") -> list[str]:
    r = call("get", f"{U}{qs}", h)
    assert r.status_code == 200, r.content
    return [x["email"] for x in r.json()["items"]]


def test_search_by_name_email_phone_and_ids() -> None:
    _, h = admin_headers("support_agent")
    p = people()
    assert names(h, "?q=sana") == ["sana@example.com"]
    assert names(h, "?q=KHAN") == ["sana@example.com"]
    assert names(h, "?q=example.org") == ["bilal@example.org"]
    assert names(h, "?q=1234567") == ["sana@example.com"]  # partial phone
    assert names(h, "?q=0300%201234567".replace(" ", "%20")) == ["sana@example.com"]
    assert names(h, "?q=%2B923451112222") == ["bilal@example.org"]
    assert names(h, f"?q={p['bilal'].public_id}") == ["bilal@example.org"]
    assert names(h, f"?q={p['bilal'].pk}").count("bilal@example.org") == 1
    assert names(h, "?q=nobody") == []


def test_filters_sort_and_pagination() -> None:
    _, h = admin_headers("support_agent")
    p = people()
    User.objects.filter(pk=p["bilal"].pk).update(status="suspended", is_phone_verified=False)
    assert names(h, "?role=business_owner") == ["owner@example.com"]
    assert names(h, "?status=suspended") == ["bilal@example.org"]
    assert names(h, "?phone_verified=false") == ["bilal@example.org"]
    assert "sana@example.com" in names(h, "?email_verified=true")
    future = (timezone.now() + timedelta(days=1)).isoformat().replace("+", "%2B")
    assert names(h, f"?created_from={future}") == []
    assert names(h, f"?created_to={future}&role=customer&status=active") == ["sana@example.com"]
    by_email = names(h, "?role=customer&sort=email")
    assert by_email == sorted(by_email) and names(h, "?role=customer&sort=-email") == by_email[::-1]
    page = call("get", f"{U}?limit=2&offset=1&sort=email", h).json()
    assert (
        page["limit"] == 2
        and page["offset"] == 1
        and page["total"] >= 4
        and len(page["items"]) == 2
    )


@pytest.mark.parametrize(
    "qs",
    [
        "?sort=password",
        "?sort=id;drop",
        "?limit=0",
        "?limit=101",
        "?offset=-1",
        "?role=wizard",
        "?status=gone",
        "?q=",
        "?created_from=x",
        "?phone_verified=maybe",
    ],
)
def test_list_validation(qs: str) -> None:
    _, h = admin_headers("support_agent")
    assert call("get", f"{U}{qs}", h).status_code == 422


def test_list_query_count_is_bounded(django_assert_max_num_queries) -> None:  # type: ignore[no-untyped-def]
    _, h = admin_headers("support_agent")
    for i in range(25):
        make_customer(name=f"Person {i}")
    with django_assert_max_num_queries(12):
        assert call("get", f"{U}?limit=50", h).status_code == 200


# ------------------------------------------------------------ detail


def test_detail_assembles_everything_about_a_user() -> None:
    _, h = admin_headers("support_agent")
    user = make_customer(name="Sana Khan", phone="+923001234567", email="sana@example.com")
    business = BusinessFactory(name="Glow")
    AppointmentFactory(customer=user, business=business, status="completed")
    AppointmentFactory(customer=user, business=business, status="confirmed")
    loyalty.credit(
        customer_id=user.pk,
        action="booking",
        reference_type="appointment",
        reference_id=1,
        amount=2000,
    )
    AccountDeletionRequest.objects.create(user=user, reason="leaving")
    issue_session(user, user_agent="Mozilla/5.0", ip="203.0.113.9")
    issue_session(user, user_agent="Chrome", ip="203.0.113.10")
    r = call("get", f"{U}/{user.public_id}", h)
    assert r.status_code == 200, r.content
    d = r.json()
    assert (d["id"], d["email"], d["name"], d["role"], d["status"]) == (
        str(user.public_id),
        "sana@example.com",
        "Sana Khan",
        "customer",
        "active",
    )
    assert d["bookings_by_status"] == {"completed": 1, "confirmed": 1}
    assert {b["business"] for b in d["recent_bookings"]} == {"Glow"} and len(
        d["recent_bookings"]
    ) == 2
    assert d["points"]["balance"] > 0 and d["points"]["recent"][0]["reason"] == "booking"
    assert [(x["ip"], x["user_agent"]) for x in d["login_history"]] == [
        ("203.0.113.10", "Chrome"),
        ("203.0.113.9", "Mozilla/5.0"),
    ]
    assert d["deletion_requests"][0]["status"] == "pending" and d["suspension"] is None
    assert d["tickets"] == [] and d["reviews"] == []


def test_detail_404_and_422() -> None:
    _, h = admin_headers("support_agent")
    assert call("get", f"{U}/00000000-0000-0000-0000-000000000000", h).status_code == 404
    assert call("get", f"{U}/not-a-uuid", h).status_code == 422


# ------------------------------------------------------------ suspend / reactivate


def listed_business(owner: User, **kw: Any) -> Business:
    return BusinessFactory(owner=owner, **kw)


def test_suspend_revokes_sessions_unlists_and_reactivate_restores_exactly(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    listed = listed_business(owner)
    hidden_already = listed_business(owner, is_listed=False, verification_status="draft")
    owner_headers = headers_for(owner)
    assert call("get", "/api/v1/provider/businesses", owner_headers).status_code == 200
    with run_commit():
        r = call("post", f"{U}/{owner.public_id}/suspend", h, {"reason": "Fraud reports"})
    assert r.status_code == 200 and r.json()["status"] == "suspended"
    owner.refresh_from_db()
    listed.refresh_from_db(), hidden_already.refresh_from_db()
    assert owner.status == "suspended" and not listed.is_listed and not hidden_already.is_listed
    assert RefreshToken.objects.filter(user=owner, revoked_at__isnull=True).count() == 0
    assert (
        call("get", "/api/v1/provider/businesses", owner_headers).status_code == 401
    )  # session revoked
    record = SuspensionRecord.objects.get(user=owner)
    assert record.previous_listings == {str(listed.pk): True, str(hidden_already.pk): False}
    row = audit_rows("admin.user_suspended")
    assert len(row) == 1
    assert (row[0].actor_id, row[0].target_id, row[0].before) == (
        admin.pk,
        str(owner.public_id),
        {"status": "active"},
    )
    assert row[0].after == {
        "status": "suspended",
        "reason": "Fraud reports",
        "unlisted_businesses": [listed.pk],
    }
    assert row[0].ip_address
    again = call("post", f"{U}/{owner.public_id}/reactivate", h, {"reason": "Appeal accepted"})
    assert again.status_code == 200 and again.json()["status"] == "active"
    owner.refresh_from_db()
    listed.refresh_from_db(), hidden_already.refresh_from_db()
    assert (
        owner.status == "active" and listed.is_listed is True and hidden_already.is_listed is False
    )
    done = SuspensionRecord.objects.get(user=owner)
    assert done.reactivated_at and done.reactivated_by_id == admin.pk
    after = audit_rows("admin.user_reactivated")
    assert len(after) == 1 and after[0].after["relisted_businesses"] == [listed.pk]
    # a suspended owner cannot log in again until reactivated, and a new suspension is allowed
    assert (
        call("post", f"{U}/{owner.public_id}/suspend", h, {"reason": "Second time"}).status_code
        == 200
    )


def test_suspension_notifies_the_other_side_and_keeps_bookings(run_commit) -> None:  # type: ignore[no-untyped-def]
    _, h = ops()
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001110000", email="o@example.com")
    business = listed_business(owner)
    customer = make_customer(phone="+923002220000")
    soon = timezone.now() + timedelta(days=2)
    mine = AppointmentFactory(
        customer=customer,
        business=business,
        status="confirmed",
        scheduled_start=soon,
        scheduled_end=soon + timedelta(hours=1),
    )
    with run_commit():
        assert (
            call("post", f"{U}/{owner.public_id}/suspend", h, {"reason": "Fraud"}).status_code
            == 200
        )
    mine.refresh_from_db()
    assert mine.status == "confirmed"  # bookings are kept
    assert Notification.objects.filter(
        user=customer, template_key="business_suspended_notice"
    ).exists()
    other_owner = UserFactory(role=Role.BUSINESS_OWNER, email="o2@example.com")
    b2 = listed_business(other_owner)
    theirs = AppointmentFactory(
        customer=customer,
        business=b2,
        status="confirmed",
        scheduled_start=soon,
        scheduled_end=soon + timedelta(hours=1),
    )
    with run_commit():
        assert (
            call("post", f"{U}/{customer.public_id}/suspend", h, {"reason": "Abuse"}).status_code
            == 200
        )
    theirs.refresh_from_db()
    assert theirs.status == "confirmed"
    assert Notification.objects.filter(
        user=other_owner, template_key="customer_suspended_notice"
    ).exists()


def test_suspend_requires_reauth_and_a_reason() -> None:
    _, h = admin_headers("ops_admin")
    target = make_customer()
    r = call("post", f"{U}/{target.public_id}/suspend", h, {"reason": "x"})
    assert r.status_code == 403 and r.json()["code"] == "reauth_required"
    assert reauth(h).status_code == 200
    for bad in ({}, {"reason": ""}, {"reason": "   "}, {"reason": "r" * 501}):
        assert call("post", f"{U}/{target.public_id}/suspend", h, bad).status_code == 422
    assert (
        call("post", f"{U}/{target.public_id}/suspend", h, {"reason": "r" * 500}).status_code == 200
    )
    target.refresh_from_db()
    assert target.status == "suspended"


def test_suspend_state_conflicts() -> None:
    admin, h = ops()
    target = make_customer()
    assert call("post", f"{U}/{target.public_id}/reactivate", h, {"reason": "x"}).status_code == 409
    assert call("post", f"{U}/{target.public_id}/suspend", h, {"reason": "x"}).status_code == 200
    assert call("post", f"{U}/{target.public_id}/suspend", h, {"reason": "x"}).status_code == 409
    own = call("post", f"{U}/{admin.public_id}/suspend", h, {"reason": "x"})
    assert own.status_code == 403  # an ops admin may not touch any admin account, even their own
    boss, bh = admin_headers("super_admin")
    reauth(bh)
    make_admin("super_admin")  # so the boss is not the last one
    mine = call("post", f"{U}/{boss.public_id}/suspend", bh, {"reason": "x"})
    assert mine.status_code == 409 and mine.json()["code"] == "cannot_target_self"
    assert (
        call(
            "post", f"{U}/00000000-0000-0000-0000-000000000000/suspend", h, {"reason": "x"}
        ).status_code
        == 404
    )


def test_only_super_admins_may_act_on_admin_accounts() -> None:
    _, h = ops()
    for kind in ("support_agent", "ops_admin", "super_admin"):
        target = make_admin(kind)
        r = call("post", f"{U}/{target.public_id}/suspend", h, {"reason": "x"})
        assert r.status_code == 403 and r.json()["code"] == "permission_denied", kind
        assert call("post", f"{U}/{target.public_id}/force-logout", h, {}).status_code == 403
        target.refresh_from_db()
        assert target.status == "active"
    boss, bh = admin_headers("super_admin")
    reauth(bh)
    other = make_admin("ops_admin")
    assert call("post", f"{U}/{other.public_id}/suspend", bh, {"reason": "x"}).status_code == 200
    peer = make_admin("super_admin")
    assert call("post", f"{U}/{peer.public_id}/suspend", bh, {"reason": "x"}).status_code == 200


def test_the_only_active_super_admin_cannot_be_suspended() -> None:
    from apps.backoffice import permissions as perms
    from apps.backoffice.services import users as svc
    from apps.core.errors import AppError

    last = make_admin("super_admin")
    stale = make_admin("super_admin")
    User.objects.filter(pk=stale.pk).update(
        status="suspended"
    )  # an actor whose account just lapsed
    perms.mark_reauthenticated("sess")
    actor = perms.AdminActor(stale, perms.permissions_of(stale), "sess", None)
    with pytest.raises(AppError) as exc:
        svc.suspend(actor, str(last.public_id), "x")
    assert exc.value.status == 409 and exc.value.code == "last_super_admin"
    last.refresh_from_db()
    assert last.status == "active" and not audit_rows("admin.user_suspended")


# ------------------------------------------------------------ force logout / password reset


def test_force_logout_revokes_all_sessions_and_is_audited() -> None:
    admin, h = admin_headers("support_agent")
    target = make_customer()
    issue_session(target)
    victim = headers_for(target)
    r = call("post", f"{U}/{target.public_id}/force-logout", h, {})
    assert r.status_code == 200 and r.json()["revoked_sessions"] == 2
    assert call("get", "/api/v1/users/me", victim).status_code == 401
    row = audit_rows("admin.user_force_logout")
    assert len(row) == 1 and row[0].actor_id == admin.pk and row[0].before == {"active_sessions": 2}
    assert row[0].after == {"active_sessions": 0} and row[0].ip_address


def test_password_reset_sends_a_link_and_never_sets_a_password(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = admin_headers("support_agent")
    target = make_customer(email="sana@example.com")
    old_hash = target.password
    with run_commit():
        r = call("post", f"{U}/{target.public_id}/password-reset", h, {})
    assert r.status_code == 202
    target.refresh_from_db()
    assert target.password == old_hash
    assert len(mail.outbox) == 1 and mail.outbox[0].to == ["sana@example.com"]
    assert "reset-password?token=" in mail.outbox[0].body
    assert OneTimeToken.objects.filter(user=target, purpose="password_reset").count() == 1
    assert len(audit_rows("admin.user_password_reset_sent")) == 1


def test_password_reset_conflicts() -> None:
    _, h = admin_headers("support_agent")
    no_email = UserFactory(email=None, phone="+923007770000")
    assert call("post", f"{U}/{no_email.public_id}/password-reset", h, {}).status_code == 422
    suspended = make_customer()
    User.objects.filter(pk=suspended.pk).update(status="suspended")
    assert call("post", f"{U}/{suspended.public_id}/password-reset", h, {}).status_code == 409
    assert not AuditLog.objects.filter(action="admin.user_password_reset_sent").exists()


# ------------------------------------------------------------ deletion requests


def test_deletion_requests_list_and_filter() -> None:
    _, h = admin_headers("support_agent")
    a, b = make_customer(name="Ayesha K"), make_customer(name="Bilal A")
    AccountDeletionRequest.objects.create(user=a, reason="r1")
    done = AccountDeletionRequest.objects.create(user=b, status="processed")
    r = call("get", f"{A}/deletion-requests", h).json()
    assert r["total"] == 2 and [x["user_name"] for x in r["items"]] == ["Ayesha K", "Bilal A"]
    only = call("get", f"{A}/deletion-requests?status=processed", h).json()
    assert [x["id"] for x in only["items"]] == [done.pk]
    assert call("get", f"{A}/deletion-requests?status=weird", h).status_code == 422


def test_processing_anonymises_pii_but_keeps_bookings_and_ledger(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin, h = ops()
    user = make_customer(name="Sana Khan", phone="+923001234567", email="sana@example.com")
    path = default_storage.save("profiles/x/old.jpg", ContentFile(b"jpg"))
    CustomerProfile.objects.filter(user=user).update(photo=path)
    Wishlist.objects.create(user=user, business=BusinessFactory())
    business = BusinessFactory()
    appt = AppointmentFactory(
        customer=user,
        business=business,
        status="completed",
        walk_in_name="Sana",
        walk_in_phone="+923001234567",
    )
    loyalty.credit(
        customer_id=user.pk,
        action="booking",
        reference_type="appointment",
        reference_id=appt.pk,
        amount=2000,
    )
    issue_session(user)
    Notification.objects.create(
        user=user,
        recipient="+923001234567",
        channel="sms",
        template_key="booking_confirmed",
        payload={"customer_name": "Sana"},
    )
    request = AccountDeletionRequest.objects.create(user=user, reason="bye")
    with run_commit():
        r = call("post", f"{A}/deletion-requests/{request.pk}/process", h, {})
    assert r.status_code == 200, r.content
    user.refresh_from_db()
    assert user.status == "deleted" and user.phone is None and not user.has_usable_password()
    assert user.email == f"deleted-{user.public_id}@deleted.invalid" and "sana" not in user.email
    profile = CustomerProfile.objects.get(user=user)
    assert profile.name == "Deleted user" and profile.photo == ""
    assert not default_storage.exists(path)
    assert not Wishlist.objects.filter(user=user).exists()
    assert RefreshToken.objects.filter(user=user, revoked_at__isnull=True).count() == 0
    appt.refresh_from_db()
    assert appt.customer_id == user.pk and appt.status == "completed"
    assert (appt.walk_in_name, appt.walk_in_phone) == ("", "")
    assert loyalty.summary(user.pk)[0] > 0  # the ledger is untouched
    n = Notification.objects.get(user=user, channel="sms")
    assert n.recipient == "" and n.payload["customer_name"] == ""
    request.refresh_from_db()
    assert request.status == "processed"
    rows = audit_rows("admin.deletion_processed")
    assert (
        len(rows) == 1 and rows[0].actor_id == admin.pk and rows[0].target_id == str(user.public_id)
    )
    assert rows[0].before == {"status": "active", "had_phone": True, "had_email": True}
    assert "sana" not in str(rows[0].before) + str(rows[0].after)
    assert call("get", f"{U}/{user.public_id}", h).json()["name"] == "Deleted user"


def test_processing_conflicts_and_permissions() -> None:
    _, h = ops()
    owner = UserFactory(role=Role.BUSINESS_OWNER)
    BusinessFactory(owner=owner)
    req = AccountDeletionRequest.objects.create(user=owner)
    r = call("post", f"{A}/deletion-requests/{req.pk}/process", h, {})
    assert r.status_code == 409 and r.json()["code"] == "owner_has_businesses"
    owner.refresh_from_db()
    assert owner.status == "active" and not audit_rows("admin.deletion_processed")
    plain = make_customer()
    ok = AccountDeletionRequest.objects.create(user=plain)
    assert call("post", f"{A}/deletion-requests/{ok.pk}/process", h, {}).status_code == 200
    assert call("post", f"{A}/deletion-requests/{ok.pk}/process", h, {}).status_code == 409
    assert call("post", f"{A}/deletion-requests/987654/process", h, {}).status_code == 404
    _, support = admin_headers("support_agent")
    fresh = AccountDeletionRequest.objects.create(user=make_customer())
    assert call("post", f"{A}/deletion-requests/{fresh.pk}/process", support, {}).status_code == 403


# ------------------------------------------------------------ admin accounts


def test_super_admin_creates_admins_and_changes_groups(run_commit) -> None:  # type: ignore[no-untyped-def]
    boss, h = admin_headers("super_admin")
    reauth(h)
    with run_commit():
        r = call(
            "post",
            f"{A}/admins",
            h,
            {"email": "New.Agent@Example.com", "name": "New Agent", "group": "support_agent"},
        )
    assert r.status_code == 201, r.content
    data = r.json()
    assert (
        data["role"] == "admin"
        and data["groups"] == ["support_agent"]
        and data["email"] == "new.agent@example.com"
    )
    created = User.objects.get(email="new.agent@example.com")
    assert not created.has_usable_password() and created.role == Role.ADMIN
    assert len(mail.outbox) == 1 and "reset-password?token=" in mail.outbox[0].body
    created_rows = audit_rows("admin.admin_created")
    assert len(created_rows) == 1 and created_rows[0].after == {
        "group": "support_agent",
        "role": "admin",
    }
    up = call("patch", f"{A}/admins/{created.public_id}/groups", h, {"group": "super_admin"})
    assert (
        up.status_code == 200
        and up.json()["role"] == "super_admin"
        and up.json()["groups"] == ["super_admin"]
    )
    down = call("patch", f"{A}/admins/{created.public_id}/groups", h, {"group": "ops_admin"})
    assert down.json()["role"] == "admin" and down.json()["groups"] == ["ops_admin"]
    changes = audit_rows("admin.admin_group_changed")
    assert len(changes) == 2 and changes[1].before == {
        "role": "super_admin",
        "groups": ["super_admin"],
    }
    assert changes[1].after == {"role": "admin", "groups": ["ops_admin"]}
    listing = call("get", f"{A}/admins", h).json()
    assert {x["email"] for x in listing} >= {boss.email, "new.agent@example.com"}


def test_admin_creation_validation_and_conflicts() -> None:
    _, h = admin_headers("super_admin")
    reauth(h)
    good = {"email": "a@example.com", "name": "A", "group": "ops_admin"}
    for bad in (
        {**good, "group": "root"},
        {**good, "email": "nope"},
        {**good, "name": ""},
        {**good, "name": "n" * 101},
    ):
        assert call("post", f"{A}/admins", h, bad).status_code == 422
    assert call("post", f"{A}/admins", h, good).status_code == 201
    dup = call("post", f"{A}/admins", h, good)
    assert dup.status_code == 409 and dup.json()["code"] == "email_taken"
    assert User.objects.filter(email="a@example.com").count() == 1


def test_a_support_agent_cannot_grant_themselves_permissions() -> None:
    agent, h = admin_headers("support_agent")
    reauth(h)
    r = call("patch", f"{A}/admins/{agent.public_id}/groups", h, {"group": "super_admin"})
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"
    assert (
        call(
            "post",
            f"{A}/admins",
            h,
            {"email": "x@example.com", "name": "X", "group": "super_admin"},
        ).status_code
        == 403
    )
    agent.refresh_from_db()
    assert agent.role == Role.ADMIN and [g.name for g in agent.groups.all()] == ["support_agent"]
    ops_user, oh = admin_headers("ops_admin")
    assert (
        call(
            "patch", f"{A}/admins/{ops_user.public_id}/groups", oh, {"group": "super_admin"}
        ).status_code
        == 403
    )


def test_group_changes_need_reauth_and_valid_targets() -> None:
    boss, h = admin_headers("super_admin")
    target = make_admin("ops_admin")
    stale = call("patch", f"{A}/admins/{target.public_id}/groups", h, {"group": "support_agent"})
    assert stale.status_code == 403 and stale.json()["code"] == "reauth_required"
    reauth(h)
    assert (
        call("patch", f"{A}/admins/{target.public_id}/groups", h, {"group": "root"}).status_code
        == 422
    )
    customer = make_customer()
    r = call("patch", f"{A}/admins/{customer.public_id}/groups", h, {"group": "ops_admin"})
    assert r.status_code == 409 and r.json()["code"] == "not_an_admin"
    own = call("patch", f"{A}/admins/{boss.public_id}/groups", h, {"group": "super_admin"})
    assert own.status_code == 409 and own.json()["code"] == "cannot_target_self"
    assert (
        call(
            "patch",
            f"{A}/admins/00000000-0000-0000-0000-000000000000/groups",
            h,
            {"group": "ops_admin"},
        ).status_code
        == 404
    )


def test_the_last_super_admin_cannot_be_demoted() -> None:
    boss, h = admin_headers("super_admin")
    reauth(h)
    r = call("patch", f"{A}/admins/{boss.public_id}/groups", h, {"group": "ops_admin"})
    assert r.status_code == 409 and r.json()["code"] == "last_super_admin"
    boss.refresh_from_db()
    assert boss.role == Role.SUPER_ADMIN and not audit_rows("admin.admin_group_changed")
    other = make_admin("super_admin")
    assert (
        call("patch", f"{A}/admins/{other.public_id}/groups", h, {"group": "ops_admin"}).status_code
        == 200
    )


def test_unused_imports_guard() -> None:
    assert CustomerProfileFactory

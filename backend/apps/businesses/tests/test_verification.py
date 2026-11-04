import pytest
from django.test import Client

from apps.accounts.factories import UserFactory
from apps.accounts.roles import Role
from apps.businesses import events, verification
from apps.businesses.factories import BusinessFactory
from apps.businesses.models import Business, VerificationStatus
from apps.businesses.tests.provider_helpers import P, call, owner_headers
from apps.core.errors import AppError
from apps.core.models import AuditLog
from apps.notifications.models import Notification

pytestmark = pytest.mark.django_db
ALL = [s.value for s in VerificationStatus]
PUBLIC = "/api/v1/businesses"
SEARCH = "/api/v1/search/businesses"


def admin_user() -> object:
    return UserFactory(role=Role.ADMIN)


def biz(status: str, **kw: object) -> Business:
    owner = UserFactory(role=Role.BUSINESS_OWNER, phone="+923001112222", email="owner@example.com")
    return BusinessFactory(
        owner=owner, verification_status=status, is_listed=status == "verified", **kw
    )


ACTIONS = {
    "mark_verified": (
        (lambda b, a: verification.mark_verified(b, a)),
        {"pending", "revoked"},
        "verified",
    ),
    "revoke_verification": (
        (lambda b, a: verification.revoke_verification(b, a, "Fake documents")),
        {"verified"},
        "revoked",
    ),
    "request_more_info": (
        (lambda b, a: verification.request_more_info(b, a, "Upload a clearer CNIC")),
        {"pending"},
        "needs_info",
    ),
    "reject": (
        (lambda b, a: verification.reject(b, a, "Not a salon")),
        {"pending", "needs_info"},
        "rejected",
    ),
}


@pytest.mark.parametrize("status", ALL)
@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_transition_matrix(status: str, action: str, run_commit) -> None:  # type: ignore[no-untyped-def]
    fn, allowed_from, target = ACTIONS[action]
    b = biz(status)
    admin = admin_user()
    if status in allowed_from:
        with run_commit():
            result = fn(b, admin)
        assert result.verification_status == target
        b.refresh_from_db()
        assert b.verification_status == target
    else:
        with pytest.raises(AppError) as exc:
            fn(b, admin)
        assert exc.value.status == 409 and exc.value.code == "invalid_transition"
        b.refresh_from_db()
        assert b.verification_status == status
        assert not AuditLog.objects.filter(target_id=str(b.public_id)).exists()
        assert not Notification.objects.exists()


@pytest.mark.parametrize("fn_name", sorted(ACTIONS))
def test_only_admins_may_call(fn_name: str) -> None:
    fn = ACTIONS[fn_name][0]
    b = biz("pending" if fn_name != "revoke_verification" else "verified")
    for role in (Role.CUSTOMER, Role.BUSINESS_OWNER, Role.STAFF):
        with pytest.raises(AppError) as exc:
            fn(b, UserFactory(role=role))
        assert exc.value.status == 403
    assert Business.objects.get(pk=b.pk).verification_status in ("pending", "verified")
    assert fn(b, UserFactory(role=Role.SUPER_ADMIN))


def test_mark_verified_lists_stamps_audits_notifies_and_emits(run_commit) -> None:  # type: ignore[no-untyped-def]
    b = biz("pending", name="Glow")
    admin = admin_user()
    seen: list[tuple[int, str]] = []

    def handler(sender: object, business_id: int, status: str, **kw: object) -> None:
        seen.append((business_id, status))

    events.business_verification_changed.connect(handler, weak=False)
    try:
        with run_commit():
            verification.mark_verified(b, admin)
    finally:
        events.business_verification_changed.disconnect(handler)
    b.refresh_from_db()
    assert b.is_listed and b.is_verified and b.verified_by_id == admin.pk and b.verified_at
    assert b.status_message == "" and b.revoked_reason == ""
    audit = AuditLog.objects.get(action="business.verified")
    assert audit.actor_id == admin.pk and audit.target_id == str(b.public_id)
    assert audit.before == {
        "status": "pending",
        "is_listed": False,
        "status_message": "",
        "revoked_reason": "",
        "verified_by": None,
    }
    assert audit.after["status"] == "verified" and audit.after["is_listed"] is True
    assert audit.after["verified_by"] == admin.pk
    assert seen == [(b.pk, "verified")]
    n = Notification.objects.filter(
        user=b.owner, template_key="onboarding_status_changed", channel="in_app"
    )
    assert n.count() == 1 and n.get().payload["status"] == "verified"


def test_messages_are_stored_audited_and_sent_to_the_owner(run_commit) -> None:  # type: ignore[no-untyped-def]
    admin = admin_user()
    b = biz("pending")
    with run_commit():
        verification.request_more_info(b, admin, "  Upload a clearer CNIC  ")
    b.refresh_from_db()
    assert b.status_message == "Upload a clearer CNIC" and b.is_listed is False
    sms = Notification.objects.get(user=b.owner, channel="sms")
    assert (
        sms.payload["message"] == "Upload a clearer CNIC" and sms.payload["status"] == "needs info"
    )
    assert (
        AuditLog.objects.get(action="business.more_info_requested").after["status_message"]
        == "Upload a clearer CNIC"
    )
    with run_commit():
        verification.reject(b, admin, "Not a salon")
    b.refresh_from_db()
    assert b.verification_status == "rejected" and b.status_message == "Not a salon"
    assert AuditLog.objects.get(action="business.rejected").before["status"] == "needs_info"


@pytest.mark.parametrize("fn_name", ["revoke_verification", "request_more_info", "reject"])
@pytest.mark.parametrize("message", ["", "   ", "x" * 501])
def test_message_required_and_bounded(fn_name: str, message: str) -> None:
    status = "verified" if fn_name == "revoke_verification" else "pending"
    b = biz(status)
    fn = {
        "revoke_verification": verification.revoke_verification,
        "request_more_info": verification.request_more_info,
        "reject": verification.reject,
    }[fn_name]
    with pytest.raises(AppError) as exc:
        fn(b, admin_user(), message)
    assert exc.value.status == 422
    assert Business.objects.get(pk=b.pk).verification_status == status
    ok = fn(b, admin_user(), "x" * 500)
    assert ok.status_message == "x" * 500


def test_revoke_removes_badge_immediately_everywhere(run_commit) -> None:  # type: ignore[no-untyped-def]
    b = biz("verified", name="Glow Studio")
    admin = admin_user()

    def badge_public() -> bool:
        return bool(Client().get(f"{PUBLIC}/{b.public_id}").json()["is_verified"])

    def in_search(verified_only: bool) -> list[str]:
        qs = "?verified_only=true" if verified_only else ""
        items = Client().get(f"{SEARCH}{qs}").json()["items"]
        return [i["name"] for i in items]

    def search_badge() -> bool | None:
        for item in Client().get(SEARCH).json()["items"]:
            if item["id"] == str(b.public_id):
                return bool(item["is_verified"])
        return None

    assert badge_public() is True  # populates the page cache
    assert in_search(True) == ["Glow Studio"] and search_badge() is True
    with run_commit():
        verification.revoke_verification(b, admin, "Fake documents")
    assert badge_public() is False  # served fresh: cache invalidated
    assert in_search(True) == [] and search_badge() is False  # badge gone, listing untouched
    assert in_search(False) == ["Glow Studio"]
    b.refresh_from_db()
    assert b.revoked_reason == "Fake documents" and b.status_message == "Fake documents"
    audit = AuditLog.objects.get(action="business.verification_revoked")
    assert (
        audit.before["status"] == "verified" and audit.after["revoked_reason"] == "Fake documents"
    )
    with run_commit():
        verification.mark_verified(b, admin)  # reinstating shows the badge again at once
    assert badge_public() is True and in_search(True) == ["Glow Studio"]
    b.refresh_from_db()
    assert b.revoked_reason == ""


def test_approval_lists_the_business_and_search_reacts(run_commit) -> None:  # type: ignore[no-untyped-def]
    b = biz("pending", name="New Salon")
    assert Client().get(f"{PUBLIC}/{b.public_id}").status_code == 404
    with run_commit():
        verification.mark_verified(b, admin_user())
    assert Client().get(f"{PUBLIC}/{b.public_id}").json()["is_verified"] is True
    assert [i["name"] for i in Client().get(f"{SEARCH}?verified_only=true").json()["items"]] == [
        "New Salon"
    ]


def test_cache_invalidation_and_frontend_revalidation_on_each_change(run_commit, settings) -> None:  # type: ignore[no-untyped-def]
    from unittest import mock

    settings.FRONTEND_REVALIDATE_URL = "https://front.example/revalidate"
    settings.FRONTEND_REVALIDATE_SECRET = "s3cret"
    b = biz("verified")
    with mock.patch("apps.businesses.tasks.requests.post") as post, run_commit():
        verification.revoke_verification(b, admin_user(), "Complaint")
    assert post.called


def test_owner_sees_status_and_messages(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner, h = owner_headers()
    b = BusinessFactory(owner=owner, verification_status="pending", is_listed=False)
    admin = admin_user()
    base = f"{P}/businesses/{b.public_id}"
    assert call("get", base, h).json()["status"] == "pending"
    with run_commit():
        verification.request_more_info(b, admin, "Add the storefront photo")
    detail, checklist = call("get", base, h).json(), call("get", f"{base}/onboarding", h).json()
    assert detail["status"] == checklist["status"] == "needs_info"
    assert detail["status_message"] == checklist["status_message"] == "Add the storefront photo"
    assert detail["is_listed"] is False and detail["is_verified"] is False
    Business.objects.filter(pk=b.pk).update(verification_status="pending")
    with run_commit():
        verification.mark_verified(b, admin)
    detail = call("get", base, h).json()
    assert detail["status"] == "verified" and detail["is_verified"] is True
    assert detail["verified_at"] and detail["is_listed"] is True and detail["status_message"] == ""
    with run_commit():
        verification.revoke_verification(b, admin, "Fake documents")
    detail = call("get", base, h).json()
    assert detail["status"] == "revoked" and detail["revoked_reason"] == "Fake documents"
    assert detail["is_verified"] is False


def test_concurrent_verification_changes_do_not_both_apply() -> None:
    b = biz("pending")
    admin = admin_user()
    verification.mark_verified(b, admin)
    with pytest.raises(AppError) as exc:  # the second admin acted on stale information
        verification.reject(b, admin_user(), "Too late")
    assert exc.value.code == "invalid_transition"
    assert Business.objects.get(pk=b.pk).verification_status == "verified"

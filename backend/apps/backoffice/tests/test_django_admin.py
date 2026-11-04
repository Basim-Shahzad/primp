import uuid

import pytest
from django.contrib import admin
from django.test import Client
from django.urls import reverse

from apps.backoffice.factories import make_admin
from apps.core.admin_base import ReadOnlyAdmin
from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db

READ_ONLY = {
    "core.auditlog",
    "accounts.refreshtoken",
    "bookings.appointment",
    "payments.paymentwebhookevent",
    "businesses.businessdocument",
    "backoffice.platformsettinghistory",
    "loyalty.pointtransaction",
    "loyalty.loyaltyaccount",
    "loyalty.pointrule",
}


def staff_client(kind: str = "super_admin") -> Client:
    user = make_admin(kind)
    if kind == "super_admin":
        user.is_superuser = True
        user.save(update_fields=["is_superuser"])
    c = Client()
    c.force_login(user)
    return c


def test_sensitive_models_are_registered_read_only() -> None:
    registered = {m._meta.label_lower: a for m, a in admin.site._registry.items()}
    assert set(registered) >= READ_ONLY
    for label in READ_ONLY:
        assert isinstance(registered[label], ReadOnlyAdmin), label


def test_every_registered_page_renders_and_nothing_can_be_changed() -> None:
    c = staff_client()
    AuditLog.objects.create(action="x.y", target_type="t", target_id="1")
    for model in admin.site._registry:
        name = f"admin:{model._meta.app_label}_{model._meta.model_name}"
        assert c.get(reverse(f"{name}_changelist")).status_code == 200, name
        if model._meta.label_lower in READ_ONLY:
            assert c.get(reverse(f"{name}_add")).status_code == 403, name
            assert c.post(reverse(f"{name}_add"), {}).status_code == 403, name


def test_read_only_pages_reject_edits_and_deletes() -> None:
    c = staff_client()
    row = AuditLog.objects.create(action="x.y")
    url = reverse("admin:core_auditlog_change", args=[row.pk])
    assert c.get(url).status_code == 200  # viewing is allowed
    assert c.post(url, {"action": "changed"}).status_code == 403
    assert (
        c.post(reverse("admin:core_auditlog_delete", args=[row.pk]), {"post": "yes"}).status_code
        == 403
    )
    row.refresh_from_db()
    assert row.action == "x.y"
    bulk = c.post(
        reverse("admin:core_auditlog_changelist"),
        {"action": "delete_selected", "_selected_action": [row.pk], "post": "yes"},
    )
    assert bulk.status_code in (200, 302) and AuditLog.objects.filter(pk=row.pk).exists()


def test_documents_and_sessions_never_show_secrets() -> None:
    from apps.accounts.models import RefreshToken
    from apps.businesses.factories import BusinessFactory
    from apps.businesses.models import BusinessDocument

    c = staff_client()
    biz = BusinessFactory()
    doc = BusinessDocument.objects.create(
        business=biz,
        kind="license",
        file="private/secret-path/doc.pdf",
        content_type="application/pdf",
        size=10,
    )
    page = c.get(reverse("admin:businesses_businessdocument_change", args=[doc.pk]))
    assert page.status_code == 200 and b"secret-path" not in page.content
    listing = c.get(reverse("admin:businesses_businessdocument_changelist"))
    assert b"secret-path" not in listing.content
    admin_user = make_admin("ops_admin")
    token = RefreshToken.objects.create(
        user=admin_user,
        token_hash="f" * 64,
        family_id=uuid.uuid4(),
        expires_at="2099-01-01T00:00:00Z",
    )
    page = c.get(reverse("admin:accounts_refreshtoken_change", args=[token.pk]))
    assert page.status_code == 200 and b"f" * 64 not in page.content


def test_admins_without_django_permissions_cannot_use_the_admin_site() -> None:
    c = staff_client("ops_admin")
    assert c.get(reverse("admin:core_auditlog_changelist")).status_code == 403


def test_ip_allow_list_hides_the_admin_from_other_addresses(settings) -> None:  # type: ignore[no-untyped-def]
    c = staff_client()
    settings.ADMIN_ALLOWED_IPS = ["10.1.2.3"]
    assert c.get("/admin/", REMOTE_ADDR="198.51.100.9").status_code == 404
    assert c.get("/admin/", REMOTE_ADDR="10.1.2.3").status_code == 200
    # the API is not behind the allow-list
    assert c.get("/healthz", REMOTE_ADDR="198.51.100.9").status_code == 200


def test_empty_allow_list_allows_everyone(settings) -> None:  # type: ignore[no-untyped-def]
    c = staff_client()
    settings.ADMIN_ALLOWED_IPS = []
    assert c.get("/admin/", REMOTE_ADDR="198.51.100.9").status_code == 200


def test_allow_list_uses_the_trusted_proxy_client_address(settings) -> None:  # type: ignore[no-untyped-def]
    c = staff_client()
    settings.ADMIN_ALLOWED_IPS = ["10.1.2.3"]
    settings.TRUSTED_PROXY_COUNT = 1
    spoof = {"HTTP_X_FORWARDED_FOR": "10.1.2.3, 198.51.100.9"}
    assert c.get("/admin/", REMOTE_ADDR="172.16.0.1", **spoof).status_code == 404
    ok = {"HTTP_X_FORWARDED_FOR": "10.1.2.3"}
    assert c.get("/admin/", REMOTE_ADDR="172.16.0.1", **ok).status_code == 200

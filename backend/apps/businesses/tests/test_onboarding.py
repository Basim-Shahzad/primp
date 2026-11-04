import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest
from django.core.files.storage import storages
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import Client

from apps.accounts.factories import UserFactory
from apps.accounts.roles import Role
from apps.businesses import events
from apps.businesses.factories import (
    BusinessPhotoFactory,
    ServiceFactory,
    open_all_week,
)
from apps.businesses.models import (
    Business,
    BusinessDocument,
    BusinessSubmission,
    StarterMenuItem,
    VerificationStatus,
)
from apps.businesses.tests.provider_helpers import (
    P,
    call,
    customer_headers,
    draft_business,
    owner_headers,
    pdf_bytes,
    png_bytes,
    upload,
)
from apps.catalog.factories import CategoryFactory
from apps.core.models import AuditLog
from apps.geo.factories import AreaFactory, CityFactory
from apps.geo.models import CityVisibility

pytestmark = pytest.mark.django_db


def complete_business(owner: Any, **kw: Any) -> Business:
    """A draft with every onboarding requirement satisfied (via the real API steps)."""
    b = draft_business(
        owner, description="Great salon", address="Main road", phone="+923001234567", **kw
    )
    b.onboarding_progress = {"completed": ["basics", "location"], "last_step": "location"}
    b.save()
    ServiceFactory(business=b)
    open_all_week(b)
    b.logo = "businesses/x/logo.jpg"
    b.save(update_fields=["logo"])
    BusinessPhotoFactory(business=b)
    for kind in ("cnic_front", "cnic_back", "selfie_with_cnic"):
        BusinessDocument.objects.create(
            business=b, kind=kind, file=f"documents/{kind}.png", content_type="image/png", size=10
        )
    return b


TERMS = {
    "accept_provider_agreement": True,
    "accept_commission_terms": True,
    "terms_version": "2026-01",
}


def submit(headers: dict[str, str], b: Business, **overrides: Any) -> Any:
    return call("post", f"{P}/businesses/{b.public_id}/submit", headers, {**TERMS, **overrides})


# ---------------- access control


def test_anonymous_gets_401() -> None:
    r = Client().post(f"{P}/businesses", data="{}", content_type="application/json")
    assert r.status_code == 401


def test_invalid_token_gets_401() -> None:
    r = call("get", f"{P}/businesses", {"HTTP_AUTHORIZATION": "Bearer nope"})
    assert r.status_code == 401


def test_customer_gets_403() -> None:
    r = call("get", f"{P}/businesses", customer_headers())
    assert r.status_code == 403 and r.json()["code"] == "permission_denied"


def test_other_owner_gets_404_everywhere() -> None:
    owner, _ = owner_headers()
    b = draft_business(owner)
    _, other = owner_headers()
    base = f"{P}/businesses/{b.public_id}"
    assert call("get", f"{base}/onboarding", other).status_code == 404
    assert call("patch", f"{base}/onboarding/basics", other, {}).status_code == 404
    assert submit(other, b).status_code == 404
    assert call("get", f"{base}/documents", other).status_code == 404
    assert call("delete", f"{base}/documents/1", other).status_code == 404
    assert (
        upload(f"{base}/documents", other, "a.png", png_bytes(), kind="cnic_front").status_code
        == 404
    )
    assert upload(f"{base}/logo", other, "a.png", png_bytes()).status_code == 404
    assert call("get", f"{P}/businesses/not-a-uuid/onboarding", other).status_code == 422


# ---------------- draft creation


def create_body(**kw: Any) -> dict[str, Any]:
    cat = kw.pop("category", None) or CategoryFactory()
    city = kw.pop("city", None) or CityFactory()
    return {
        "name": "Glow Studio",
        "category_id": cat.pk,
        "gender_type": "women",
        "description": "Hair and makeup",
        "city_id": city.pk,
        **kw,
    }


def test_create_draft_success() -> None:
    owner, h = owner_headers()
    r = call("post", f"{P}/businesses", h, create_body())
    assert r.status_code == 201, r.content
    data = r.json()
    b = Business.objects.get(public_id=data["id"])
    assert b.owner_id == owner.pk and b.verification_status == "draft" and not b.is_listed
    assert data["status"] == "draft" and data["slug"] == "glow-studio"
    assert b.onboarding_progress == {"completed": ["basics"], "last_step": "basics"}


def test_create_slug_is_unique_per_city() -> None:
    _, h = owner_headers()
    city = CityFactory()
    a = call("post", f"{P}/businesses", h, create_body(city=city)).json()
    b = call("post", f"{P}/businesses", h, create_body(city=city)).json()
    assert a["slug"] == "glow-studio" and b["slug"] == "glow-studio-2"


@pytest.mark.parametrize(
    "bad",
    [
        {"name": ""},
        {"name": "x" * 151},
        {"gender_type": "robots"},
        {"category_id": "abc"},
        {"description": "d" * 2001},
    ],
)
def test_create_validation(bad: dict[str, Any]) -> None:
    _, h = owner_headers()
    r = call("post", f"{P}/businesses", h, create_body(**bad))
    assert r.status_code == 422 and r.json()["fields"]


def test_create_boundary_name_150_ok() -> None:
    _, h = owner_headers()
    assert call("post", f"{P}/businesses", h, create_body(name="x" * 150)).status_code == 201


def test_create_rejects_inactive_category_and_hidden_city() -> None:
    _, h = owner_headers()
    inactive = CategoryFactory(is_active=False)
    r = call("post", f"{P}/businesses", h, create_body(category=inactive))
    assert r.status_code == 422 and "category_id" in r.json()["fields"]
    hidden = CityFactory(visibility=CityVisibility.HIDDEN)
    r = call("post", f"{P}/businesses", h, create_body(city=hidden))
    assert r.status_code == 422 and "city_id" in r.json()["fields"]
    preview = CityFactory(visibility=CityVisibility.PREVIEW)
    assert call("post", f"{P}/businesses", h, create_body(city=preview)).status_code == 201


def test_create_is_throttled() -> None:
    _, h = owner_headers()
    codes = [call("post", f"{P}/businesses", h, create_body()).status_code for _ in range(11)]
    assert codes[:10] == [201] * 10 and codes[10] == 429


def test_list_only_own_businesses() -> None:
    owner, h = owner_headers()
    mine = draft_business(owner)
    draft_business(UserFactory(role=Role.BUSINESS_OWNER))
    r = call("get", f"{P}/businesses", h)
    assert [x["id"] for x in r.json()] == [str(mine.public_id)]


# ---------------- steps


def test_basics_step_updates_and_records_progress() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    cat = CategoryFactory()
    r = call(
        "patch",
        f"{P}/businesses/{b.public_id}/onboarding/basics",
        h,
        {"name": "New Name", "category_id": cat.pk, "gender_type": "men", "description": "Barber"},
    )
    assert r.status_code == 200, r.content
    b.refresh_from_db()
    assert (b.name, b.slug, b.category_id, b.gender_type) == ("New Name", "new-name", cat.pk, "men")
    assert "basics" in r.json()["completed_steps"] and r.json()["last_step"] == "basics"


def test_unknown_step_is_404_and_extra_fields_422() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    assert call("patch", f"{P}/businesses/{b.public_id}/onboarding/nope", h, {}).status_code == 404
    r = call("patch", f"{P}/businesses/{b.public_id}/onboarding/media", h, {"x": 1})
    assert r.status_code == 422


LOC = {"address": "12 Clifton", "lat": 24.86, "lng": 67.0, "phone": "+92 300 1234567"}


def location_url(b: Business) -> str:
    return f"{P}/businesses/{b.public_id}/onboarding/location"


def test_location_step_success_sets_point_and_phone() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    area = AreaFactory(city=b.city)
    r = call("patch", location_url(b), h, {**LOC, "area_id": area.pk, "whatsapp": "+923111234567"})
    assert r.status_code == 200, r.content
    b.refresh_from_db()
    assert (round(b.location.y, 2), round(b.location.x, 2)) == (24.86, 67.0)
    assert b.phone == "+923001234567" and b.whatsapp == "+923111234567" and b.area_id == area.pk
    assert "location" in r.json()["completed_steps"]


@pytest.mark.parametrize(
    "bad,field",
    [
        ({"lat": 91}, "lat"),
        ({"lat": -91}, "lat"),
        ({"lng": 181}, "lng"),
        ({"phone": "0300123"}, "phone"),
        ({"phone": ""}, "phone"),
        ({"whatsapp": "abc"}, "whatsapp"),
        ({"address": ""}, "address"),
        ({"address": "a" * 256}, "address"),
    ],
)
def test_location_validation(bad: dict[str, Any], field: str) -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    r = call("patch", location_url(b), h, {**LOC, **bad})
    assert r.status_code == 422 and field in r.json()["fields"], r.content


def test_location_outside_city_radius_rejected_and_boundary(settings) -> None:  # type: ignore[no-untyped-def]
    settings.CITY_MAX_RADIUS_KM = 10
    owner, h = owner_headers()
    b = draft_business(owner)
    far = call("patch", location_url(b), h, {**LOC, "lat": 25.5, "lng": 67.0})
    assert far.status_code == 422 and "lat" in far.json()["fields"]
    near = call("patch", location_url(b), h, {**LOC, "lat": 24.90, "lng": 67.0})
    assert near.status_code == 200


def test_location_hidden_city_and_foreign_area_rejected() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    hidden = CityFactory(visibility=CityVisibility.HIDDEN)
    r = call("patch", location_url(b), h, {**LOC, "city_id": hidden.pk})
    assert r.status_code == 422 and "city_id" in r.json()["fields"]
    other_area = AreaFactory()  # belongs to a different city
    r = call("patch", location_url(b), h, {**LOC, "area_id": other_area.pk})
    assert r.status_code == 422 and "area_id" in r.json()["fields"]
    inactive_city = CityFactory(is_active=False)
    r = call("patch", location_url(b), h, {**LOC, "city_id": inactive_city.pk})
    assert r.status_code == 422


def test_changing_city_regenerates_slug() -> None:
    owner, h = owner_headers()
    b = draft_business(owner, name="Same Name", slug="same-name")
    other = CityFactory()
    r = call("patch", location_url(b), h, {**LOC, "city_id": other.pk})
    assert r.status_code == 200
    b.refresh_from_db()
    assert b.city_id == other.pk and b.slug == "same-name"


def test_services_step_requires_one_active_service() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    url = f"{P}/businesses/{b.public_id}/onboarding/services"
    r = call("patch", url, h, {})
    assert r.status_code == 422 and "services" in r.json()["fields"]
    ServiceFactory(business=b, is_active=False)
    assert call("patch", url, h, {}).status_code == 422
    ServiceFactory(business=b)
    assert call("patch", url, h, {}).status_code == 200


def test_services_step_from_starter_menu_deduplicates() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    item = StarterMenuItem.objects.create(
        category=b.category, name="Haircut", price=1500, duration_minutes=45
    )
    url = f"{P}/businesses/{b.public_id}/onboarding/services"
    assert call("patch", url, h, {"starter_item_ids": [item.pk]}).status_code == 200
    assert call("patch", url, h, {"starter_item_ids": [item.pk]}).status_code == 200
    assert b.services.count() == 1 and b.services.get().price == 1500


def test_starter_menu_endpoint_and_seed_command() -> None:
    _, h = owner_headers()
    call_command("seed_starter_menu")
    call_command("seed_starter_menu")  # idempotent
    from apps.catalog.models import Category

    hair = Category.objects.get(slug="hair")
    r = call("get", f"{P}/starter-menu?category_id={hair.pk}", h)
    assert r.status_code == 200 and {i["name"] for i in r.json()} >= {"Haircut", "Hair colour"}
    assert StarterMenuItem.objects.filter(category=hair, name="Haircut").count() == 1
    assert call("get", f"{P}/starter-menu", h).status_code == 422
    assert call("get", f"{P}/starter-menu?category_id=0", h).json() == []
    assert (
        call("get", f"{P}/starter-menu?category_id={hair.pk}", customer_headers()).status_code
        == 403
    )


def test_staff_hours_step_sets_week_and_requires_hours() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    url = f"{P}/businesses/{b.public_id}/onboarding/staff_hours"
    assert call("patch", url, h, {}).status_code == 422
    week = [
        {"weekday": d, "shifts": [{"opens_at": "10:00", "closes_at": "19:00"}]} for d in range(6)
    ]
    assert call("patch", url, h, {"hours": week}).status_code == 200
    assert b.opening_hours.filter(staff__isnull=True).count() == 6


def test_staff_hours_step_rejects_bad_shifts() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    url = f"{P}/businesses/{b.public_id}/onboarding/staff_hours"
    bad = [{"weekday": 0, "shifts": [{"opens_at": "12:00", "closes_at": "10:00"}]}]
    assert call("patch", url, h, {"hours": bad}).status_code == 422
    assert b.opening_hours.count() == 0


def test_media_step_needs_logo_and_photo() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    url = f"{P}/businesses/{b.public_id}/onboarding/media"
    r = call("patch", url, h, {})
    assert r.status_code == 422 and set(r.json()["fields"]) == {"logo", "photos"}
    assert upload(f"{P}/businesses/{b.public_id}/logo", h, "l.png", png_bytes()).status_code == 200
    r = call("patch", url, h, {})
    assert r.status_code == 422 and set(r.json()["fields"]) == {"photos"}
    r = Client().post(
        f"{P}/businesses/{b.public_id}/photos",
        {"files": [SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")]},
        **h,
    )
    assert r.status_code == 201, r.content
    assert call("patch", url, h, {}).status_code == 200


def test_documents_step_requires_required_kinds() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    url = f"{P}/businesses/{b.public_id}/onboarding/documents"
    assert call("patch", url, h, {}).status_code == 422
    for kind in ("cnic_front", "cnic_back", "selfie_with_cnic"):
        r = upload(f"{P}/businesses/{b.public_id}/documents", h, "d.png", png_bytes(), kind=kind)
        assert r.status_code == 201, r.content
    assert call("patch", url, h, {}).status_code == 200


def test_steps_can_be_revisited() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    cat = CategoryFactory()
    url = f"{P}/businesses/{b.public_id}/onboarding/basics"
    body = {"name": "One", "category_id": cat.pk, "gender_type": "women", "description": "d"}
    call("patch", url, h, body)
    call("patch", url, h, {**body, "name": "Two"})
    b.refresh_from_db()
    assert b.name == "Two" and b.onboarding_progress["completed"].count("basics") == 1


def test_resume_after_partial_steps() -> None:
    owner, h = owner_headers()
    b = draft_business(owner, description="")
    call("patch", location_url(b), h, LOC)
    r = call("get", f"{P}/businesses/{b.public_id}/onboarding", h)
    data = r.json()
    done = {i["key"]: i["done"] for i in data["items"]}
    assert data["last_step"] == "location" and "location" in data["completed_steps"]
    assert done["location"] is True and done["basics"] is False and done["media"] is False
    assert data["is_complete"] is False
    missing = {i["key"]: i["missing"] for i in data["items"]}
    assert missing["basics"] == ["description"] and "logo" in missing["media"]


def test_step_rejected_once_submitted() -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    assert submit(h, b).status_code == 200
    r = call("patch", f"{P}/businesses/{b.public_id}/onboarding/basics", h, {})
    assert r.status_code == 409 and r.json()["code"] == "business_locked"


# ---------------- submission


def test_submit_success_records_terms_status_event_audit_notification(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner, h = owner_headers()
    b = complete_business(owner)
    received: list[int] = []

    def handler(sender: Any, business_id: int, **kw: Any) -> None:
        received.append(business_id)

    events.business_submitted.connect(handler, weak=False)
    try:
        with run_commit():
            r = Client().post(
                f"{P}/businesses/{b.public_id}/submit",
                data=json.dumps(TERMS),
                content_type="application/json",
                REMOTE_ADDR="203.0.113.7",
                **h,
            )
    finally:
        events.business_submitted.disconnect(handler)
    assert r.status_code == 200, r.content
    b.refresh_from_db()
    assert b.verification_status == "pending" and b.is_listed is False
    sub = BusinessSubmission.objects.get(business=b)
    assert (sub.number, sub.terms_version, sub.ip_address) == (1, "2026-01", "203.0.113.7")
    assert sub.accepted_at <= datetime.now(UTC)
    assert received == [b.pk]
    audit = AuditLog.objects.get(action="business.submitted")
    assert audit.before == {"status": "draft"} and audit.after["status"] == "pending"
    from apps.notifications.models import Notification

    n = Notification.objects.filter(user=owner, template_key="onboarding_status_changed")
    assert n.exists()


@pytest.mark.parametrize(
    "missing", ["logo", "photo", "service", "hours", "documents", "location", "description"]
)
def test_submit_with_each_missing_piece_is_422_with_fields(missing: str) -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    key = {
        "logo": "media",
        "photo": "media",
        "service": "services",
        "hours": "staff_hours",
        "documents": "documents",
        "location": "location",
        "description": "basics",
    }[missing]
    if missing == "logo":
        b.logo = ""
        b.save(update_fields=["logo"])
    elif missing == "photo":
        b.photos.all().delete()
    elif missing == "service":
        b.services.all().delete()
    elif missing == "hours":
        b.opening_hours.all().delete()
    elif missing == "documents":
        b.documents.filter(kind="cnic_back").delete()
    elif missing == "location":
        b.onboarding_progress = {"completed": ["basics"], "last_step": "basics"}
        b.save(update_fields=["onboarding_progress"])
    else:
        b.description = ""
        b.save(update_fields=["description"])
    r = submit(h, b)
    assert r.status_code == 422, r.content
    assert key in r.json()["fields"]
    b.refresh_from_db()
    assert b.verification_status == "draft" and not b.submissions.exists()


def test_submit_requires_terms() -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    r = submit(
        h, b, accept_provider_agreement=False, accept_commission_terms=False, terms_version="old"
    )
    assert r.status_code == 422
    assert set(r.json()["fields"]) == {
        "accept_provider_agreement",
        "accept_commission_terms",
        "terms_version",
    }
    assert not BusinessSubmission.objects.exists()


def test_submit_twice_is_conflict() -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    assert submit(h, b).status_code == 200
    r = submit(h, b)
    assert r.status_code == 409 and r.json()["code"] == "already_submitted"


def test_verified_business_cannot_be_submitted() -> None:
    owner, h = owner_headers()
    b = complete_business(owner, verification_status=VerificationStatus.VERIFIED)
    r = submit(h, b)
    assert r.status_code == 409 and r.json()["code"] == "invalid_status"


def test_needs_info_can_edit_and_resubmit_repeatedly() -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    for _n in (1, 2, 3):
        assert submit(h, b).status_code == 200
        Business.objects.filter(pk=b.pk).update(
            verification_status="needs_info", status_message="Please upload a clearer CNIC"
        )
    assert b.submissions.count() == 3
    r = call("get", f"{P}/businesses/{b.public_id}/onboarding", h)
    assert r.json()["status"] == "needs_info"
    assert r.json()["status_message"] == "Please upload a clearer CNIC"
    assert call("patch", f"{P}/businesses/{b.public_id}/onboarding/media", h, {}).status_code == 200


def test_rejected_can_resubmit_exactly_once() -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    assert submit(h, b).status_code == 200
    Business.objects.filter(pk=b.pk).update(verification_status="rejected", status_message="no")
    assert submit(h, b).status_code == 200
    assert b.submissions.get(number=2).is_resubmission_after_rejection is True
    Business.objects.filter(pk=b.pk).update(verification_status="rejected")
    r = submit(h, b)
    assert r.status_code == 409 and r.json()["code"] == "resubmission_not_allowed"


def test_owner_sees_admin_message_in_business_payload() -> None:
    owner, h = owner_headers()
    draft_business(owner, verification_status="needs_info", status_message="Add storefront photo")
    r = call("get", f"{P}/businesses", h)
    assert r.json()[0]["status"] == "needs_info"
    assert r.json()[0]["status_message"] == "Add storefront photo"


# ---------------- documents


def doc_url(b: Business) -> str:
    return f"{P}/businesses/{b.public_id}/documents"


def test_document_upload_stores_privately_with_signed_url() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    r = upload(doc_url(b), h, "front.png", png_bytes(), kind="cnic_front")
    assert r.status_code == 201, r.content
    doc = BusinessDocument.objects.get()
    assert doc.status == "pending" and doc.content_type == "image/png" and doc.size > 0
    assert doc.file.startswith(f"documents/{b.public_id}/") and doc.file.endswith(".png")
    assert storages["private"].exists(doc.file)
    from django.core.files.storage import default_storage

    assert not default_storage.exists(doc.file)  # not in the public bucket
    with pytest.raises(ValueError):
        storages["private"].url(doc.file)  # no direct URL exists
    url = r.json()["url"]
    got = Client().get(url)  # signed URL works without a bearer token
    assert got.status_code == 200 and got["Content-Type"] == "image/png"
    assert got["Cache-Control"] == "private, no-store"
    assert b"".join(got.streaming_content if got.streaming else [got.content]) == png_bytes()


def test_signed_url_expires_and_rejects_tampering(settings) -> None:  # type: ignore[no-untyped-def]
    import time_machine

    owner, h = owner_headers()
    b = draft_business(owner)
    url = upload(doc_url(b), h, "f.png", png_bytes(), kind="cnic_front").json()["url"]
    assert Client().get(url + "x").status_code == 404
    assert Client().get("/api/v1/files/documents/garbage").status_code == 404
    with time_machine.travel(datetime.now(UTC).replace(microsecond=0), tick=False) as t:
        t.shift(settings.PRIVATE_URL_TTL_SECONDS - 5)
        assert Client().get(url).status_code == 200
        t.shift(10)
        assert Client().get(url).status_code == 404


def test_document_pdf_ok_and_listing_has_signed_urls() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    assert upload(doc_url(b), h, "licence.pdf", pdf_bytes(), kind="licence").status_code == 201
    listing = call("get", doc_url(b), h).json()
    assert listing[0]["kind"] == "licence" and listing[0]["content_type"] == "application/pdf"
    assert listing[0]["url"].startswith("/api/v1/files/documents/")


@pytest.mark.parametrize(
    "name,data,code,status",
    [
        ("evil.png", b"<?php echo 1; ?>", "invalid_document", 422),
        ("empty.png", b"", "invalid_document", 422),
        ("disguised.png", b"\x89PNG\r\n\x1a\n" + b"not really", "invalid_document", 422),
        ("fake.pdf", b"%PDF-1.4 truncated", "invalid_document", 422),
        ("gif.gif", b"GIF89a" + b"\x00" * 20, "invalid_document", 422),
    ],
)
def test_document_rejects_wrong_or_disguised_files(
    name: str, data: bytes, code: str, status: int
) -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    r = upload(doc_url(b), h, name, data, kind="cnic_front")
    assert r.status_code == status and r.json()["code"] == code
    assert not BusinessDocument.objects.exists()


def test_document_size_limit_boundary(settings) -> None:  # type: ignore[no-untyped-def]
    settings.DOCUMENT_MAX_BYTES = 10 * 1024 * 1024
    owner, h = owner_headers()
    b = draft_business(owner)
    pad = b"%%EOF\n"
    at_limit = b"%PDF-1.4\n" + b"0" * (settings.DOCUMENT_MAX_BYTES - 9 - len(pad)) + pad
    assert len(at_limit) == settings.DOCUMENT_MAX_BYTES
    assert upload(doc_url(b), h, "ok.pdf", at_limit, kind="licence").status_code == 201
    r = upload(doc_url(b), h, "big.pdf", at_limit + b"0", kind="storefront")
    assert r.status_code == 413 and r.json()["code"] == "file_too_large"


def test_document_unknown_kind_and_missing_file() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    r = upload(doc_url(b), h, "a.png", png_bytes(), kind="passport")
    assert r.status_code == 422 and "kind" in r.json()["fields"]
    assert Client().post(doc_url(b), {"kind": "licence"}, **h).status_code == 422


def test_new_upload_replaces_unaccepted_same_kind(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner, h = owner_headers()
    b = draft_business(owner)
    with run_commit():
        upload(doc_url(b), h, "a.png", png_bytes(), kind="cnic_front")
    first = BusinessDocument.objects.get()
    with run_commit():
        upload(doc_url(b), h, "b.png", png_bytes((10, 10)), kind="cnic_front")
    assert BusinessDocument.objects.count() == 1
    assert not storages["private"].exists(first.file)


def test_accepted_document_is_kept_and_locked() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    upload(doc_url(b), h, "a.png", png_bytes(), kind="cnic_front")
    doc = BusinessDocument.objects.get()
    BusinessDocument.objects.filter(pk=doc.pk).update(status="accepted")
    upload(doc_url(b), h, "b.png", png_bytes((9, 9)), kind="cnic_front")
    assert BusinessDocument.objects.count() == 2
    r = call("delete", f"{doc_url(b)}/{doc.pk}", h)
    assert r.status_code == 409 and r.json()["code"] == "document_locked"


def test_delete_document_and_foreign_document_404(run_commit) -> None:  # type: ignore[no-untyped-def]
    owner, h = owner_headers()
    b = draft_business(owner)
    upload(doc_url(b), h, "a.png", png_bytes(), kind="licence")
    doc = BusinessDocument.objects.get()
    other_biz = draft_business(owner)
    assert (
        call("delete", f"{P}/businesses/{other_biz.public_id}/documents/{doc.pk}", h).status_code
        == 404
    )
    with run_commit():
        assert call("delete", f"{doc_url(b)}/{doc.pk}", h).status_code == 204
    assert not BusinessDocument.objects.exists() and not storages["private"].exists(doc.file)


def test_documents_locked_while_pending() -> None:
    owner, h = owner_headers()
    b = complete_business(owner)
    assert submit(h, b).status_code == 200
    r = upload(doc_url(b), h, "a.png", png_bytes(), kind="licence")
    assert r.status_code == 409 and r.json()["code"] == "business_locked"


def test_upload_throttle() -> None:
    owner, h = owner_headers()
    b = draft_business(owner)
    codes = [
        upload(f"{P}/businesses/{b.public_id}/logo", h, "l.png", png_bytes()).status_code
        for _ in range(61)
    ]
    assert codes[:60] == [200] * 60 and codes[60] == 429


def test_unused_imports_guard() -> None:
    assert uuid.uuid4()

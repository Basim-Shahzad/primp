"""Security pass over the provider API (Plan 6.2, Step 9).

Every provider endpoint must be classified here. Object-scoped ones are called as a *different*
owner and must answer 404 (no existence leak) without changing anything.
"""

import json
from datetime import timedelta
from typing import Any

import pytest
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client
from django.utils import timezone

from apps.accounts.factories import make_customer
from apps.bookings.factories import AppointmentFactory
from apps.bookings.models import Appointment, AppointmentService, RescheduleRequest
from apps.businesses.factories import (
    BusinessFactory,
    BusinessPhotoFactory,
    ClosureFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.businesses.models import (
    Business,
    BusinessDocument,
    BusinessPhoto,
    Closure,
    Service,
    Staff,
)
from apps.businesses.tests.provider_helpers import customer_headers, owner_headers, png_bytes
from config.api import api

pytestmark = pytest.mark.django_db
PREFIX = "/api/v1"

TERMS = {"accept_provider_agreement": True, "accept_commission_terms": True, "terms_version": "x"}
START = "2030-01-02T12:00:00+05:00"
RANGE = "from=2030-01-01&to=2030-01-31"

# (method, path template) -> (query string, json body | None)
OBJECT_SCOPED: dict[tuple[str, str], tuple[str, Any]] = {
    ("get", "/provider/businesses/{business_id}"): ("", None),
    ("patch", "/provider/businesses/{business_id}"): ("", {"description": "x"}),
    ("get", "/provider/businesses/{business_id}/preview"): ("", None),
    ("get", "/provider/businesses/{business_id}/onboarding"): ("", None),
    ("patch", "/provider/businesses/{business_id}/onboarding/{step}"): ("", {}),
    ("post", "/provider/businesses/{business_id}/submit"): ("", TERMS),
    ("get", "/provider/businesses/{business_id}/documents"): ("", None),
    ("delete", "/provider/businesses/{business_id}/documents/{document_id}"): ("", None),
    ("get", "/provider/businesses/{business_id}/photos"): ("", None),
    ("patch", "/provider/businesses/{business_id}/photos/order"): ("", {"photo_ids": [1]}),
    ("patch", "/provider/businesses/{business_id}/photos/{photo_id}/cover"): ("", None),
    ("delete", "/provider/businesses/{business_id}/photos/{photo_id}"): ("", None),
    ("get", "/provider/businesses/{business_id}/services"): ("", None),
    (
        "post",
        "/provider/businesses/{business_id}/services",
    ): ("", {"name": "x", "price": 1, "duration_minutes": 5}),
    ("patch", "/provider/businesses/{business_id}/services/order"): ("", {"service_ids": [1]}),
    ("patch", "/provider/services/{service_id}"): ("", {"price": 5}),
    ("delete", "/provider/services/{service_id}"): ("", None),
    ("get", "/provider/businesses/{business_id}/hours"): ("", None),
    ("put", "/provider/businesses/{business_id}/hours"): ("", {"days": []}),
    ("get", "/provider/businesses/{business_id}/closures"): ("", None),
    (
        "post",
        "/provider/businesses/{business_id}/closures",
    ): ("", {"starts_on": "2030-01-01", "ends_on": "2030-01-02"}),
    ("delete", "/provider/closures/{closure_id}"): ("", None),
    ("get", "/provider/businesses/{business_id}/blocks"): ("", None),
    (
        "post",
        "/provider/businesses/{business_id}/blocks",
    ): ("", {"starts_at": START, "ends_at": "2030-01-02T14:00:00+05:00"}),
    ("delete", "/provider/blocks/{block_id}"): ("", None),
    ("get", "/provider/businesses/{business_id}/booking-settings"): ("", None),
    ("patch", "/provider/businesses/{business_id}/booking-settings"): ("", {"buffer_minutes": 5}),
    ("get", "/provider/businesses/{business_id}/staff"): ("", None),
    ("post", "/provider/businesses/{business_id}/staff"): ("", {"name": "New"}),
    ("patch", "/provider/staff/{staff_id}"): ("", {"name": "X"}),
    ("get", "/provider/staff/{staff_id}/hours"): ("", None),
    ("put", "/provider/staff/{staff_id}/hours"): ("", {"days": []}),
    ("get", "/provider/staff/{staff_id}/impact"): ("action=deactivate", None),
    ("post", "/provider/staff/{staff_id}/deactivate"): ("", {"resolutions": []}),
    ("put", "/provider/staff/{staff_id}/services"): ("", {"service_ids": []}),
    (
        "post",
        "/provider/businesses/{business_id}/manual-bookings",
    ): (
        "",
        {"customer_name": "A", "customer_phone": "03001234567", "service_ids": [1], "start": START},
    ),
    ("get", "/provider/businesses/{business_id}/conflicts"): ("", None),
    ("get", "/provider/businesses/{business_id}/schedule"): (RANGE, None),
    ("get", "/provider/businesses/{business_id}/appointments"): ("", None),
    ("get", "/provider/businesses/{business_id}/appointments/today"): ("", None),
    ("get", "/provider/businesses/{business_id}/appointments/needs-action"): ("", None),
    (
        "get",
        "/provider/businesses/{business_id}/appointments/changes",
    ): ("since=2030-01-01T00:00:00Z", None),
    ("get", "/provider/businesses/{business_id}/appointments/export.csv"): (RANGE, None),
    ("get", "/provider/businesses/{business_id}/analytics/summary"): ("", None),
    ("get", "/provider/businesses/{business_id}/analytics/top-services"): ("", None),
    ("get", "/provider/businesses/{business_id}/analytics/timeseries"): ("", None),
    ("get", "/provider/appointments/{appointment_id}"): ("", None),
    ("post", "/provider/appointments/{appointment_id}/accept"): ("", {}),
    ("post", "/provider/appointments/{appointment_id}/decline"): ("", {}),
    ("post", "/provider/appointments/{appointment_id}/cancel"): ("", {"reason": "x"}),
    ("post", "/provider/appointments/{appointment_id}/reschedule"): ("", {"start": START}),
    ("post", "/provider/appointments/{appointment_id}/complete"): ("", {}),
    ("post", "/provider/appointments/{appointment_id}/no-show"): ("", {}),
    ("put", "/provider/appointments/{appointment_id}/note"): ("", {"note": "x"}),
    ("post", "/provider/appointments/{appointment_id}/reschedule-request/accept"): ("", {}),
    ("post", "/provider/appointments/{appointment_id}/reschedule-request/decline"): ("", {}),
}
# uploads need multipart bodies: (method, template) -> form fields
UPLOADS: dict[tuple[str, str], dict[str, str]] = {
    ("post", "/provider/businesses/{business_id}/documents"): {"kind": "licence"},
    ("post", "/provider/businesses/{business_id}/logo"): {},
    ("post", "/provider/businesses/{business_id}/photos"): {},
    ("post", "/provider/staff/{staff_id}/photo"): {},
}
# not object-scoped: they act on the caller's own data only, or are public by signed token
OWN_DATA_ONLY = {
    ("post", "/provider/businesses"),
    ("get", "/provider/businesses"),
    ("get", "/provider/starter-menu"),
    ("get", "/provider/notification-preferences"),
    ("patch", "/provider/notification-preferences"),
    ("get", "/provider/notifications"),
    ("post", "/provider/notifications/read"),
}
SIGNED_TOKEN = {("get", "/files/documents/{token}")}


def documented_operations() -> set[tuple[str, str]]:
    schema = api.get_openapi_schema()
    ops: set[tuple[str, str]] = set()
    for path, item in schema["paths"].items():
        short = path.removeprefix(PREFIX)
        if short.startswith(("/provider", "/files")):
            for method in item:
                ops.add((method, short))
    return ops


def normalise(template: str) -> str:
    return template


def test_every_provider_endpoint_is_classified() -> None:
    classified = set(OBJECT_SCOPED) | set(UPLOADS) | OWN_DATA_ONLY | SIGNED_TOKEN
    documented = documented_operations()
    assert documented - classified == set(), "classify new endpoints in test_provider_security.py"
    assert classified - documented == set(), "stale entries in test_provider_security.py"
    assert len(documented) >= 60  # sanity: the schema really lists the provider API


class World:
    def __init__(self) -> None:
        self.owner, self.headers = owner_headers()
        self.business: Business = BusinessFactory(owner=self.owner, staff_level_booking=True)
        open_all_week(self.business)
        self.service: Service = ServiceFactory(business=self.business)
        self.staff: Staff = StaffFactory(business=self.business)
        StaffServiceFactory(staff=self.staff, service=self.service)
        self.photo: BusinessPhoto = BusinessPhotoFactory(business=self.business, is_cover=True)
        self.closure: Closure = ClosureFactory(business=self.business)
        self.block: Closure = ClosureFactory(business=self.business, staff=self.staff)
        self.document = BusinessDocument.objects.create(
            business=self.business,
            kind="cnic_front",
            file="documents/x.png",
            content_type="image/png",
            size=1,
        )
        start = timezone.now() + timedelta(days=3)
        self.appointment: Appointment = AppointmentFactory(
            business=self.business,
            staff=self.staff,
            customer=make_customer(),
            status="pending",
            scheduled_start=start,
            scheduled_end=start + timedelta(hours=1),
        )
        AppointmentService.objects.create(
            appointment=self.appointment,
            service=self.service,
            name="x",
            price_at_booking=1,
            duration_at_booking=60,
        )
        RescheduleRequest.objects.create(
            appointment=self.appointment,
            new_start=start + timedelta(hours=3),
            new_end=start + timedelta(hours=4),
        )

    def fill(self, template: str) -> str:
        return template.format(
            business_id=self.business.public_id,
            service_id=self.service.pk,
            staff_id=self.staff.pk,
            photo_id=self.photo.pk,
            closure_id=self.closure.pk,
            block_id=self.block.pk,
            document_id=self.document.pk,
            appointment_id=self.appointment.public_id,
            step="basics",
        )

    def snapshot(self) -> dict[str, Any]:
        return {
            "business": Business.objects.filter(pk=self.business.pk).values().get(),
            "services": list(
                Service.objects.filter(business=self.business).values_list("pk", "price")
            ),
            "staff": list(
                Staff.objects.filter(business=self.business).values_list("pk", "name", "is_active")
            ),
            "photos": BusinessPhoto.objects.filter(business=self.business).count(),
            "closures": Closure.objects.filter(business=self.business).count(),
            "documents": BusinessDocument.objects.filter(business=self.business).count(),
            "appointments": list(
                Appointment.objects.filter(business=self.business).values_list(
                    "pk", "status", "scheduled_start", "staff_id", "business_note"
                )
            ),
            "requests": list(
                RescheduleRequest.objects.filter(appointment=self.appointment).values_list(
                    "pk", "status"
                )
            ),
        }


def request(method: str, url: str, headers: dict[str, str], body: Any = None) -> Any:
    client = Client()
    if body is None:
        return getattr(client, method)(url, **headers)
    return getattr(client, method)(
        url, data=json.dumps(body), content_type="application/json", **headers
    )


@pytest.mark.parametrize(("method", "template"), sorted(OBJECT_SCOPED))
def test_other_owner_gets_404_and_nothing_changes(method: str, template: str) -> None:
    world = World()
    _, intruder = owner_headers()
    before = world.snapshot()
    query, body = OBJECT_SCOPED[(method, template)]
    url = f"{PREFIX}{world.fill(template)}" + (f"?{query}" if query else "")
    response = request(method, url, intruder, body)
    assert response.status_code == 404, (
        method,
        template,
        response.status_code,
        response.content[:200],
    )
    assert world.snapshot() == before


@pytest.mark.parametrize(("method", "template"), sorted(UPLOADS))
def test_other_owner_cannot_upload(method: str, template: str) -> None:
    world = World()
    _, intruder = owner_headers()
    before = world.snapshot()
    url = f"{PREFIX}{world.fill(template)}"
    form = {**UPLOADS[(method, template)]}
    if template.endswith("/photos"):
        form["files"] = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")  # type: ignore[assignment]
    else:
        form["file"] = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")  # type: ignore[assignment]
    response = Client().post(url, form, **intruder)
    assert response.status_code == 404, (template, response.status_code, response.content[:200])
    assert world.snapshot() == before


def send(world: World, method: str, template: str, headers: dict[str, str]) -> Any:
    """Send the request for any classified endpoint (multipart for uploads)."""
    if (method, template) in UPLOADS:
        form: dict[str, Any] = dict(UPLOADS[(method, template)])
        field = "files" if template.endswith("/photos") else "file"
        form[field] = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")
        return Client().post(f"{PREFIX}{world.fill(template)}", form, **headers)
    query, body = OBJECT_SCOPED[(method, template)]
    url = f"{PREFIX}{world.fill(template)}" + (f"?{query}" if query else "")
    return request(method, url, headers, body)


@pytest.mark.parametrize(("method", "template"), sorted(set(OBJECT_SCOPED) | set(UPLOADS)))
def test_customers_and_anonymous_are_refused(method: str, template: str) -> None:
    world = World()
    assert send(world, method, template, {}).status_code == 401
    assert send(world, method, template, customer_headers()).status_code == 403
    assert send(world, method, template, {"HTTP_AUTHORIZATION": "Bearer broken"}).status_code == 401


def test_owner_can_still_reach_their_own_objects() -> None:
    """Guards the guard: the same requests succeed (not 404) for the real owner."""
    world = World()
    for method, template in (
        ("get", "/provider/businesses/{business_id}"),
        ("get", "/provider/businesses/{business_id}/staff"),
        ("get", "/provider/appointments/{appointment_id}"),
        ("get", "/provider/businesses/{business_id}/conflicts"),
    ):
        response = request(method, f"{PREFIX}{world.fill(template)}", world.headers)
        assert response.status_code == 200, (template, response.status_code)


def test_signed_document_links_are_the_only_unauthenticated_provider_route() -> None:
    assert Client().get(f"{PREFIX}/files/documents/not-a-token").status_code == 404
    unauthenticated = [(m, t) for (m, t) in documented_operations() if t.startswith("/provider")]
    world = World()
    for method, template in unauthenticated:
        url = f"{PREFIX}{world.fill(template.replace('{token}', 'x'))}"
        assert request(method, url, {}, {} if method != "get" else None).status_code == 401, (
            template
        )


# ------------------------------------------------------------ upload throttles


def test_uploads_share_one_hourly_throttle() -> None:
    world = World()
    codes = []
    for i in range(61):
        target = (
            f"{PREFIX}/provider/staff/{world.staff.pk}/photo"
            if i % 2
            else f"{PREFIX}/provider/businesses/{world.business.public_id}/logo"
        )
        file = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")
        codes.append(Client().post(target, {"file": file}, **world.headers).status_code)
    assert codes[:60] == [200] * 60 and codes[60] == 429
    file = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")
    photos = Client().post(
        f"{PREFIX}/provider/businesses/{world.business.public_id}/photos",
        {"files": [file]},
        **world.headers,
    )
    assert photos.status_code == 429
    doc = SimpleUploadedFile("p.png", png_bytes(), content_type="image/png")
    docs = Client().post(
        f"{PREFIX}/provider/businesses/{world.business.public_id}/documents",
        {"kind": "licence", "file": doc},
        **world.headers,
    )
    assert docs.status_code == 429

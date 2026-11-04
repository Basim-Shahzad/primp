"""Provider (business owner) endpoints: onboarding, documents and media. Tagged `provider`."""

import uuid
from datetime import datetime
from typing import Any

from django.core.files.storage import default_storage
from django.http import HttpRequest, HttpResponse
from ninja import Body, File, Form, Router, Status, UploadedFile
from pydantic import BaseModel

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.core import ratelimit
from apps.core.errors import not_found, validation_error
from apps.core.http import get_client_ip
from apps.core.permissions import require_role

from . import documents, hours, media, menu, onboarding, profile, selectors
from . import page as page_service
from . import staff as staff_mod
from .models import Business, Closure, Service, Staff
from .provider_schemas import (
    BlockIn,
    BlockOut,
    BookingSettingsIn,
    BookingSettingsOut,
    BusinessCreateIn,
    ChecklistItemOut,
    ClosureIn,
    ClosureOut,
    DaySchema,
    DocumentOut,
    HoursIn,
    HoursOut,
    OnboardingOut,
    OwnerBusinessOut,
    PhotoOrderIn,
    PhotoOutOwner,
    ProfilePatchIn,
    ServiceDeleteOut,
    ServiceIn,
    ServiceOrderIn,
    ServiceOut,
    ServicePatchIn,
    ShiftSchema,
    StaffIn,
    StaffOut,
    StaffPatchIn,
    StarterItemOut,
    SubmitIn,
)
from .schemas import BusinessPageOut

provider_router = Router(tags=["provider"], auth=jwt_auth)
files_router = Router(tags=["provider"])
UPLOAD_LIMIT = (60, 3600)  # uploads per owner per hour

owner_only = require_role(Role.BUSINESS_OWNER)


def _throttle_upload(user_id: int) -> None:
    ratelimit.enforce(f"upload:{user_id}", limit=UPLOAD_LIMIT[0], window_seconds=UPLOAD_LIMIT[1])


def checklist_out(business: Business) -> OnboardingOut:
    c = onboarding.checklist(business)
    return OnboardingOut(
        status=business.verification_status,
        status_message=business.status_message,
        completed_steps=c.completed_steps,
        last_step=c.last_step,
        is_complete=c.is_complete,
        items=[
            ChecklistItemOut(key=i.key, required=i.required, done=i.done, missing=i.missing)
            for i in c.items
        ],
    )


def photo_out(p: Any) -> PhotoOutOwner:
    return PhotoOutOwner(
        id=p.pk, is_cover=p.is_cover, sort_order=p.sort_order, **media.photo_urls(p.image)
    )


@provider_router.post("/businesses", response={201: OwnerBusinessOut})
def create_business(request: HttpRequest, payload: BusinessCreateIn) -> Status[Any]:
    user = owner_only(request)
    business = onboarding.create_draft(
        user.pk,
        name=payload.name,
        category_id=payload.category_id,
        gender_type=payload.gender_type,
        description=payload.description,
        city_id=payload.city_id,
    )
    return Status(201, OwnerBusinessOut.from_business(business))


@provider_router.get("/businesses", response=list[OwnerBusinessOut])
def list_businesses(request: HttpRequest) -> list[OwnerBusinessOut]:
    user = owner_only(request)
    qs = Business.objects.filter(owner_id=user.pk).order_by("id")
    return [OwnerBusinessOut.from_business(b) for b in qs]


@provider_router.get("/businesses/{business_id}/onboarding", response=OnboardingOut)
def get_onboarding(request: HttpRequest, business_id: uuid.UUID) -> OnboardingOut:
    user = owner_only(request)
    return checklist_out(selectors.owned_business(user.pk, business_id))


@provider_router.patch("/businesses/{business_id}/onboarding/{step}", response=OnboardingOut)
def patch_onboarding(
    request: HttpRequest,
    business_id: uuid.UUID,
    step: str,
    payload: Body[dict[str, Any] | None] = None,
) -> OnboardingOut:
    user = owner_only(request)
    business = onboarding.apply_step(user.pk, str(business_id), step, payload or {})
    return checklist_out(business)


@provider_router.post("/businesses/{business_id}/submit", response=OwnerBusinessOut)
def submit_business(
    request: HttpRequest, business_id: uuid.UUID, payload: SubmitIn
) -> OwnerBusinessOut:
    user = owner_only(request)
    business = onboarding.submit(
        user.pk,
        str(business_id),
        accept_provider_agreement=payload.accept_provider_agreement,
        accept_commission_terms=payload.accept_commission_terms,
        terms_version=payload.terms_version,
        ip=get_client_ip(request),
    )
    return OwnerBusinessOut.from_business(business)


@provider_router.get("/starter-menu", response=list[StarterItemOut])
def starter_menu(request: HttpRequest, category_id: int) -> list[StarterItemOut]:
    owner_only(request)
    return [
        StarterItemOut(
            id=i.pk,
            category_id=i.category_id,
            name=i.name,
            description=i.description,
            price=i.price,
            duration_minutes=i.duration_minutes,
        )
        for i in selectors.starter_menu(category_id)
    ]


# ---- documents (private) ----


@provider_router.get("/businesses/{business_id}/documents", response=list[DocumentOut])
def list_documents(request: HttpRequest, business_id: uuid.UUID) -> list[DocumentOut]:
    user = owner_only(request)
    return [
        DocumentOut.from_document(d) for d in documents.list_documents(user.pk, str(business_id))
    ]


@provider_router.post("/businesses/{business_id}/documents", response={201: DocumentOut})
def upload_document(
    request: HttpRequest,
    business_id: uuid.UUID,
    kind: Form[str],
    file: File[UploadedFile],
) -> Status[Any]:
    user = owner_only(request)
    _throttle_upload(user.pk)
    document = documents.upload(user.pk, str(business_id), kind, file.read())
    return Status(201, DocumentOut.from_document(document))


@provider_router.delete("/businesses/{business_id}/documents/{document_id}", response={204: None})
def delete_document(request: HttpRequest, business_id: uuid.UUID, document_id: int) -> Status[Any]:
    user = owner_only(request)
    documents.delete_document(user.pk, str(business_id), document_id)
    return Status(204, None)


@files_router.get("/documents/{token}", auth=None, response={200: Any})
def download_document(request: HttpRequest, token: str) -> HttpResponse:
    found = documents.download(token)
    if found is None:
        raise not_found()
    document, content = found
    response = HttpResponse(content, content_type=document.content_type)
    response["Content-Disposition"] = f'attachment; filename="{document.kind}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


# ---- media ----


@provider_router.post("/businesses/{business_id}/logo", response=OwnerBusinessOut)
def upload_logo(
    request: HttpRequest, business_id: uuid.UUID, file: File[UploadedFile]
) -> OwnerBusinessOut:
    user = owner_only(request)
    _throttle_upload(user.pk)
    return OwnerBusinessOut.from_business(media.set_logo(user.pk, str(business_id), file.read()))


@provider_router.post("/businesses/{business_id}/photos", response={201: list[PhotoOutOwner]})
def upload_photos(
    request: HttpRequest, business_id: uuid.UUID, files: File[list[UploadedFile]]
) -> Status[Any]:
    user = owner_only(request)
    _throttle_upload(user.pk)
    photos = media.add_photos(user.pk, str(business_id), [f.read() for f in files])
    return Status(201, [photo_out(p) for p in photos])


def partial(payload: BaseModel, nullable: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Only the fields the client sent; an explicit null is an error unless the field allows it."""
    changes = payload.model_dump(exclude_unset=True)
    bad = [k for k, v in changes.items() if v is None and k not in nullable]
    if bad:
        raise validation_error({k: ["This field may not be null."] for k in bad})
    return changes


# ---- profile ----


@provider_router.get("/businesses/{business_id}", response=OwnerBusinessOut)
def get_business(request: HttpRequest, business_id: uuid.UUID) -> OwnerBusinessOut:
    user = owner_only(request)
    return OwnerBusinessOut.from_business(selectors.owned_business(user.pk, business_id))


@provider_router.patch("/businesses/{business_id}", response=OwnerBusinessOut)
def patch_business(
    request: HttpRequest, business_id: uuid.UUID, payload: ProfilePatchIn
) -> OwnerBusinessOut:
    user = owner_only(request)
    changes = partial(payload, nullable=frozenset({"area_id"}))
    business = profile.update_profile(user.pk, str(business_id), changes)
    return OwnerBusinessOut.from_business(business)


@provider_router.get("/businesses/{business_id}/preview", response=BusinessPageOut)
def preview_business(request: HttpRequest, business_id: uuid.UUID) -> dict[str, Any]:
    user = owner_only(request)
    return page_service.render_page(selectors.owned_business(user.pk, business_id))


# ---- photos ----


@provider_router.get("/businesses/{business_id}/photos", response=list[PhotoOutOwner])
def list_photos(request: HttpRequest, business_id: uuid.UUID) -> list[PhotoOutOwner]:
    user = owner_only(request)
    business = selectors.owned_business(user.pk, business_id)
    return [photo_out(p) for p in business.photos.all()]


@provider_router.patch("/businesses/{business_id}/photos/order", response=list[PhotoOutOwner])
def order_photos(
    request: HttpRequest, business_id: uuid.UUID, payload: PhotoOrderIn
) -> list[PhotoOutOwner]:
    user = owner_only(request)
    return [photo_out(p) for p in media.reorder(user.pk, str(business_id), payload.photo_ids)]


@provider_router.patch("/businesses/{business_id}/photos/{photo_id}/cover", response=PhotoOutOwner)
def cover_photo(request: HttpRequest, business_id: uuid.UUID, photo_id: int) -> PhotoOutOwner:
    user = owner_only(request)
    return photo_out(media.make_cover(user.pk, str(business_id), photo_id))


@provider_router.delete("/businesses/{business_id}/photos/{photo_id}", response={204: None})
def remove_photo(request: HttpRequest, business_id: uuid.UUID, photo_id: int) -> Status[Any]:
    user = owner_only(request)
    media.delete_photo(user.pk, str(business_id), photo_id)
    return Status(204, None)


# ---- services ----


def service_out(s: Service) -> ServiceOut:
    return ServiceOut(
        id=s.pk,
        name=s.name,
        description=s.description,
        category_id=s.category_id,
        price=s.price,
        is_from_price=s.is_from_price,
        duration_minutes=s.duration_minutes,
        gender=s.gender,
        is_active=s.is_active,
        sort_order=s.sort_order,
    )


@provider_router.get("/businesses/{business_id}/services", response=list[ServiceOut])
def list_services(request: HttpRequest, business_id: uuid.UUID) -> list[ServiceOut]:
    user = owner_only(request)
    return [service_out(s) for s in menu.list_services(user.pk, str(business_id))]


@provider_router.post("/businesses/{business_id}/services", response={201: ServiceOut})
def create_service(
    request: HttpRequest, business_id: uuid.UUID, payload: ServiceIn
) -> Status[ServiceOut]:
    user = owner_only(request)
    service = menu.create_service(user.pk, str(business_id), payload.model_dump())
    return Status(201, service_out(service))


@provider_router.patch("/businesses/{business_id}/services/order", response=list[ServiceOut])
def order_services(
    request: HttpRequest, business_id: uuid.UUID, payload: ServiceOrderIn
) -> list[ServiceOut]:
    user = owner_only(request)
    return [service_out(s) for s in menu.reorder(user.pk, str(business_id), payload.service_ids)]


@provider_router.patch("/services/{service_id}", response=ServiceOut)
def patch_service(request: HttpRequest, service_id: int, payload: ServicePatchIn) -> ServiceOut:
    user = owner_only(request)
    changes = partial(payload, nullable=frozenset({"category_id"}))
    return service_out(menu.update_service(user.pk, service_id, changes))


@provider_router.delete("/services/{service_id}", response=ServiceDeleteOut)
def remove_service(request: HttpRequest, service_id: int) -> ServiceDeleteOut:
    user = owner_only(request)
    return ServiceDeleteOut(deactivated=menu.delete_service(user.pk, service_id))


# ---- weekly hours ----


def week_out(week: list[hours.DayShifts]) -> HoursOut:
    return HoursOut(
        days=[
            DaySchema(
                weekday=d.weekday,
                shifts=[ShiftSchema(opens_at=o, closes_at=c) for o, c in d.shifts],
            )
            for d in week
        ]
    )


def hours_out(business_id: int) -> HoursOut:
    return week_out(hours.week_of(business_id, None))


@provider_router.get("/businesses/{business_id}/hours", response=HoursOut)
def get_hours(request: HttpRequest, business_id: uuid.UUID) -> HoursOut:
    user = owner_only(request)
    return hours_out(selectors.owned_business(user.pk, business_id).pk)


@provider_router.put("/businesses/{business_id}/hours", response=HoursOut)
def put_hours(request: HttpRequest, business_id: uuid.UUID, payload: HoursIn) -> HoursOut:
    user = owner_only(request)
    business = selectors.owned_business(user.pk, business_id)
    week = [
        hours.DayShifts(d.weekday, [(s.opens_at, s.closes_at) for s in d.shifts])
        for d in payload.days
    ]
    hours.replace_week(business.pk, None, week)
    return hours_out(business.pk)


# ---- closures ----


def closure_out(c: Closure) -> ClosureOut:
    return ClosureOut(
        id=c.pk, kind=c.kind, starts_at=c.starts_at, ends_at=c.ends_at, message=c.reason
    )


@provider_router.get("/businesses/{business_id}/closures", response=list[ClosureOut])
def list_closures(request: HttpRequest, business_id: uuid.UUID) -> list[ClosureOut]:
    user = owner_only(request)
    return [closure_out(c) for c in profile.list_closures(user.pk, str(business_id))]


@provider_router.post("/businesses/{business_id}/closures", response={201: ClosureOut})
def create_closure(
    request: HttpRequest, business_id: uuid.UUID, payload: ClosureIn
) -> Status[ClosureOut]:
    user = owner_only(request)
    closure = profile.create_closure(
        user.pk,
        str(business_id),
        starts_on=payload.starts_on,
        ends_on=payload.ends_on,
        message=payload.message,
    )
    return Status(201, closure_out(closure))


@provider_router.delete("/closures/{closure_id}", response={204: None})
def remove_closure(request: HttpRequest, closure_id: int) -> Status[Any]:
    user = owner_only(request)
    profile.delete_closure(user.pk, closure_id)
    return Status(204, None)


# ---- booking settings ----


def settings_out(b: Business) -> BookingSettingsOut:
    return BookingSettingsOut(
        confirm_mode=b.confirm_mode,
        cancellation_notice_hours=b.cancellation_notice_hours,
        lead_time_minutes=b.lead_time_minutes,
        max_advance_days=b.max_advance_days,
        slot_interval_minutes=b.slot_interval_minutes,
        buffer_minutes=b.buffer_minutes,
        staff_level_booking=b.staff_level_booking,
        parallel_capacity=b.parallel_capacity,
    )


@provider_router.get("/businesses/{business_id}/booking-settings", response=BookingSettingsOut)
def get_booking_settings(request: HttpRequest, business_id: uuid.UUID) -> BookingSettingsOut:
    user = owner_only(request)
    return settings_out(selectors.owned_business(user.pk, business_id))


@provider_router.patch("/businesses/{business_id}/booking-settings", response=BookingSettingsOut)
def patch_booking_settings(
    request: HttpRequest, business_id: uuid.UUID, payload: BookingSettingsIn
) -> BookingSettingsOut:
    user = owner_only(request)
    changes = partial(payload)
    business = profile.update_booking_settings(user.pk, str(business_id), changes)
    return settings_out(business)


# ---- staff ----


def staff_outs(rows: list[Staff]) -> list[StaffOut]:
    ids = [s.pk for s in rows]
    links = staff_mod.service_ids_by_staff(ids)
    overrides = staff_mod.staff_with_override(ids)
    return [
        StaffOut(
            id=s.pk,
            name=s.name,
            bio=s.bio,
            photo_url=str(default_storage.url(s.photo)) if s.photo else None,
            is_active=s.is_active,
            service_ids=links.get(s.pk, []),
            has_hours_override=s.pk in overrides,
        )
        for s in rows
    ]


@provider_router.get("/businesses/{business_id}/staff", response=list[StaffOut])
def list_staff(request: HttpRequest, business_id: uuid.UUID) -> list[StaffOut]:
    user = owner_only(request)
    return staff_outs(staff_mod.list_staff(user.pk, str(business_id)))


@provider_router.post("/businesses/{business_id}/staff", response={201: StaffOut})
def create_staff(
    request: HttpRequest, business_id: uuid.UUID, payload: StaffIn
) -> Status[StaffOut]:
    user = owner_only(request)
    staff = staff_mod.create_staff(
        user.pk,
        str(business_id),
        name=payload.name,
        bio=payload.bio,
        service_ids=payload.service_ids,
    )
    return Status(201, staff_outs([staff])[0])


@provider_router.patch("/staff/{staff_id}", response=StaffOut)
def patch_staff(request: HttpRequest, staff_id: int, payload: StaffPatchIn) -> StaffOut:
    user = owner_only(request)
    changes = partial(payload)
    return staff_outs([staff_mod.update_staff(user.pk, staff_id, changes)])[0]


@provider_router.post("/staff/{staff_id}/photo", response=StaffOut)
def upload_staff_photo(request: HttpRequest, staff_id: int, file: File[UploadedFile]) -> StaffOut:
    user = owner_only(request)
    _throttle_upload(user.pk)
    return staff_outs([staff_mod.set_photo(user.pk, staff_id, file.read())])[0]


@provider_router.get("/staff/{staff_id}/hours", response=HoursOut)
def get_staff_hours(request: HttpRequest, staff_id: int) -> HoursOut:
    user = owner_only(request)
    return week_out(staff_mod.get_hours(user.pk, staff_id))


@provider_router.put("/staff/{staff_id}/hours", response=HoursOut)
def put_staff_hours(request: HttpRequest, staff_id: int, payload: HoursIn) -> HoursOut:
    user = owner_only(request)
    week = [
        hours.DayShifts(d.weekday, [(s.opens_at, s.closes_at) for s in d.shifts])
        for d in payload.days
    ]
    staff = staff_mod.set_hours(user.pk, staff_id, week)
    return week_out(hours.week_of(staff.business_id, staff.pk))


# ---- blocks (part-day or multi-day, business or staff) ----


def block_out(c: Closure) -> BlockOut:
    return BlockOut(
        id=c.pk,
        kind=c.kind,
        staff_id=c.staff_id,
        starts_at=c.starts_at,
        ends_at=c.ends_at,
        reason=c.reason,
    )


@provider_router.get("/businesses/{business_id}/blocks", response=list[BlockOut])
def list_blocks(
    request: HttpRequest,
    business_id: uuid.UUID,
    after: datetime | None = None,
    before: datetime | None = None,
    staff_id: int | None = None,
) -> list[BlockOut]:
    user = owner_only(request)
    rows = profile.list_blocks(
        user.pk, str(business_id), after=after, before=before, staff_id=staff_id
    )
    return [block_out(c) for c in rows]


@provider_router.post("/businesses/{business_id}/blocks", response={201: BlockOut})
def create_block(
    request: HttpRequest, business_id: uuid.UUID, payload: BlockIn
) -> Status[BlockOut]:
    user = owner_only(request)
    block = profile.create_block(
        user.pk,
        str(business_id),
        starts_at=payload.starts_at,
        ends_at=payload.ends_at,
        kind=payload.kind,
        reason=payload.reason,
        staff_id=payload.staff_id,
    )
    return Status(201, block_out(block))


@provider_router.delete("/blocks/{block_id}", response={204: None})
def remove_block(request: HttpRequest, block_id: int) -> Status[Any]:
    user = owner_only(request)
    profile.delete_closure(user.pk, block_id)
    return Status(204, None)

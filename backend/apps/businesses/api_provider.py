"""Provider (business owner) endpoints: onboarding, documents and media. Tagged `provider`."""

import uuid
from typing import Any

from django.http import HttpRequest, HttpResponse
from ninja import Body, File, Form, Router, Status, UploadedFile

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.core import ratelimit
from apps.core.errors import not_found
from apps.core.http import get_client_ip
from apps.core.permissions import require_role

from . import documents, media, onboarding, selectors
from .models import Business
from .provider_schemas import (
    BusinessCreateIn,
    ChecklistItemOut,
    DocumentOut,
    OnboardingOut,
    OwnerBusinessOut,
    PhotoOutOwner,
    StarterItemOut,
    SubmitIn,
)

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

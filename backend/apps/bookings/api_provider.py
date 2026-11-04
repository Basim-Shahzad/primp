"""Provider-side booking endpoints (staff impact workflow now; dashboard actions follow)."""

import uuid
from typing import Literal

from django.http import HttpRequest
from ninja import Router

from apps.accounts.auth import jwt_auth
from apps.accounts.roles import Role
from apps.core.errors import validation_error
from apps.core.permissions import require_role

from . import staff_impact
from .provider_schemas import (
    ApplyOut,
    DeactivateIn,
    ImpactItemOut,
    ImpactOut,
    ResolutionIn,
    StaffServicesIn,
)

provider_router = Router(tags=["provider"], auth=jwt_auth)
owner_only = require_role(Role.BUSINESS_OWNER)


def to_resolutions(items: list[ResolutionIn]) -> list[staff_impact.Resolution]:
    return [staff_impact.Resolution(str(i.appointment_id), i.action, i.new_staff_id) for i in items]


def parse_ids(raw: str) -> list[int]:
    try:
        ids = [int(x) for x in raw.split(",") if x.strip()]
    except ValueError:
        raise validation_error({"service_ids": ["Use a comma-separated list of ids."]}) from None
    if len(ids) > 200:
        raise validation_error({"service_ids": ["Too many ids."]})
    return ids


@provider_router.get("/staff/{staff_id}/impact", response=ImpactOut)
def staff_impact_view(
    request: HttpRequest,
    staff_id: int,
    action: Literal["deactivate", "remove_services"],
    service_ids: str = "",
) -> ImpactOut:
    user = owner_only(request)
    items = staff_impact.impact(user.pk, staff_id, action, parse_ids(service_ids))
    return ImpactOut(
        count=len(items),
        appointments=[
            ImpactItemOut(
                appointment_id=uuid.UUID(i.appointment_id),
                status=i.status,
                start=i.start,
                end=i.end,
                customer_name=i.customer_name,
                services=i.services,
            )
            for i in items
        ],
    )


@provider_router.post("/staff/{staff_id}/deactivate", response=ApplyOut)
def deactivate_staff(request: HttpRequest, staff_id: int, payload: DeactivateIn) -> ApplyOut:
    user = owner_only(request)
    result = staff_impact.deactivate(user.pk, staff_id, to_resolutions(payload.resolutions))
    return ApplyOut(reassigned=result.reassigned, cancelled=result.cancelled)


@provider_router.put("/staff/{staff_id}/services", response=ApplyOut)
def put_staff_services(request: HttpRequest, staff_id: int, payload: StaffServicesIn) -> ApplyOut:
    user = owner_only(request)
    result = staff_impact.set_services(
        user.pk, staff_id, payload.service_ids, to_resolutions(payload.resolutions)
    )
    return ApplyOut(reassigned=result.reassigned, cancelled=result.cancelled)

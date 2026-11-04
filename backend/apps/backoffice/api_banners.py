"""Banner endpoints: admin management (`/admin/banners`) and the public feed (`/banners`)."""

from typing import Any

from django.core.files.storage import default_storage
from django.http import HttpRequest
from django.utils import timezone
from ninja import File, Query, Router, Status, UploadedFile

from apps.core.errors import validation_error
from apps.core.http import get_client_ip

from . import banners
from .permissions import admin_auth, require_permission
from .schemas_banners import (
    BannerIn,
    BannerOut,
    BannerPatchIn,
    BannerQuery,
    PublicBannerOut,
    ReorderIn,
)

router = Router(tags=["admin"], auth=admin_auth)
public_router = Router(tags=["banners"])
NON_NULL = ("title", "subtitle", "link_url", "starts_at", "ends_at", "is_enabled")


def out(b: Any) -> BannerOut:
    now = timezone.now()
    if not b.is_enabled:
        status = "disabled"
    elif b.ends_at <= now:
        status = "expired"
    elif b.starts_at > now:
        status = "scheduled"
    else:
        status = "active"
    pending_impressions, pending_clicks = banners.pending_counts(b.pk)
    return BannerOut(
        id=b.pk,
        title=b.title,
        subtitle=b.subtitle,
        link_url=b.link_url,
        city_id=b.city_id,
        starts_at=b.starts_at,
        ends_at=b.ends_at,
        is_enabled=b.is_enabled,
        sort_order=b.sort_order,
        desktop_image_url=str(default_storage.url(b.desktop_image)) if b.desktop_image else None,
        mobile_image_url=str(default_storage.url(b.mobile_image)) if b.mobile_image else None,
        impressions=b.impressions + pending_impressions,
        clicks=b.clicks + pending_clicks,
        status=status,
        version=b.version,
    )


@router.get("/banners", response=list[BannerOut])
def list_banners(request: HttpRequest, params: Query[BannerQuery]) -> list[BannerOut]:
    require_permission("banners.manage")(request)
    return [out(b) for b in banners.admin_list(params.status, params.city_id)]


@router.post("/banners", response={201: BannerOut})
def create_banner(request: HttpRequest, payload: BannerIn) -> Status[BannerOut]:
    actor = require_permission("banners.manage")(request)
    return Status(201, out(banners.create(actor, payload.model_dump())))


@router.post("/banners/reorder", response=list[BannerOut])
def reorder_banners(request: HttpRequest, payload: ReorderIn) -> list[BannerOut]:
    actor = require_permission("banners.manage")(request)
    return [out(b) for b in banners.reorder(actor, payload.ids)]


@router.patch("/banners/{banner_id}", response=BannerOut)
def patch_banner(request: HttpRequest, banner_id: int, payload: BannerPatchIn) -> BannerOut:
    actor = require_permission("banners.manage")(request)
    changes = payload.model_dump(exclude_unset=True)
    version = changes.pop("version")
    nulls = [k for k in NON_NULL if k in changes and changes[k] is None]
    if nulls:
        raise validation_error({k: ["This field may not be null."] for k in nulls})
    return out(banners.update(actor, banner_id, version, changes))


@router.delete("/banners/{banner_id}", response={204: None})
def delete_banner(request: HttpRequest, banner_id: int, version: int) -> Status[None]:
    actor = require_permission("banners.manage")(request)
    banners.delete(actor, banner_id, version)
    return Status(204, None)


@router.post("/banners/{banner_id}/images/{kind}", response=BannerOut)
def upload_banner_image(
    request: HttpRequest, banner_id: int, kind: str, file: File[UploadedFile]
) -> BannerOut:
    actor = require_permission("banners.manage")(request)
    if kind not in banners.SIZES:
        raise validation_error({"kind": ["Use desktop or mobile."]})
    return out(banners.set_image(actor, banner_id, kind, file.read()))


# ---------------------------------------------------------------- public


@public_router.get("", auth=None, response=list[PublicBannerOut])
def public_banners(request: HttpRequest, city_id: int | None = None) -> list[dict[str, Any]]:
    return banners.public_feed(city_id)


@public_router.post("/{banner_id}/impression", auth=None, response={204: None})
def impression(request: HttpRequest, banner_id: int) -> Status[None]:
    banners.track(banner_id, "impressions", get_client_ip(request) or "unknown")
    return Status(204, None)


@public_router.post("/{banner_id}/click", auth=None, response={204: None})
def click(request: HttpRequest, banner_id: int) -> Status[None]:
    banners.track(banner_id, "clicks", get_client_ip(request) or "unknown")
    return Status(204, None)

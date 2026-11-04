"""Admin platform configuration endpoints."""

from django.http import HttpRequest
from ninja import Query, Router

from .pagination import slice_page
from .permissions import admin_auth, require_permission
from .schemas_config import (
    HistoryPage,
    HistoryQuery,
    HistoryRow,
    SettingChangedOut,
    SettingOut,
    SettingPatchIn,
)
from .services import config as service

router = Router(tags=["admin"], auth=admin_auth)


@router.get("/config", response=list[SettingOut])
def list_settings(request: HttpRequest) -> list[dict[str, object]]:
    require_permission("config.manage", reauth=False)(request)
    return service.listing()


@router.patch("/config/{key}", response=SettingChangedOut)
def patch_setting(request: HttpRequest, key: str, payload: SettingPatchIn) -> SettingChangedOut:
    actor = require_permission("config.manage")(request)
    return SettingChangedOut(**service.set_value(actor, key, payload.value, payload.version))


@router.get("/config/{key}/history", response=HistoryPage)
def setting_history(request: HttpRequest, key: str, params: Query[HistoryQuery]) -> HistoryPage:
    require_permission("config.manage", reauth=False)(request)
    rows, total = slice_page(service.history(key), params)
    return HistoryPage(
        items=[
            HistoryRow(
                version=h.version,
                old_value=h.old_value,
                new_value=h.new_value,
                changed_by=h.changed_by.public_id if h.changed_by_id else None,
                changed_at=h.changed_at,
            )
            for h in rows
        ],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )

"""Platform configuration changes by super admins (Plan 6.3, Step 9)."""

from typing import Any

from django.db import IntegrityError, transaction
from django.db.models import QuerySet
from django.utils.translation import gettext_lazy as _
from pydantic import ValidationError

from apps.core import audit
from apps.core.errors import conflict, fields_from_pydantic, not_found, validation_error

from .. import config_service
from ..config_registry import REGISTRY, SettingDef, UnknownSetting, cross_check, definition
from ..models import PlatformSetting, PlatformSettingHistory
from ..permissions import AdminActor

PERMISSION = "config.manage"


def spec_of(key: str) -> SettingDef:
    try:
        return definition(key)
    except UnknownSetting:
        raise not_found() from None


def listing() -> list[dict[str, Any]]:
    stored = {s.key: s for s in PlatformSetting.objects.select_related("updated_by")}
    rows = []
    for key, spec in sorted(REGISTRY.items()):
        row = stored.get(key)
        rows.append(
            {
                "key": key,
                "description": spec.description,
                "value": config_service.get(key),
                "default": spec.adapter.dump_python(
                    spec.adapter.validate_python(spec.default()), mode="json"
                ),
                "is_default": row is None,
                "version": row.version if row else 0,
                "value_schema": spec.schema(),
                "updated_by": row.updated_by.public_id if row and row.updated_by else None,
                "updated_at": row.updated_at if row else None,
            }
        )
    return rows


def set_value(actor: AdminActor, key: str, value: Any, version: int) -> dict[str, Any]:
    actor.require(PERMISSION)  # includes the re-authentication check (high risk)
    spec = spec_of(key)
    try:
        clean = spec.adapter.dump_python(spec.adapter.validate_python(value), mode="json")
    except ValidationError as exc:
        errors = [{**e, "msg": str(e.get("ctx", {}).get("error", e["msg"]))} for e in exc.errors()]
        raise validation_error(
            {"value": [f"{k}: {m}" for k, ms in fields_from_pydantic(errors).items() for m in ms]}
        ) from exc
    problem = cross_check(key, clean, config_service.get)
    if problem:
        raise validation_error({"value": [problem]})
    try:
        with transaction.atomic():
            row = PlatformSetting.objects.select_for_update().filter(key=key).first()
            current = row.version if row else 0
            if current != version:
                raise conflict("stale_version", _("The setting changed; reload it and try again."))
            old = (
                row.value
                if row
                else spec.adapter.dump_python(
                    spec.adapter.validate_python(spec.default()), mode="json"
                )
            )
            if row is None:
                row = PlatformSetting(key=key, schema_id=key)
            row.value = clean
            row.version = current + 1
            row.updated_by_id = actor.id
            row.save()
            PlatformSettingHistory.objects.create(
                key=key, old_value=old, new_value=clean, version=row.version, changed_by_id=actor.id
            )
            audit.record(
                "admin.config_changed",
                actor_id=actor.id,
                target_type="setting",
                target_id=key,
                before={"value": old, "version": current},
                after={"value": clean, "version": row.version},
                ip=actor.ip,
            )
            config_service.invalidate(key)
    except IntegrityError as exc:  # two admins created the first override at once
        raise conflict("stale_version", _("The setting changed; reload it and try again.")) from exc
    return {"key": key, "value": clean, "version": row.version}


def history(key: str) -> QuerySet[PlatformSettingHistory]:
    spec_of(key)
    return PlatformSettingHistory.objects.filter(key=key).select_related("changed_by")

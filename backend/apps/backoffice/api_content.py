"""Admin notification-template and point-rule endpoints."""

from typing import Any

from django.http import HttpRequest
from ninja import Router

from .permissions import admin_auth, require_permission
from .schemas_content import (
    RuleOut,
    RulePatchIn,
    TemplateOut,
    TemplatePatchIn,
    TemplatePreviewIn,
    TemplatePreviewOut,
)
from .services import content as service

router = Router(tags=["admin"], auth=admin_auth)


@router.get("/notification-templates", response=list[TemplateOut])
def list_templates(request: HttpRequest, key: str | None = None) -> list[dict[str, Any]]:
    require_permission("config.manage", reauth=False)(request)
    return service.list_templates(key)


@router.post("/notification-templates/{template_id}/preview", response=TemplatePreviewOut)
def preview_template(
    request: HttpRequest, template_id: int, payload: TemplatePreviewIn
) -> dict[str, str]:
    require_permission("config.manage", reauth=False)(request)
    return service.preview_template(template_id, payload.subject, payload.body)


@router.patch("/notification-templates/{template_id}", response=TemplateOut)
def patch_template(
    request: HttpRequest, template_id: int, payload: TemplatePatchIn
) -> dict[str, Any]:
    actor = require_permission("config.manage")(request)
    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    changes = {k: v for k, v in changes.items() if v is not None}
    return service.patch_template(actor, template_id, payload.version, changes)


@router.get("/point-rules", response=list[RuleOut])
def list_rules(request: HttpRequest) -> list[dict[str, Any]]:
    require_permission("config.manage", reauth=False)(request)
    return service.list_rules()


@router.patch("/point-rules/{rule_id}", response=RuleOut)
def patch_rule(request: HttpRequest, rule_id: int, payload: RulePatchIn) -> dict[str, Any]:
    actor = require_permission("config.manage")(request)
    changes = payload.model_dump(exclude_unset=True, exclude={"version"})
    return service.patch_rule(actor, rule_id, payload.version, changes)

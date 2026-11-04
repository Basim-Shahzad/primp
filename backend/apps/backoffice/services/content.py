"""Notification templates and point rules edited by admins (Plan 6.3, Step 9)."""

from typing import Any

from django.utils.translation import gettext_lazy as _

from apps.core import audit
from apps.core.errors import conflict, not_found, validation_error
from apps.loyalty import adminops as loyalty
from apps.notifications import adminops as notifications

from ..permissions import AdminActor

PERMISSION = "config.manage"
STALE = _("This record changed; reload it and try again.")


def _stale() -> Any:
    return conflict("stale_version", STALE)


def list_templates(key: str | None) -> list[dict[str, Any]]:
    return [notifications.template_snapshot(t) for t in notifications.list_templates(key)]


def preview_template(template_id: int, subject: str | None, body: str) -> dict[str, str]:
    t = notifications.find_template(template_id)
    if t is None:
        raise not_found()
    try:
        return notifications.preview(t.key, subject if subject is not None else t.subject, body)
    except notifications.TemplateError as exc:
        raise validation_error({exc.field: [exc.message]}) from exc


def patch_template(
    actor: AdminActor, template_id: int, version: int, changes: dict[str, Any]
) -> dict[str, Any]:
    actor.require(PERMISSION)
    try:
        result = notifications.update_template(template_id, version, changes)
    except notifications.StaleTemplate:
        raise _stale() from None
    except notifications.TemplateError as exc:
        raise validation_error({exc.field: [exc.message]}) from exc
    if result is None:
        raise not_found()
    before, after = result
    audit.record(
        "admin.template_changed",
        actor_id=actor.id,
        target_type="notification_template",
        target_id=template_id,
        before=before,
        after=after,
        ip=actor.ip,
    )
    return after


def list_rules() -> list[dict[str, Any]]:
    return [loyalty.rule_snapshot(r) for r in loyalty.list_rules()]


def patch_rule(
    actor: AdminActor, rule_id: int, version: int, changes: dict[str, Any]
) -> dict[str, Any]:
    actor.require(PERMISSION)
    try:
        result = loyalty.update_rule(rule_id, version, changes)
    except loyalty.StaleRule:
        raise _stale() from None
    except loyalty.RuleError as exc:
        raise validation_error({exc.field: [exc.message]}) from exc
    if result is None:
        raise not_found()
    before, after = result
    audit.record(
        "admin.point_rule_changed",
        actor_id=actor.id,
        target_type="point_rule",
        target_id=rule_id,
        before=before,
        after=after,
        ip=actor.ip,
    )
    return after

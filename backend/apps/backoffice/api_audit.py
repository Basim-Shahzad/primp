"""Admin audit-log viewer."""

from django.http import HttpRequest, StreamingHttpResponse
from ninja import Query, Router

from apps.core import audit

from .pagination import slice_page
from .permissions import admin_auth, require_permission
from .schemas_audit import AuditPage, AuditQuery, AuditRow, ExportQuery
from .services import audit_log as service

router = Router(tags=["admin"], auth=admin_auth)


@router.get("/audit-log", response=AuditPage)
def list_audit(request: HttpRequest, params: Query[AuditQuery]) -> AuditPage:
    require_permission("audit.view", reauth=False)(request)
    qs = service.search(**params.dict(exclude={"limit", "offset"}))
    rows, total = slice_page(qs, params)
    actors = service.actors_of(rows)
    return AuditPage(
        items=[AuditRow(**service.render(r, actors)) for r in rows],
        total=total,
        limit=params.limit,
        offset=params.offset,
    )


@router.get("/audit-log/export.csv", response={200: None})
def export_audit(request: HttpRequest, params: Query[ExportQuery]) -> StreamingHttpResponse:
    actor = require_permission("audit.view", reauth=False)(request)
    qs = service.search(**params.dict())
    audit.record(
        "admin.audit_exported",
        actor_id=actor.id,
        target_type="audit_log",
        after={k: str(v) for k, v in params.dict().items() if v is not None},
        ip=actor.ip,
    )
    response = StreamingHttpResponse(service.export_lines(qs), content_type="text/csv")
    response["Content-Disposition"] = 'attachment; filename="audit-log.csv"'
    response["Cache-Control"] = "no-store"
    return response

"""Test-only URLconf exposing routes that exercise the shared API plumbing."""

from django.core.exceptions import PermissionDenied
from django.http import Http404
from django.urls import path
from ninja import NinjaAPI, Query, Schema
from ninja.errors import HttpError
from ninja.throttling import AnonRateThrottle

from apps.core.errors import AppError, register_exception_handlers
from apps.core.models import AuditLog
from apps.core.pagination import CursorParams, paginate_cursor

api = NinjaAPI(urls_namespace="core-test")
register_exception_handlers(api)


class In(Schema):
    name: str
    age: int


@api.get("app-error")
def app_error(request):  # type: ignore[no-untyped-def]
    raise AppError("slot_unavailable", "Taken", 409, {"slot": ["gone"]})


@api.post("validate")
def validate(request, payload: In):  # type: ignore[no-untyped-def]
    return {"ok": True}


@api.get("validate-query")
def validate_query(request, lat: float):  # type: ignore[no-untyped-def]
    return {"lat": lat}


@api.get("not-found")
def nf(request):  # type: ignore[no-untyped-def]
    raise Http404


@api.get("denied")
def denied(request):  # type: ignore[no-untyped-def]
    raise PermissionDenied


@api.get("unauth")
def unauth(request):  # type: ignore[no-untyped-def]
    raise HttpError(401, "nope")


@api.get("throttled", throttle=[AnonRateThrottle("3/min")])
def throttled(request):  # type: ignore[no-untyped-def]
    return {}


@api.get("boom")
def boom(request):  # type: ignore[no-untyped-def]
    raise RuntimeError("secret database password leaked")


@api.get("page")
def page(request, params: Query[CursorParams]):  # type: ignore[no-untyped-def]
    result = paginate_cursor(
        AuditLog.objects.all(),
        ordering=["-created_at", "-id"],
        cursor=params.cursor,
        limit=params.limit,
    )
    return {"ids": [r.id for r in result.items], "next_cursor": result.next_cursor}


urlpatterns = [path("t/", api.urls)]

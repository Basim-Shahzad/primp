"""One error format everywhere: {"code": ..., "message": ..., "fields": {...}}."""

import logging
from typing import Any

from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpRequest, HttpResponse
from django.utils.translation import gettext_lazy as _
from ninja import NinjaAPI
from ninja.errors import HttpError, ValidationError

logger = logging.getLogger(__name__)

HTTP_CODES = {
    400: "bad_request",
    401: "unauthenticated",
    403: "permission_denied",
    404: "not_found",
    409: "conflict",
    429: "throttled",
}


class AppError(Exception):
    def __init__(
        self,
        code: str,
        message: Any = "",
        status: int = 400,
        fields: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(str(message))
        self.code = code
        self.message = message
        self.status = status
        self.fields = fields or {}


def not_found(message: Any = None) -> AppError:
    return AppError("not_found", message or _("Not found."), 404)


def conflict(code: str, message: Any) -> AppError:
    return AppError(code, message, 409)


def forbidden(message: Any = None) -> AppError:
    return AppError("permission_denied", message or _("You do not have permission."), 403)


def body(code: str, message: Any, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"code": code, "message": str(message), "fields": fields or {}}


def _respond(
    api: NinjaAPI, request: HttpRequest, status: int, payload: dict[str, Any]
) -> HttpResponse:
    return api.create_response(request, payload, status=status)


def _field_errors(errors: list[dict[str, Any]]) -> dict[str, list[str]]:
    fields: dict[str, list[str]] = {}
    for err in errors:
        loc = [str(p) for p in err.get("loc", [])]
        if loc and loc[0] == "body" and len(loc) > 2:
            loc = loc[2:]
        elif loc:
            loc = loc[1:]
        name = ".".join(loc) or "non_field"
        fields.setdefault(name, []).append(str(err.get("msg", "Invalid value.")))
    return fields


def register_exception_handlers(api: NinjaAPI) -> None:
    @api.exception_handler(AppError)
    def app_error(request: HttpRequest, exc: AppError) -> HttpResponse:
        return _respond(api, request, exc.status, body(exc.code, exc.message, exc.fields))

    @api.exception_handler(ValidationError)
    def validation(request: HttpRequest, exc: ValidationError) -> HttpResponse:
        return _respond(
            api,
            request,
            422,
            body("validation_error", _("Invalid input."), _field_errors(exc.errors)),
        )

    @api.exception_handler(HttpError)
    def http_error(request: HttpRequest, exc: HttpError) -> HttpResponse:
        code = HTTP_CODES.get(exc.status_code, "http_error")
        message = str(exc.message) or code
        return _respond(api, request, exc.status_code, body(code, message))

    @api.exception_handler(Http404)
    def http404(request: HttpRequest, exc: Http404) -> HttpResponse:
        return _respond(api, request, 404, body("not_found", _("Not found.")))

    @api.exception_handler(PermissionDenied)
    def denied(request: HttpRequest, exc: PermissionDenied) -> HttpResponse:
        return _respond(
            api, request, 403, body("permission_denied", _("You do not have permission."))
        )

    @api.exception_handler(Exception)
    def unexpected(request: HttpRequest, exc: Exception) -> HttpResponse:
        logger.exception("Unhandled error", extra={"path": request.path})
        return _respond(api, request, 500, body("server_error", _("Something went wrong.")))

import uuid
from collections.abc import Callable

from django.http import HttpRequest, HttpResponse

from .logging import request_id_var

HEADER = "X-Request-ID"


class RequestIDMiddleware:
    """Attach a request ID (inbound header when sane, else generated) to logs and response."""

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        incoming = request.headers.get(HEADER, "")
        rid = incoming if incoming and len(incoming) <= 64 and incoming.isprintable() else ""
        rid = rid or uuid.uuid4().hex
        token = request_id_var.set(rid)
        request.request_id = rid  # type: ignore[attr-defined]
        try:
            response = self.get_response(request)
        finally:
            request_id_var.reset(token)
        response[HEADER] = rid
        return response

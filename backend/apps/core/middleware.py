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


class AdminIPAllowListMiddleware:
    """Only allow-listed addresses reach the Django admin when ADMIN_ALLOWED_IPS is set.

    The admin URL prefix is `settings.ADMIN_URL_PATH`. An empty list allows everyone (local
    development); a non-empty list denies every other address with a bare 404 so the admin's
    existence is not advertised.
    """

    def __init__(self, get_response: Callable[[HttpRequest], HttpResponse]) -> None:
        self.get_response = get_response

    def __call__(self, request: HttpRequest) -> HttpResponse:
        from django.conf import settings

        allowed = [str(ip) for ip in getattr(settings, "ADMIN_ALLOWED_IPS", [])]
        prefix = "/" + str(getattr(settings, "ADMIN_URL_PATH", "admin/")).lstrip("/")
        if allowed and request.path.startswith(prefix):
            from .http import get_client_ip

            if get_client_ip(request) not in allowed:
                from django.http import HttpResponseNotFound

                return HttpResponseNotFound()
        return self.get_response(request)

from django.conf import settings
from django.http import HttpRequest


def get_client_ip(request: HttpRequest) -> str | None:
    """Client IP. X-Forwarded-For is only honoured behind TRUSTED_PROXY_COUNT proxies."""
    proxies = int(getattr(settings, "TRUSTED_PROXY_COUNT", 0))
    forwarded: str = request.META.get("HTTP_X_FORWARDED_FOR", "")
    if proxies and forwarded:
        parts = [p.strip() for p in forwarded.split(",") if p.strip()]
        if len(parts) >= proxies:
            return parts[-proxies]
    remote: str | None = request.META.get("REMOTE_ADDR")
    return remote or None

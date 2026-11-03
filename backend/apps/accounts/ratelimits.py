"""Per-IP rate limits for the auth endpoints that are not already limited inside a service.

register, login and OTP requests enforce their own (stricter, per-identity) limits in
`services`/`otp`; `SERVICE_LIMITED` lists them so a test can prove every auth route is covered.
"""

from django.http import HttpRequest

from apps.core import ratelimit
from apps.core.http import get_client_ip

# name -> (max requests, window seconds)
LIMITS: dict[str, tuple[int, int]] = {
    "refresh": (120, 60),
    "logout": (60, 60),
    "email_verify": (30, 3600),
    "otp_verify": (30, 600),
    "google": (30, 60),
    "password_reset_request": (10, 3600),
    "password_reset_confirm": (10, 3600),
}

SERVICE_LIMITED = {"/auth/register", "/auth/login", "/auth/otp/request"}

PATHS = {
    "/auth/refresh": "refresh",
    "/auth/logout": "logout",
    "/auth/email/verify": "email_verify",
    "/auth/otp/verify": "otp_verify",
    "/auth/google": "google",
    "/auth/password/reset/request": "password_reset_request",
    "/auth/password/reset/confirm": "password_reset_confirm",
}


def check(request: HttpRequest, name: str) -> None:
    """Raise 429 when this IP exceeded the limit for the endpoint."""
    limit, window = LIMITS[name]
    ip = get_client_ip(request) or "unknown"
    ratelimit.enforce(f"auth:{name}:{ip}", limit=limit, window_seconds=window)

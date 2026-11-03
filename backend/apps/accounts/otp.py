"""Phone OTP: hashed in Redis, 5 minute validity, single use, attempt/rate limited.

Expiry and cooldowns are stored as timestamps inside the cached values (cache TTL is only
garbage collection) so they are exact and testable with a frozen clock.
"""

import hashlib
import hmac
import secrets
import time
from typing import Any

from django.conf import settings
from django.core.cache import cache
from django.utils.translation import gettext_lazy as _

from apps.core import ratelimit
from apps.core.errors import AppError
from apps.notifications.services import queue_sms

OTP_TTL = 300
RESEND_COOLDOWN = 60
MAX_VERIFY_ATTEMPTS = 5
MAX_REQUESTS_PER_HOUR = 5
MAX_REQUESTS_PER_IP_HOUR = 20

LOGIN = "login"
PHONE_CHANGE = "phone_change"


def _code_key(purpose: str, phone: str) -> str:
    return f"otp:code:{purpose}:{phone}"


def _hash(purpose: str, phone: str, code: str) -> str:
    msg = f"{purpose}:{phone}:{code}".encode()
    return hmac.new(settings.SECRET_KEY.encode(), msg, hashlib.sha256).hexdigest()


def _throttle(retry_after: int) -> dict[str, Any]:
    return {"retry_after": [str(retry_after)]}


def request_otp(phone: str, purpose: str, ip: str | None) -> None:
    now = time.time()
    if ip:
        ratelimit.enforce(f"otp-ip:{ip}", limit=MAX_REQUESTS_PER_IP_HOUR, window_seconds=3600)
    with cache.lock(f"lock:otp:{phone}", timeout=5, blocking_timeout=5):  # type: ignore[attr-defined]
        meta: dict[str, Any] = cache.get(f"otp:meta:{phone}") or {"last": 0.0, "sent": []}
        wait = RESEND_COOLDOWN - (now - meta["last"])
        if wait > 0:
            raise AppError(
                "otp_cooldown",
                _("Please wait before requesting another code."),
                429,
                _throttle(int(wait) + 1),
            )
        sent = [t for t in meta["sent"] if now - t < 3600]
        if len(sent) >= MAX_REQUESTS_PER_HOUR:
            raise AppError(
                "otp_rate_limited",
                _("Too many codes requested. Try again later."),
                429,
                _throttle(int(3600 - (now - sent[0])) + 1),
            )
        code = f"{secrets.randbelow(1_000_000):06d}"
        cache.set(
            _code_key(purpose, phone),
            {"h": _hash(purpose, phone, code), "exp": now + OTP_TTL, "att": 0},
            timeout=OTP_TTL + 60,
        )
        cache.set(f"otp:meta:{phone}", {"last": now, "sent": [*sent, now]}, timeout=3700)
    queue_sms(
        phone,
        str(
            _("Your Primp verification code is %(code)s. It expires in 5 minutes.") % {"code": code}
        ),
    )


def _invalid() -> AppError:
    return AppError("invalid_otp", _("The code is incorrect or has expired."), 400)


def verify_otp(phone: str, purpose: str, code: str) -> None:
    key = _code_key(purpose, phone)
    with cache.lock(f"lock:otpv:{phone}", timeout=5, blocking_timeout=5):  # type: ignore[attr-defined]
        record: dict[str, Any] | None = cache.get(key)
        if record is None or time.time() > record["exp"]:
            cache.delete(key)
            raise _invalid()
        record["att"] += 1
        if record["att"] > MAX_VERIFY_ATTEMPTS:
            cache.delete(key)
            raise AppError(
                "otp_attempts_exceeded", _("Too many incorrect attempts. Request a new code."), 429
            )
        if not hmac.compare_digest(record["h"], _hash(purpose, phone, code)):
            if record["att"] >= MAX_VERIFY_ATTEMPTS:
                cache.delete(key)
            else:
                cache.set(key, record, timeout=max(int(record["exp"] - time.time()) + 60, 1))
            raise _invalid()
        cache.delete(key)  # single use

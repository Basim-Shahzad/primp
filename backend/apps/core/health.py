import logging
from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache
from django.db import connection
from django.http import HttpRequest, JsonResponse
from kombu import Connection

logger = logging.getLogger(__name__)


def check_database() -> None:
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")


def check_redis() -> None:
    cache.set("readyz", "1", timeout=5)
    if cache.get("readyz") != "1":
        raise RuntimeError("Redis round-trip failed")


def check_broker() -> None:
    with Connection(settings.CELERY_BROKER_URL, connect_timeout=2) as conn:
        conn.ensure_connection(max_retries=1, timeout=2)


CHECKS: dict[str, Callable[[], None]] = {
    "database": check_database,
    "redis": check_redis,
    "broker": check_broker,
}


def healthz(request: HttpRequest) -> JsonResponse:
    return JsonResponse({"status": "ok"})


def readyz(request: HttpRequest) -> JsonResponse:
    results: dict[str, str] = {}
    for name, check in CHECKS.items():
        try:
            check()
            results[name] = "ok"
        except Exception:
            logger.exception("Readiness check failed", extra={"check": name})
            results[name] = "failed"
    ok = all(v == "ok" for v in results.values())
    return JsonResponse(
        {"status": "ok" if ok else "unavailable", "checks": results}, status=200 if ok else 503
    )

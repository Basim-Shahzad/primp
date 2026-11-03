import json
from typing import Any

from django.test import Client

from apps.accounts.factories import make_customer
from apps.accounts.models import User
from apps.accounts.tests.helpers import bearer, login_client
from apps.bookings.tests.helpers import TUESDAY, local
from apps.businesses.factories import (
    BusinessFactory,
    ServiceFactory,
    StaffFactory,
    StaffServiceFactory,
    open_all_week,
)
from apps.businesses.models import Business, Service, Staff

HOLD = "/api/v1/appointments/hold"
BOOK = "/api/v1/appointments"


def capacity_business(**kw: Any) -> tuple[Business, Service]:
    kw.setdefault("lead_time_minutes", 0)
    b = BusinessFactory(**kw)
    open_all_week(b)
    return b, ServiceFactory(business=b, duration_minutes=60, price=2000)


def staff_business(staff_count: int = 2, **kw: Any) -> tuple[Business, Service, list[Staff]]:
    kw.setdefault("lead_time_minutes", 0)
    b = BusinessFactory(staff_level_booking=True, **kw)
    open_all_week(b)
    svc = ServiceFactory(business=b, duration_minutes=60, price=2000)
    staff = [StaffFactory(business=b) for _ in range(staff_count)]
    for s in staff:
        StaffServiceFactory(staff=s, service=svc)
    return b, svc, staff


def customer_session() -> tuple[User, dict[str, str]]:
    user = make_customer()
    _, session = login_client(user)
    return user, bearer(session)


def post(url: str, data: Any, headers: dict[str, str], **extra: Any) -> Any:
    return Client().post(
        url, data=json.dumps(data), content_type="application/json", **headers, **extra
    )


def hold_body(
    b: Business, services: list[Service], hour: int = 12, minute: int = 0, **extra: Any
) -> dict[str, Any]:
    return {
        "business_id": str(b.public_id),
        "service_ids": [s.pk for s in services],
        "start": local(TUESDAY, hour, minute).isoformat(),
        **extra,
    }


def place_hold(
    headers: dict[str, str], b: Business, services: list[Service], hour: int = 12, **extra: Any
) -> Any:
    return post(HOLD, hold_body(b, services, hour, **extra), headers)


def book(headers: dict[str, str], token: str, key: str | None = None) -> Any:
    extra = {"HTTP_IDEMPOTENCY_KEY": key} if key else {}
    return post(BOOK, {"hold_token": token}, headers, **extra)

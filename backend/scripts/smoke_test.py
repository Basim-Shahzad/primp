"""End-to-end smoke test against a running Primp API loaded with `seed_demo` data.

    BASE_URL=http://localhost:8000 python scripts/smoke_test.py

Exits non-zero on the first failed check. Creates one throwaway customer and one booking (which it
cancels again), so it is safe to run repeatedly.
"""

import os
import sys
import uuid
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import requests

KARACHI = (24.8607, 67.0011)


class SmokeFailure(Exception):
    pass


def check(condition: object, message: str) -> None:
    if not condition:
        raise SmokeFailure(message)
    print(f"ok  {message}")


def run(base_url: str, http: Any = None) -> None:
    http = http or requests.Session()
    api = base_url.rstrip("/") + "/api/v1"

    def call(method: str, path: str, expect: int | tuple[int, ...], **kwargs: Any) -> Any:
        url = path if path.startswith("http") else api + path
        response = http.request(method, url, timeout=15, **kwargs)
        codes = (expect,) if isinstance(expect, int) else expect
        check(
            response.status_code in codes,
            f"{method} {path.split('?')[0]} -> {response.status_code}",
        )
        return response.json() if response.content else None

    # health
    check(http.get(base_url.rstrip("/") + "/healthz", timeout=10).status_code == 200, "healthz")
    ready = http.get(base_url.rstrip("/") + "/readyz", timeout=10)
    check(ready.status_code == 200, f"readyz {ready.json().get('checks')}")

    # discovery
    found = call(
        "GET",
        "/search/businesses",
        200,
        params={"lat": KARACHI[0], "lng": KARACHI[1], "radius_km": 10, "limit": 10},
    )
    check(found["items"], "search returns seeded businesses")
    business = next(i for i in found["items"] if i["starting_price"])
    page = call("GET", f"/businesses/{business['slug']}", 200, params={"city": "karachi"})
    check(page["services"] and page["hours"], "business page has services and hours")
    service_id = page["services"][0]["services"][0]["id"]
    call("GET", f"/businesses/{business['id']}/reviews", 200)

    # availability: first day in the next week with a free slot
    tz = ZoneInfo(page["timezone"])
    slot = None
    for offset in range(0, 8):
        day = (datetime.now(tz) + timedelta(days=offset)).date().isoformat()
        data = call(
            "GET",
            f"/businesses/{business['id']}/availability",
            200,
            params={"date": day, "service_ids": str(service_id)},
        )
        if data["slots"]:
            slot = data["slots"][0]
            break
    check(slot, "found an available slot")

    # a customer books it
    email = f"smoke-{uuid.uuid4().hex[:10]}@example.com"
    auth = call(
        "POST",
        "/auth/register",
        201,
        json={"name": "Smoke Test", "email": email, "password": "Smoke-test-pass-9"},
    )
    headers = {"Authorization": f"Bearer {auth['access_token']}"}
    me = call("GET", "/users/me", 200, headers=headers)
    check(me["email"] == email and me["role"] == "customer", "users/me returns the new customer")

    hold = call(
        "POST",
        "/appointments/hold",
        201,
        headers=headers,
        json={"business_id": business["id"], "service_ids": [service_id], "start": slot["start"]},
    )
    key = uuid.uuid4().hex
    booked = call(
        "POST",
        "/appointments",
        201,
        headers={**headers, "Idempotency-Key": key},
        json={"hold_token": hold["token"]},
    )
    replay = call(
        "POST",
        "/appointments",
        200,
        headers={**headers, "Idempotency-Key": key},
        json={"hold_token": hold["token"]},
    )
    check(booked["id"] == replay["id"], "idempotent retry returns the same booking")
    check(booked["status"] in ("confirmed", "pending"), f"booking status {booked['status']}")

    # booking history, policy, wishlist, points
    upcoming = call(
        "GET", "/users/me/appointments", 200, headers=headers, params={"scope": "upcoming"}
    )
    check(any(a["id"] == booked["id"] for a in upcoming["items"]), "booking appears under upcoming")
    detail = call("GET", f"/appointments/{booked['id']}", 200, headers=headers)
    check(
        detail["actions"]["can_cancel"] and detail["timeline"],
        "detail has timeline and allowed actions",
    )
    call("GET", f"/appointments/{booked['id']}/policy", 200, headers=headers)
    call("POST", f"/users/me/wishlist/{business['id']}", (200, 201), headers=headers)
    wished = call("GET", "/users/me/wishlist", 200, headers=headers)
    check(any(i["id"] == business["id"] for i in wished["items"]), "wishlist lists the business")
    call("DELETE", f"/users/me/wishlist/{business['id']}", 204, headers=headers)
    points = call("GET", "/users/me/points", 200, headers=headers)
    check(points["balance"] == 0, "no points before completion")
    cancelled = call(
        "PATCH",
        f"/appointments/{booked['id']}/cancel",
        200,
        headers=headers,
        json={"reason": "other"},
    )
    check(cancelled["status"] == "cancelled_by_customer", "booking cancelled")
    call("POST", "/auth/logout", (204,), json={})
    print("smoke test passed")


if __name__ == "__main__":
    try:
        run(os.environ.get("BASE_URL", "http://localhost:8000"))
    except SmokeFailure as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        sys.exit(1)

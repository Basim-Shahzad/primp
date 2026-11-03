"""Cross-cutting security and handover checks (Step 16)."""

import io
import json
import logging
from pathlib import Path
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import Client, override_settings

from apps.accounts import ratelimits
from apps.accounts.factories import UserFactory, make_customer
from apps.accounts.tests.helpers import bearer, login_client, post_json
from apps.core.management.commands.export_openapi import render_schema
from apps.core.redact import mask_phone, redact

BACKEND = Path(__file__).resolve().parents[1]


# ---------------- OpenAPI ----------------


def test_committed_openapi_is_current() -> None:
    committed = (BACKEND / "openapi.json").read_text()
    assert committed == render_schema(), "openapi.json is stale: run `make openapi` and commit it"


def test_export_command_prints_the_schema() -> None:
    out = io.StringIO()
    call_command("export_openapi", stdout=out)
    assert out.getvalue() == render_schema()
    schema = json.loads(out.getvalue())
    assert schema["info"]["title"] == "Primp API"
    assert "/api/v1/appointments/hold" in schema["paths"]


def test_every_public_route_is_versioned_and_documented() -> None:
    schema = json.loads(render_schema())
    assert all(p.startswith("/api/v1/") for p in schema["paths"])
    for path, ops in schema["paths"].items():
        for method, op in ops.items():
            assert op.get("operationId"), (method, path)
            assert op.get("responses"), (method, path)


# ---------------- rate limits ----------------

VALID_BODIES = {
    "/auth/refresh": {},
    "/auth/logout": {},
    "/auth/email/verify": {"token": "x"},
    "/auth/otp/verify": {"phone": "03001234567", "code": "123456"},
    "/auth/google": {"id_token": "x"},
    "/auth/password/reset/request": {"email": "a@example.com"},
    "/auth/password/reset/confirm": {"token": "x", "new_password": "Whatever-pass-1"},
}


def test_every_auth_route_has_a_rate_limit() -> None:
    schema = json.loads(render_schema())
    auth_paths = {
        p.removeprefix("/api/v1") for p in schema["paths"] if p.startswith("/api/v1/auth/")
    }
    covered = set(ratelimits.PATHS) | ratelimits.SERVICE_LIMITED
    assert auth_paths == covered, f"auth routes without a limiter: {auth_paths - covered}"
    assert set(ratelimits.PATHS.values()) == set(ratelimits.LIMITS)
    assert set(VALID_BODIES) == set(ratelimits.PATHS)


@pytest.mark.django_db
@pytest.mark.parametrize("path", sorted(ratelimits.PATHS))
def test_auth_endpoint_returns_429_past_its_limit(path: str) -> None:
    name = ratelimits.PATHS[path]
    url = f"/api/v1{path}"
    with mock.patch.dict(ratelimits.LIMITS, {name: (2, 60)}):
        client = Client()
        codes = [post_json(client, url, VALID_BODIES[path]).status_code for _ in range(3)]
        again = post_json(Client(), url, VALID_BODIES[path])
    assert codes[2] == 429 and 429 not in codes[:2], codes
    assert again.status_code == 429 and again.json()["code"] == "throttled"


@pytest.mark.django_db
def test_rate_limits_are_per_ip() -> None:
    path = "/api/v1/auth/google"
    with mock.patch.dict(ratelimits.LIMITS, {"google": (1, 60)}):
        a = post_json(Client(REMOTE_ADDR="10.0.0.1"), path, {"id_token": "x"})
        b = post_json(Client(REMOTE_ADDR="10.0.0.1"), path, {"id_token": "x"})
        c = post_json(Client(REMOTE_ADDR="10.0.0.2"), path, {"id_token": "x"})
    assert (a.status_code != 429, b.status_code, c.status_code != 429) == (True, 429, True)


@pytest.mark.django_db
def test_service_limited_auth_routes_still_throttle() -> None:
    from apps.accounts import services

    with mock.patch.object(services, "LOGIN_IP_LIMIT", (1, 60)):
        codes = [
            post_json(
                Client(), "/api/v1/auth/login", {"email": "x@example.com", "password": "y"}
            ).status_code
            for _ in range(2)
        ]
    assert codes == [401, 429]


@pytest.mark.django_db
def test_global_user_and_anonymous_throttles_are_wired() -> None:
    from config.api import api

    assert len(api.throttle) == 2
    assert {type(t).__name__ for t in api.throttle} == {"AnonRateThrottle", "AuthRateThrottle"}


# ---------------- CORS / headers ----------------


@pytest.mark.django_db
def test_cors_allow_list_only() -> None:
    with override_settings(CORS_ALLOWED_ORIGINS=["https://primp.pk"]):
        ok = Client().get("/healthz", HTTP_ORIGIN="https://primp.pk")
        bad = Client().get("/healthz", HTTP_ORIGIN="https://evil.example")
    assert ok["Access-Control-Allow-Origin"] == "https://primp.pk"
    assert "Access-Control-Allow-Origin" not in bad


@pytest.mark.django_db
def test_security_headers_present() -> None:
    r = Client().get("/healthz")
    assert r["X-Content-Type-Options"] == "nosniff"
    assert r["X-Frame-Options"] == "DENY"
    assert r["Referrer-Policy"] == "same-origin"
    assert r["X-Request-ID"]


# ---------------- PII in logs ----------------


@pytest.mark.parametrize(
    ("text", "forbidden"),
    [
        ("login failed for sana.private@example.com", "sana.private@example.com"),
        ("call +923001234567 now", "923001234567"),
        ("call 0300-1234567 now", "1234567"),
        ("call 03001234567 now", "03001234567"),
        ("call +92 300 1234567 now", "1234567"),
        ("Authorization: Bearer abc.def-ghi_jkl", "abc.def-ghi_jkl"),
        ("token eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.c2lnbmF0dXJl here", "eyJhbGciOiJIUzI1NiJ9"),
        ("DELETE /api/v1/appointments/hold/SeCrEtHoLdToKeN123 404", "SeCrEtHoLdToKeN123"),
        ("GET /api/v1/search/businesses?lat=24.86&lng=67.00&q=x", "24.86"),
        ("POST ?token=abc123&code=999111", "abc123"),
        ("Your Primp verification code is 482913. It expires", "482913"),
    ],
)
def test_redact_removes_sensitive_values(text: str, forbidden: str) -> None:
    assert forbidden not in redact(text)


def test_redact_leaves_ordinary_text_alone() -> None:
    text = "Booking 123 confirmed for business 45 at 2026-11-03T12:00:00+05:00 price 2000"
    assert redact(text) == text
    assert mask_phone("+923001234567") == "+92300*****67" and mask_phone("123") == "[phone]"


@pytest.mark.django_db
def test_no_pii_in_logs_across_the_main_flows(
    caplog: pytest.LogCaptureFixture, run_commit, frozen
) -> None:  # type: ignore[no-untyped-def]
    from apps.bookings.tests.api_helpers import book, capacity_business, place_hold
    from apps.notifications.adapters import FakeSMS

    email, phone, password = "private.person@example.com", "+923009871234", "Sup3r-secret-pass!"
    caplog.set_level(logging.DEBUG)
    secrets: list[str] = [email, phone, "03009871234", "923009871234", password]

    # register + failing and successful logins + password reset
    c = Client()
    reg = post_json(
        c, "/api/v1/auth/register", {"name": "Private Person", "email": email, "password": password}
    )
    secrets.append(reg.json()["access_token"])
    post_json(c, "/api/v1/auth/login", {"email": email, "password": "wrong-password-1"})
    with run_commit():
        post_json(c, "/api/v1/auth/password/reset/request", {"email": email})
        post_json(c, "/api/v1/auth/password/reset/request", {"email": "ghost.user@example.com"})
    secrets.append("ghost.user@example.com")

    # phone OTP request + verify with a wrong then a right code
    with run_commit():
        post_json(Client(), "/api/v1/auth/otp/request", {"phone": phone})
    code = FakeSMS.outbox[-1].body.split("code is ")[1][:6]
    secrets.append(code)
    post_json(Client(), "/api/v1/auth/otp/verify", {"phone": phone, "code": "000000"})
    verified = post_json(Client(), "/api/v1/auth/otp/verify", {"phone": phone, "code": code})
    secrets.append(verified.json()["access_token"])

    # a booking (hold token travels in a URL) and a search with coordinates
    customer = make_customer(phone="+923001112222", email="booker.private@example.com")
    _, session = login_client(customer)
    secrets += ["booker.private@example.com", "+923001112222", session.refresh_token]
    b, svc = capacity_business()
    token = place_hold(bearer(session), b, [svc]).json()["token"]
    secrets.append(token)
    with run_commit():
        book(bearer(session), token)
    Client().delete(f"/api/v1/appointments/hold/{token}", **bearer(session))
    Client().get("/api/v1/search/businesses", {"lat": 24.123456, "lng": 67.654321})
    Client().get("/api/v1/does-not-exist/9988776655")  # 404 path in the logs

    haystack = caplog.text + "\n".join(
        str(v)
        for r in caplog.records
        for v in r.__dict__.values()
        if isinstance(v, str | int | float)
    )
    assert caplog.records, "expected some log output to inspect"
    for secret in secrets:
        assert secret not in haystack, f"{secret!r} leaked into logs"
    assert "24.123456" not in haystack and "67.654321" not in haystack
    assert UserFactory is not None


@pytest.mark.django_db
def test_json_formatter_redacts_extras() -> None:
    from apps.core.logging import JSONFormatter

    record = logging.getLogger("x").makeRecord(
        "x",
        logging.INFO,
        __file__,
        1,
        "msg %s",
        ("a@b.co",),
        None,
        extra={"note": "ring +923001234567"},
    )
    line = json.loads(JSONFormatter().format(record))
    assert "a@b.co" not in json.dumps(line) and "923001234567" not in json.dumps(line)


def test_console_sms_masks_the_number(caplog: pytest.LogCaptureFixture) -> None:
    from apps.notifications.adapters import ConsoleSMS

    caplog.set_level(logging.INFO)
    with override_settings(DEBUG=False):
        ConsoleSMS().send("+923001234567", "Your Primp verification code is 111222.")
    record = next(r for r in caplog.records if "console adapter" in r.getMessage())
    assert record.to == "+92300*****67" and not hasattr(record, "body")  # type: ignore[attr-defined]
    with override_settings(DEBUG=True):
        ConsoleSMS().send("+923001234567", "code 1")
    assert any(getattr(r, "body", "") == "code 1" for r in caplog.records)


# ---------------- deployment config ----------------


def test_no_secrets_committed_in_settings() -> None:
    for path in (BACKEND / "config" / "settings").glob("*.py"):
        text = path.read_text()
        assert "AKIA" not in text and "BEGIN PRIVATE KEY" not in text, path
    example = (BACKEND / ".env.example").read_text()
    assert "DJANGO_DEBUG=False" in example


def test_env_example_documents_every_environment_variable() -> None:
    """Every env.* lookup in base settings should appear in .env.example or the README table."""
    import re

    settings_text = (BACKEND / "config" / "settings" / "base.py").read_text()
    names = set(re.findall(r'env\.\w+\(\s*"([A-Z0-9_]+)"', settings_text))
    docs = (BACKEND / ".env.example").read_text() + (BACKEND / "README.md").read_text()
    missing = sorted(n for n in names if n not in docs)
    assert not missing, f"undocumented env vars: {missing}"

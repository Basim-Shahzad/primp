from unittest import mock

import pytest
from django.db import OperationalError
from django.test import Client, override_settings

pytestmark = pytest.mark.django_db


def test_healthz(client: Client) -> None:
    assert client.get("/healthz").json() == {"status": "ok"}


def test_readyz_ok(client: Client) -> None:
    r = client.get("/readyz")
    assert r.status_code == 200
    assert r.json()["checks"] == {"database": "ok", "redis": "ok", "broker": "ok"}


def test_readyz_fails_when_database_down(client: Client) -> None:
    with mock.patch("apps.core.health.connection.cursor", side_effect=OperationalError("down")):
        r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["database"] == "failed"


def test_readyz_fails_when_redis_down(client: Client) -> None:
    with mock.patch("apps.core.health.cache.set", side_effect=ConnectionError("down")):
        r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["redis"] == "failed"


def test_readyz_fails_when_broker_down(client: Client) -> None:
    with override_settings(CELERY_BROKER_URL="redis://localhost:1/0"):
        r = client.get("/readyz")
    assert r.status_code == 503
    assert r.json()["checks"]["broker"] == "failed"


def test_docs_served_when_enabled(client: Client) -> None:
    assert client.get("/api/v1/docs").status_code == 200

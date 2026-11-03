import json

import pytest
from django.test import Client, override_settings

pytestmark = pytest.mark.django_db
URLS = override_settings(ROOT_URLCONF="apps.core.tests.urls")


@pytest.fixture(autouse=True)
def _urls():  # type: ignore[no-untyped-def]
    with URLS:
        yield


def test_app_error_shape(client: Client) -> None:
    r = client.get("/t/app-error")
    assert r.status_code == 409
    assert r.json() == {
        "code": "slot_unavailable",
        "message": "Taken",
        "fields": {"slot": ["gone"]},
    }


def test_validation_body_fields(client: Client) -> None:
    r = client.post("/t/validate", data=json.dumps({"name": 5}), content_type="application/json")
    assert r.status_code == 422
    data = r.json()
    assert data["code"] == "validation_error"
    assert "age" in data["fields"]
    assert set(data) == {"code", "message", "fields"}


def test_validation_query_fields(client: Client) -> None:
    r = client.get("/t/validate-query?lat=abc")
    assert r.status_code == 422
    assert "lat" in r.json()["fields"]


def test_404_and_403_and_401(client: Client) -> None:
    assert client.get("/t/not-found").json()["code"] == "not_found"
    r = client.get("/t/denied")
    assert (r.status_code, r.json()["code"]) == (403, "permission_denied")
    r = client.get("/t/unauth")
    assert (r.status_code, r.json()["code"]) == (401, "unauthenticated")


def test_unexpected_error_hides_internals(client: Client) -> None:
    r = client.get("/t/boom")
    assert r.status_code == 500
    assert r.json() == {"code": "server_error", "message": "Something went wrong.", "fields": {}}
    assert "secret" not in r.content.decode()
    assert "Traceback" not in r.content.decode()


def test_throttle_returns_429_in_standard_shape() -> None:
    c = Client()
    codes = [c.get("/t/throttled").status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert c.get("/t/throttled").json()["code"] == "throttled"

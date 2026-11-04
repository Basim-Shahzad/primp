"""One parametrised walk over every admin endpoint x every role (Plan 6.3, A4.3)."""

import pytest
from django.test import Client

from apps.backoffice import permissions
from apps.backoffice.tests.helpers import ROLES, A, call, headers_for, user_for
from apps.backoffice.tests.matrix import ENDPOINTS, PUBLIC, UPLOADS, fill
from config.api import api

pytestmark = pytest.mark.django_db
PREFIX = "/api/v1"


def documented() -> set[tuple[str, str]]:
    ops: set[tuple[str, str]] = set()
    for path, item in api.get_openapi_schema()["paths"].items():
        short = path.removeprefix(PREFIX)
        if short.startswith("/admin"):
            ops.update((method, short) for method in item)
    return ops


def test_every_admin_endpoint_is_in_the_matrix() -> None:
    known = set(ENDPOINTS) | PUBLIC | set(UPLOADS)
    assert documented() - known == set(), "add new admin endpoints to tests/matrix.py"
    assert known - documented() == set(), "stale entries in tests/matrix.py"


def request(method: str, template: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    _perm, body, query = ENDPOINTS[(method, template)]
    url = f"{PREFIX}{fill(template)}" + (f"?{query}" if query else "")
    return call(method, url, headers, body)


@pytest.mark.parametrize(("method", "template"), sorted(ENDPOINTS))
def test_anonymous_and_bad_tokens_get_401(method: str, template: str) -> None:
    assert request(method, template, {}).status_code == 401
    assert request(method, template, {"HTTP_AUTHORIZATION": "Bearer junk"}).status_code == 401


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "template"), sorted(ENDPOINTS))
def test_every_role_on_every_endpoint(method: str, template: str, role: str) -> None:
    permission = ENDPOINTS[(method, template)][0]
    user = user_for(role)
    held = permissions.permissions_of(user)
    is_admin = role in ("support_agent", "ops_admin", "super_admin")
    allowed = is_admin if permission is None else permission in held
    response = request(method, template, headers_for(user))
    code = response.json().get("code") if response.status_code in (401, 403, 404, 409, 422) else ""
    if allowed:
        # it may still stop at re-authentication, never at the permission check
        assert response.status_code != 401 or code == "invalid_credentials", (
            method,
            template,
            role,
        )
        assert code != "permission_denied", (method, template, role, response.content[:150])
    else:
        assert response.status_code == 403 and code == "permission_denied", (
            method,
            template,
            role,
            response.status_code,
            response.content[:150],
        )


def test_login_endpoint_is_the_only_public_admin_route() -> None:
    r = Client().post(f"{A}/auth/login", data="{}", content_type="application/json")
    assert r.status_code == 422  # reaches validation: no token needed
    for method, template in ENDPOINTS:
        assert request(method, template, {}).status_code == 401


def send_upload(method: str, template: str, headers: dict[str, str]):  # type: ignore[no-untyped-def]
    from django.core.files.uploadedfile import SimpleUploadedFile

    file = SimpleUploadedFile("f.csv", b"name,slug,lat,lng,is_pilot\n", content_type="text/csv")
    return Client().post(f"{PREFIX}{fill(template)}", {"file": file}, **headers)


@pytest.mark.parametrize("role", ROLES)
@pytest.mark.parametrize(("method", "template"), sorted(UPLOADS))
def test_upload_endpoints_by_role(method: str, template: str, role: str) -> None:
    permission = UPLOADS[(method, template)]
    user = user_for(role)
    response = send_upload(method, template, headers_for(user))
    code = response.json().get("code", "")
    if permission in permissions.permissions_of(user):
        assert response.status_code not in (401,) and code != "permission_denied"
    else:
        assert response.status_code == 403 and code == "permission_denied"
    assert send_upload(method, template, {}).status_code == 401

"""Plan 6.3 Step 11: cross-cutting guarantees over every admin endpoint."""

from datetime import timedelta

import pytest
import time_machine
from django.utils import timezone

from apps.backoffice import permissions
from apps.backoffice.tests.helpers import A, admin_headers, call
from apps.backoffice.tests.matrix import ENDPOINTS, fill
from apps.businesses.factories import BusinessFactory
from apps.businesses.models import BusinessDocument

pytestmark = pytest.mark.django_db
# high-risk permission but deliberately read-only (a dry run): no re-authentication needed
DRY_RUNS = {
    ("post", "/admin/points/adjust/preview"),
    ("post", "/admin/notification-templates/{template_id}/preview"),
}
MUTATING = {"post", "patch", "put", "delete"}
HIGH_RISK_WRITES = sorted(
    (m, t)
    for (m, t), (perm, _b, _q) in ENDPOINTS.items()
    if perm in permissions.HIGH_RISK and m in MUTATING and (m, t) not in DRY_RUNS
)


def url_of(method: str, template: str) -> str:
    _perm, _body, query = ENDPOINTS[(method, template)]
    return f"/api/v1{fill(template)}" + (f"?{query}" if query else "")


def test_high_risk_writes_exist_in_the_matrix() -> None:
    assert len(HIGH_RISK_WRITES) >= 8


@pytest.mark.parametrize(("method", "template"), HIGH_RISK_WRITES)
def test_high_risk_writes_demand_a_recent_reauthentication(
    method: str, template: str, long_tokens
) -> None:  # type: ignore[no-untyped-def]
    admin, headers = admin_headers("super_admin")
    with time_machine.travel(timezone.now() + timedelta(minutes=16), tick=False):
        body = ENDPOINTS[(method, template)][1]
        r = call(method, url_of(method, template), headers, body)
    assert r.status_code == 403 and r.json()["code"] == "reauth_required", (
        method,
        template,
        r.status_code,
        r.content[:120],
    )


def test_no_endpoint_ever_returns_a_stored_document_path() -> None:
    business = BusinessFactory()
    BusinessDocument.objects.create(
        business=business,
        kind="licence",
        file="documents/leaky-secret-path.png",
        content_type="image/png",
        size=1,
    )
    _, headers = admin_headers("super_admin")
    seen = 0
    for (method, template), (_perm, _body, _q) in sorted(ENDPOINTS.items()):
        if method != "get":
            continue
        r = call("get", url_of(method, template), headers)
        raw = b"".join(r.streaming_content) if r.streaming else r.content
        assert b"leaky-secret-path" not in raw, template
        seen += 1
    assert seen > 15
    assert call("get", f"{A}/review-queue", headers).status_code == 200

import pytest
from django.test import Client, override_settings
from django.utils import timezone

from apps.core.models import AuditLog

pytestmark = pytest.mark.django_db


@pytest.fixture(autouse=True)
def _urls():  # type: ignore[no-untyped-def]
    with override_settings(ROOT_URLCONF="apps.core.tests.urls", THROTTLE_ANON="1000/min"):
        yield


def make(n: int) -> list[int]:
    now = timezone.now()
    # equal timestamps force the id tie-breaker to matter
    rows = AuditLog.objects.bulk_create(
        [AuditLog(action=f"a{i}", created_at=now) for i in range(n)]
    )
    return [r.id for r in rows]


def walk(client: Client, limit: int) -> list[int]:
    seen: list[int] = []
    cursor = ""
    for _ in range(50):
        url = f"/t/page?limit={limit}" + (f"&cursor={cursor}" if cursor else "")
        data = client.get(url).json()
        seen += data["ids"]
        cursor = data["next_cursor"]
        if not cursor:
            return seen
    raise AssertionError("pagination did not terminate")


def test_walks_all_rows_once_in_stable_order(client: Client) -> None:
    ids = make(11)
    seen = walk(client, 3)
    assert seen == sorted(ids, reverse=True)


def test_cursor_stable_when_new_rows_arrive(client: Client) -> None:
    make(6)
    first = client.get("/t/page?limit=3").json()
    make(3)
    second = client.get(f"/t/page?limit=3&cursor={first['next_cursor']}").json()
    assert set(first["ids"]).isdisjoint(second["ids"])


def test_no_next_cursor_on_last_page(client: Client) -> None:
    make(3)
    assert client.get("/t/page?limit=3").json()["next_cursor"] is None


def test_tampered_cursor_rejected(client: Client) -> None:
    make(5)
    cursor = client.get("/t/page?limit=2").json()["next_cursor"]
    r = client.get(f"/t/page?cursor={cursor[:-2]}xx")
    assert r.status_code == 400 and r.json()["code"] == "invalid_cursor"


def test_garbage_cursor_rejected(client: Client) -> None:
    assert client.get("/t/page?cursor=garbage").json()["code"] == "invalid_cursor"


@pytest.mark.parametrize("limit", [0, 101, -1])
def test_limit_bounds(client: Client, limit: int) -> None:
    assert client.get(f"/t/page?limit={limit}").status_code == 422


def test_cursor_from_other_ordering_rejected() -> None:
    from apps.core.errors import AppError
    from apps.core.pagination import paginate_cursor

    make(5)
    page = paginate_cursor(AuditLog.objects.all(), ordering=["-id"], cursor=None, limit=2)
    with pytest.raises(AppError):
        paginate_cursor(
            AuditLog.objects.all(), ordering=["-created_at", "-id"], cursor=page.next_cursor
        )


def test_mixed_direction_rejected() -> None:
    from apps.core.pagination import paginate_cursor

    with pytest.raises(ValueError):
        paginate_cursor(AuditLog.objects.all(), ordering=["-created_at", "id"], cursor=None)


def test_offset_pagination() -> None:
    from apps.core.pagination import paginate_offset

    make(5)
    out = paginate_offset(AuditLog.objects.order_by("id"), limit=2, offset=1)
    assert out["total"] == 5 and len(out["items"]) == 2 and out["offset"] == 1

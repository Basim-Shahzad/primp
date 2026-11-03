import pytest
from django.test import RequestFactory

from apps.core import audit
from apps.core.models import AuditLog, ImmutableRecordError

pytestmark = pytest.mark.django_db


def test_record_stores_fields_and_ip() -> None:
    req = RequestFactory().get("/", REMOTE_ADDR="10.1.2.3")
    entry = audit.record(
        "user.suspend",
        actor_id=7,
        target_type="user",
        target_id=42,
        before={"status": "active"},
        after={"status": "suspended"},
        request=req,
    )
    entry.refresh_from_db()
    assert (entry.actor_id, entry.target_id, entry.ip_address) == (7, "42", "10.1.2.3")
    assert entry.after == {"status": "suspended"}


def test_update_blocked() -> None:
    entry = audit.record("x")
    entry.action = "y"
    with pytest.raises(ImmutableRecordError):
        entry.save()
    with pytest.raises(ImmutableRecordError):
        AuditLog.objects.filter(pk=entry.pk).update(action="z")
    assert AuditLog.objects.get(pk=entry.pk).action == "x"


def test_delete_blocked() -> None:
    entry = audit.record("x")
    with pytest.raises(ImmutableRecordError):
        entry.delete()
    with pytest.raises(ImmutableRecordError):
        AuditLog.objects.all().delete()
    with pytest.raises(ImmutableRecordError):
        AuditLog.objects.bulk_update([entry], ["action"])
    assert AuditLog.objects.count() == 1

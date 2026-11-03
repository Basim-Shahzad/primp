import json
import threading
import uuid

import pytest
from django.db import connection
from django.test import Client

from apps.bookings.factories import AppointmentFactory
from apps.payments.gateways import FakeGateway, InvalidPayload, InvalidSignature, gateway_registry
from apps.payments.models import PaymentWebhookEvent

pytestmark = pytest.mark.django_db
URL = "/api/v1/payments/webhooks/fake"


def event(appointment, type_: str = "payment.succeeded", event_id: str | None = None, **extra):  # type: ignore[no-untyped-def]
    return {
        "event_id": event_id or uuid.uuid4().hex,
        "type": type_,
        "appointment_id": str(appointment.public_id),
        "provider_ref": "fake_1",
        **extra,
    }


def send(
    payload,
    *,
    signed: bool = True,
    url: str = URL,
    raw: bytes | None = None,
    sig: str | None = None,
):  # type: ignore[no-untyped-def]
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {}
    if signed:
        headers["HTTP_X_FAKE_SIGNATURE"] = sig or FakeGateway.sign(body)
    return Client().post(url, data=body, content_type="application/json", **headers)


def test_success_updates_payment_status_and_records_event(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    a = AppointmentFactory()
    payload = event(a, event_id="evt-1")
    with run_commit():
        r = send(payload)
    assert r.status_code == 200 and r.json() == {"status": "processed"}
    a.refresh_from_db()
    assert a.payment_status == "paid"
    row = PaymentWebhookEvent.objects.get()
    assert (row.provider, row.event_id, row.event_type, row.outcome) == (
        "fake",
        "evt-1",
        "payment.succeeded",
        "processed",
    )
    assert row.processed_at is not None
    assert [n for n, _, _ in captured_events] == ["booking_payment_changed"]


@pytest.mark.parametrize(
    ("type_", "expected"),
    [("payment.failed", "failed"), ("refund.succeeded", "refunded")],
)
def test_other_event_types(type_: str, expected: str) -> None:
    a = AppointmentFactory()
    assert send(event(a, type_)).json()["status"] == "processed"
    a.refresh_from_db()
    assert a.payment_status == expected


def test_duplicate_event_processed_once(run_commit, captured_events) -> None:  # type: ignore[no-untyped-def]
    a = AppointmentFactory()
    payload = event(a, event_id="same")
    with run_commit():
        first = send(payload)
        second = send(payload)
    assert (first.json()["status"], second.json()["status"]) == ("processed", "duplicate")
    assert PaymentWebhookEvent.objects.count() == 1
    assert len(captured_events) == 1


def test_duplicate_after_later_status_does_not_revert() -> None:
    a = AppointmentFactory()
    paid = event(a, event_id="e1")
    send(paid)
    send(event(a, "refund.succeeded", event_id="e2"))
    send(paid)  # provider re-delivers the old event
    a.refresh_from_db()
    assert a.payment_status == "refunded"


@pytest.mark.django_db(transaction=True)
def test_concurrent_duplicates_processed_once() -> None:
    a = AppointmentFactory()
    payload = event(a, event_id="race")
    results: list[str] = []
    barrier = threading.Barrier(5)

    def worker() -> None:
        try:
            barrier.wait()
            results.append(send(payload).json()["status"])
        finally:
            connection.close()

    threads = [threading.Thread(target=worker) for _ in range(5)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert results.count("processed") == 1 and results.count("duplicate") == 4
    assert PaymentWebhookEvent.objects.count() == 1


def test_missing_wrong_and_tampered_signature_401() -> None:
    a = AppointmentFactory()
    payload = event(a)
    assert send(payload, signed=False).status_code == 401
    assert send(payload, sig="sha256=deadbeef").status_code == 401
    body = json.dumps(payload).encode()
    good = FakeGateway.sign(body)
    r = send(None, raw=body.replace(b"payment.succeeded", b"payment.failed"), sig=good)
    assert r.status_code == 401 and r.json()["code"] == "invalid_signature"
    a.refresh_from_db()
    assert a.payment_status == "unpaid" and PaymentWebhookEvent.objects.count() == 0


@pytest.mark.parametrize(
    "raw", [b"not json", b"[]", b"{}", b'{"event_id": "x"}', b'{"event_id":"x","type":"t"}']
)
def test_malformed_payload_400(raw: bytes) -> None:
    r = send(None, raw=raw)
    assert r.status_code == 400 and r.json()["code"] == "invalid_payload"


def test_unknown_provider_404() -> None:
    a = AppointmentFactory()
    assert send(event(a), url="/api/v1/payments/webhooks/stripe").status_code == 404


def test_unknown_appointment_and_unknown_type_are_ignored_but_recorded() -> None:
    ghost = type("A", (), {"public_id": uuid.uuid4()})()
    r = send(event(ghost, event_id="g1"))
    assert r.status_code == 200 and r.json()["status"] == "ignored"
    a = AppointmentFactory()
    r = send(event(a, "something.else", event_id="g2"))
    assert r.json()["status"] == "ignored"
    a.refresh_from_db()
    assert a.payment_status == "unpaid"
    assert set(PaymentWebhookEvent.objects.values_list("outcome", flat=True)) == {"ignored"}


def test_non_post_methods_rejected() -> None:
    assert Client().get(URL).status_code == 405


def test_fake_gateway_contract() -> None:
    gw = gateway_registry.get("fake")
    intent = gw.create_payment(appointment_id="a", amount=1500)
    assert (
        intent.status == "pending"
        and intent.provider_ref.startswith("fake_")
        and FakeGateway.created
    )
    assert gw.refund(provider_ref=intent.provider_ref, amount=1500).status == "succeeded"
    body = json.dumps(
        {"event_id": "1", "type": "payment.succeeded", "appointment_id": "x"}
    ).encode()
    ev = gw.verify_webhook({"X-Fake-Signature": FakeGateway.sign(body)}, body)
    assert (ev.event_id, ev.type, ev.appointment_id) == ("1", "payment.succeeded", "x")
    with pytest.raises(InvalidSignature):
        gw.verify_webhook({}, body)
    bad = b"{"
    with pytest.raises(InvalidPayload):
        gw.verify_webhook({"X-Fake-Signature": FakeGateway.sign(bad)}, bad)

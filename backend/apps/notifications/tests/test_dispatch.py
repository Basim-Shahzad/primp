from unittest import mock

import pytest
import requests
from django.core import mail
from django.test import override_settings

from apps.accounts.factories import make_customer
from apps.notifications import services
from apps.notifications.adapters import FakeSMS, HttpSMS, SMSDeliveryError
from apps.notifications.models import (
    Channel,
    Notification,
    NotificationStatus,
    NotificationTemplate,
)
from apps.notifications.tasks import deliver_notification
from apps.notifications.templating import (
    MissingPlaceholder,
    TemplateNotFound,
    placeholders,
    render,
    render_text,
)

pytestmark = pytest.mark.django_db

PAYLOAD = {
    "customer_name": "Sana",
    "business_name": "Glow",
    "staff_name": "Ayesha",
    "services": "Haircut",
    "date": "Tue 03 Nov",
    "time": "12:00",
    "total_price": "PKR 2,000",
    "appointment_id": "abc",
    "late_note": "",
    "reason": "price",
    "code": "123456",
    "status": "pending",
    "message": "",
    "note_suffix": "",
    "rating": "5",
}


# ---------------- templates ----------------


def test_placeholders_and_render_text() -> None:
    assert placeholders("Hi {name}, {name} {x}") == {"name", "x"}
    assert render_text("Hi {name}", {"name": "A", "unused": 1}) == "Hi A"
    assert render_text("", {}) == ""


def test_missing_placeholder_fails_loudly() -> None:
    with pytest.raises(MissingPlaceholder, match="name"):
        render_text("Hi {name}", {})


def test_all_required_template_keys_seeded() -> None:
    keys = set(NotificationTemplate.objects.values_list("key", flat=True))
    required = {
        "otp",
        "booking_received",
        "booking_confirmed",
        "booking_rescheduled",
        "booking_cancelled_by_business",
        "reminder_24h",
        "reminder_2h",
        "review_prompt",
    }
    assert required <= keys


def test_every_seeded_template_renders_with_a_full_payload() -> None:
    """Guards against typos in placeholders: each template must render with the standard payload."""
    templates = list(NotificationTemplate.objects.all())
    assert len(templates) > 20
    for t in templates:
        subject, body = render(t.key, t.channel, PAYLOAD)
        assert body and "{" not in body and "{" not in subject, t


def test_template_lookup_falls_back_to_english_and_missing_raises() -> None:
    assert render("booking_confirmed", "sms", PAYLOAD, language="ur")[1]
    with pytest.raises(TemplateNotFound):
        render("nope", "sms", PAYLOAD)
    NotificationTemplate.objects.filter(key="otp").update(is_active=False)
    with pytest.raises(TemplateNotFound):
        render("otp", "sms", PAYLOAD)


# ---------------- channel selection ----------------


def test_notify_prefers_sms_plus_in_app(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer(phone="+923001112222", email="a@example.com")
    with run_commit():
        created = services.notify(user.pk, "booking_confirmed", PAYLOAD)
    assert [(n.channel, n.recipient) for n in created] == [
        ("sms", "+923001112222"),
        ("in_app", str(user.pk)),
    ]
    assert len(FakeSMS.outbox) == 1 and "Glow" in FakeSMS.outbox[0].body
    assert {n.status for n in Notification.objects.all()} == {"sent", "delivered"}
    assert mail.outbox == []


def test_notify_uses_email_when_no_phone(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer(phone=None, email="only@example.com")
    with run_commit():
        created = services.notify(user.pk, "booking_confirmed", PAYLOAD)
    assert [n.channel for n in created] == ["email", "in_app"]
    assert mail.outbox[0].to == ["only@example.com"] and "Glow" in mail.outbox[0].body
    assert mail.outbox[0].subject == "Your booking is confirmed"


def test_notify_without_sms_flag_and_unknown_user(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer(phone="+923001112222", email="a@example.com")
    with run_commit():
        created = services.notify(user.pk, "new_booking", PAYLOAD, include_sms=False)
    assert [n.channel for n in created] == ["email", "in_app"] and FakeSMS.outbox == []
    assert services.notify(999999, "booking_confirmed", PAYLOAD) == []


def test_in_app_notification_is_delivered_immediately(run_commit) -> None:  # type: ignore[no-untyped-def]
    user = make_customer(phone=None, email="a@example.com")
    with run_commit():
        services.notify(user.pk, "review_prompt", PAYLOAD)
    n = Notification.objects.get(channel="in_app")
    assert n.status == NotificationStatus.DELIVERED and n.sent_at and n.attempts == 1


# ---------------- delivery attempts ----------------


def queued(channel: str = "sms", key: str = "booking_confirmed", **kw):  # type: ignore[no-untyped-def]
    user = kw.pop("user", None) or make_customer(phone="+923001112222", email="fb@example.com")
    recipient = {"sms": user.phone, "email": user.email}.get(channel, str(user.pk))
    return Notification.objects.create(
        user=user, recipient=recipient, channel=channel, template_key=key, payload=PAYLOAD, **kw
    )


def test_success_records_provider_id_and_is_idempotent() -> None:
    n = queued()
    assert services.attempt_delivery(n.pk) == services.SENT
    n.refresh_from_db()
    assert (
        n.status == "sent" and n.provider_message_id == "fake-1" and n.attempts == 1 and n.sent_at
    )
    assert services.attempt_delivery(n.pk) == services.SKIPPED  # running twice has no double effect
    assert len(FakeSMS.outbox) == 1
    n.refresh_from_db()
    assert n.attempts == 1


def test_unknown_notification_skipped() -> None:
    assert services.attempt_delivery(999999) == services.SKIPPED


def test_transient_failure_retries_then_succeeds() -> None:
    n = queued()
    FakeSMS.fail_with = SMSDeliveryError("gateway timeout")
    assert services.attempt_delivery(n.pk) == services.RETRY
    n.refresh_from_db()
    assert n.status == "queued" and n.attempts == 1 and "gateway timeout" in n.last_error
    FakeSMS.fail_with = None
    assert services.attempt_delivery(n.pk) == services.SENT
    n.refresh_from_db()
    assert n.status == "sent" and n.attempts == 2 and n.last_error == ""


def test_transient_failures_exhaust_attempts_then_fall_back_to_email(run_commit) -> None:  # type: ignore[no-untyped-def]
    n = queued()
    FakeSMS.fail_with = SMSDeliveryError("down")
    outcomes = [services.attempt_delivery(n.pk) for _ in range(services.MAX_ATTEMPTS)]
    assert outcomes == [services.RETRY] * (services.MAX_ATTEMPTS - 1) + [services.FALLBACK]
    n.refresh_from_db()
    assert (
        n.status == "failed"
        and n.attempts == services.MAX_ATTEMPTS
        and "fell back to email" in n.last_error
    )


def test_permanent_sms_failure_falls_back_immediately(run_commit) -> None:  # type: ignore[no-untyped-def]
    n = queued()
    FakeSMS.fail_with = SMSDeliveryError("number invalid", permanent=True)
    with run_commit():
        assert services.attempt_delivery(n.pk) == services.FALLBACK
    n.refresh_from_db()
    assert n.status == "failed" and n.attempts == 1
    fb = Notification.objects.get(fallback_of=n)
    assert (
        fb.channel == "email"
        and fb.recipient == "fb@example.com"
        and fb.template_key == n.template_key
    )
    assert fb.status == "sent" and mail.outbox[0].to == ["fb@example.com"]


def test_permanent_failure_without_email_marks_failed_for_admin_review() -> None:
    user = make_customer(phone="+923001112222", email=None)
    n = queued(user=user)
    FakeSMS.fail_with = SMSDeliveryError("rejected", permanent=True)
    assert services.attempt_delivery(n.pk) == services.FAILED
    n.refresh_from_db()
    assert n.status == "failed" and "no email" in n.last_error
    assert not Notification.objects.filter(fallback_of=n).exists()


def test_email_failures_retry_then_fail_without_fallback() -> None:
    n = queued("email")
    with mock.patch("apps.notifications.services.send_mail", side_effect=OSError("smtp down")):
        outcomes = [services.attempt_delivery(n.pk) for _ in range(services.MAX_ATTEMPTS)]
    assert outcomes == [services.RETRY] * (services.MAX_ATTEMPTS - 1) + [services.FAILED]
    n.refresh_from_db()
    assert (
        n.status == "failed" and "smtp down" in n.last_error and n.attempts == services.MAX_ATTEMPTS
    )
    assert not Notification.objects.filter(fallback_of=n).exists()


def test_unexpected_sms_adapter_error_is_retried() -> None:
    n = queued()
    with mock.patch(
        "apps.notifications.services.get_sms_adapter", side_effect=RuntimeError("boom")
    ):
        assert services.attempt_delivery(n.pk) == services.RETRY


def test_missing_placeholder_raises_in_tests_and_records_failure() -> None:
    n = queued()
    n.payload = {"customer_name": "only this"}
    n.save()
    with pytest.raises(MissingPlaceholder):
        services.attempt_delivery(n.pk)
    n.refresh_from_db()
    assert n.status == "failed" and "missing placeholders" in n.last_error


@override_settings(NOTIFICATIONS_RAISE_ON_RENDER_ERROR=False)
def test_render_error_in_production_mode_marks_failed_without_raising() -> None:
    n = queued()
    n.payload = {}
    n.save()
    assert services.attempt_delivery(n.pk) == services.FAILED
    n.refresh_from_db()
    assert n.status == "failed" and FakeSMS.outbox == []


def test_unsupported_channel_marked_failed() -> None:
    n = queued("push")
    NotificationTemplate.objects.create(
        key="booking_confirmed", channel="push", body="hi {business_name}"
    )
    assert services.attempt_delivery(n.pk) == services.FAILED


# ---------------- celery task wrapper ----------------


def test_backoff_is_exponential() -> None:
    assert [services.backoff_seconds(r) for r in range(3)] == [30, 60, 120]


@pytest.mark.parametrize(
    ("key", "queue"),
    [
        ("otp", "critical"),
        ("booking_confirmed", "critical"),
        ("reminder_2h", "default"),
        ("review_prompt", "bulk"),
    ],
)
def test_queue_routing(key: str, queue: str) -> None:
    assert services.queue_for(key) == queue


def test_task_retries_with_backoff_on_transient_failure() -> None:
    n = queued()
    FakeSMS.fail_with = SMSDeliveryError("down")
    with (
        mock.patch.object(
            deliver_notification, "retry", side_effect=RuntimeError("retry!")
        ) as retry,
        pytest.raises(RuntimeError, match="retry!"),
    ):
        deliver_notification.run(n.pk)
    assert retry.call_args.kwargs == {"countdown": 30, "queue": "critical"}


def test_task_retry_config_allows_three_retries() -> None:
    assert deliver_notification.max_retries == 3


def test_task_does_not_retry_on_success_or_final_failure() -> None:
    n = queued()
    assert deliver_notification.run(n.pk) == "sent"
    with mock.patch.object(deliver_notification, "retry") as retry:
        deliver_notification.run(n.pk)
        retry.assert_not_called()


def test_confirmation_task_enqueued_on_commit_not_before(
    django_capture_on_commit_callbacks,
) -> None:  # type: ignore[no-untyped-def]
    user = make_customer(phone="+923001112222")
    with django_capture_on_commit_callbacks(execute=False) as callbacks:
        services.notify(user.pk, "booking_confirmed", PAYLOAD)
        assert FakeSMS.outbox == []
    assert len(callbacks) == 2  # sms + in-app
    for cb in callbacks:
        cb()
    assert len(FakeSMS.outbox) == 1


# ---------------- queue helpers used elsewhere ----------------


def test_queue_sms_and_email_helpers(run_commit) -> None:  # type: ignore[no-untyped-def]
    with run_commit():
        services.queue_sms("+923001112222", "hello")
        services.queue_email("a@example.com", "Subj", "Body")
    assert FakeSMS.outbox[0].body == "hello" and mail.outbox[0].subject == "Subj"


def test_render_sms_body_for_otp_is_not_logged() -> None:
    body = services.render_sms_body("otp", {"code": "424242"})
    assert "424242" in body and Notification.objects.count() == 0


def test_channel_constants() -> None:
    assert {c.value for c in Channel} == {"sms", "email", "push", "in_app"}


# ---------------- HTTP SMS provider skeleton ----------------


@override_settings(SMS_API_URL="https://sms.test/send", SMS_API_KEY="k", SMS_SENDER_ID="Primp")
def test_http_sms_success_and_request_shape() -> None:
    response = mock.Mock(status_code=200)
    response.json.return_value = {"id": "msg-9"}
    with mock.patch("apps.notifications.adapters.requests.post", return_value=response) as post:
        assert HttpSMS().send("+923001112222", "hi") == "msg-9"
    assert post.call_args.kwargs["json"] == {"to": "+923001112222", "from": "Primp", "text": "hi"}
    assert post.call_args.kwargs["headers"] == {"Authorization": "Bearer k"}
    assert post.call_args.kwargs["timeout"] == 5


@override_settings(SMS_API_URL="https://sms.test/send")
@pytest.mark.parametrize(
    ("status", "permanent"), [(500, False), (503, False), (400, True), (422, True)]
)
def test_http_sms_status_classification(status: int, permanent: bool) -> None:
    with (
        mock.patch(
            "apps.notifications.adapters.requests.post", return_value=mock.Mock(status_code=status)
        ),
        pytest.raises(SMSDeliveryError) as exc,
    ):
        HttpSMS().send("+923001112222", "hi")
    assert exc.value.permanent is permanent


@override_settings(SMS_API_URL="https://sms.test/send")
def test_http_sms_network_error_is_transient_and_bad_json_tolerated() -> None:
    with (
        mock.patch(
            "apps.notifications.adapters.requests.post", side_effect=requests.Timeout("slow")
        ),
        pytest.raises(SMSDeliveryError) as exc,
    ):
        HttpSMS().send("+923001112222", "hi")
    assert exc.value.permanent is False
    bad = mock.Mock(status_code=200)
    bad.json.side_effect = ValueError
    with mock.patch("apps.notifications.adapters.requests.post", return_value=bad):
        assert HttpSMS().send("+923001112222", "hi") == ""


@override_settings(SMS_API_URL="")
def test_http_sms_unconfigured_is_permanent_failure() -> None:
    with pytest.raises(SMSDeliveryError) as exc:
        HttpSMS().send("+923001112222", "hi")
    assert exc.value.permanent is True


def test_console_sms_and_registry(caplog: pytest.LogCaptureFixture) -> None:
    from apps.notifications.adapters import ConsoleSMS, sms_registry

    caplog.set_level("INFO")
    assert ConsoleSMS().send("+923001112222", "x") == "console"
    assert set(sms_registry.names()) == {"console", "fake", "http"}

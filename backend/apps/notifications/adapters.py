"""SMS adapters. The registry picks the implementation from settings.SMS_ADAPTER."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

import requests
from django.conf import settings

from apps.core.adapters import AdapterRegistry
from apps.core.redact import mask_phone

logger = logging.getLogger(__name__)


class SMSDeliveryError(Exception):
    """Raised by adapters. `permanent` errors are never retried."""

    def __init__(self, message: str, permanent: bool = False) -> None:
        super().__init__(message)
        self.permanent = permanent


class SMSAdapter(ABC):
    @abstractmethod
    def send(self, to: str, body: str) -> str:
        """Send an SMS and return the provider message id."""


sms_registry: AdapterRegistry[SMSAdapter] = AdapterRegistry("sms", "SMS_ADAPTER")


@sms_registry.register("console")
class ConsoleSMS(SMSAdapter):
    """Development adapter: writes the message to the log instead of sending it."""

    def send(self, to: str, body: str) -> str:
        # development helper: the body (with OTP codes) is only shown when DEBUG is on
        extra = {"to": mask_phone(to), **({"body": body} if settings.DEBUG else {})}
        logger.info("SMS (console adapter)", extra=extra)
        return "console"


@dataclass
class SentSMS:
    to: str
    body: str


@sms_registry.register("fake")
class FakeSMS(SMSAdapter):
    """Test adapter that records messages in memory and can be told to fail."""

    outbox: ClassVar[list[SentSMS]] = []
    fail_with: ClassVar[SMSDeliveryError | None] = None

    def send(self, to: str, body: str) -> str:
        if FakeSMS.fail_with is not None:
            raise FakeSMS.fail_with
        FakeSMS.outbox.append(SentSMS(to, body))
        return f"fake-{len(FakeSMS.outbox)}"

    @classmethod
    def reset(cls) -> None:
        cls.outbox.clear()
        cls.fail_with = None


@sms_registry.register("http")
class HttpSMS(SMSAdapter):
    """Generic JSON-over-HTTP SMS provider skeleton, enabled with SMS_ADAPTER=http.

    Needs SMS_API_URL, SMS_API_KEY and SMS_SENDER_ID. 5xx/timeouts are transient (retried),
    4xx are permanent (the dispatcher then falls back to email).
    """

    timeout = 5

    def send(self, to: str, body: str) -> str:
        url = settings.SMS_API_URL
        if not url:
            raise SMSDeliveryError("SMS provider is not configured", permanent=True)
        try:
            response = requests.post(
                url,
                json={"to": to, "from": settings.SMS_SENDER_ID, "text": body},
                headers={"Authorization": f"Bearer {settings.SMS_API_KEY}"},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise SMSDeliveryError(f"provider unreachable: {exc}") from exc
        if response.status_code >= 500:
            raise SMSDeliveryError(f"provider error {response.status_code}")
        if response.status_code >= 400:
            raise SMSDeliveryError(
                f"provider rejected message ({response.status_code})", permanent=True
            )
        try:
            return str(response.json().get("id", ""))
        except ValueError:
            return ""


def get_sms_adapter() -> SMSAdapter:
    return sms_registry.get()

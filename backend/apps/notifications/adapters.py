"""SMS adapters. The registry picks the implementation from settings.SMS_ADAPTER."""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import ClassVar

from apps.core.adapters import AdapterRegistry

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
        logger.info("SMS (console adapter)", extra={"to": to, "body": body})
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


def get_sms_adapter() -> SMSAdapter:
    return sms_registry.get()

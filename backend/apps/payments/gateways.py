"""Payment gateway interface and adapters (real JazzCash/Easypaisa/Stripe are in FOLLOW_UPS)."""

import hashlib
import hmac
import json
from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from typing import ClassVar

from django.conf import settings

from apps.core.adapters import AdapterRegistry


class InvalidSignature(Exception):
    pass


class InvalidPayload(Exception):
    pass


@dataclass(frozen=True)
class PaymentIntent:
    provider_ref: str
    status: str
    redirect_url: str | None = None


@dataclass(frozen=True)
class WebhookEvent:
    event_id: str
    type: str
    appointment_id: str
    provider_ref: str = ""


@dataclass(frozen=True)
class RefundResult:
    refund_ref: str
    status: str


class PaymentGateway(ABC):
    @abstractmethod
    def create_payment(
        self, *, appointment_id: str, amount: int, currency: str = "PKR"
    ) -> PaymentIntent: ...

    @abstractmethod
    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent:
        """Authenticate and parse a webhook. Raises InvalidSignature / InvalidPayload."""

    @abstractmethod
    def refund(self, *, provider_ref: str, amount: int) -> RefundResult: ...


gateway_registry: AdapterRegistry[PaymentGateway] = AdapterRegistry(
    "payment gateway", "PAYMENT_GATEWAY"
)


@gateway_registry.register("fake")
class FakeGateway(PaymentGateway):
    SIGNATURE_HEADER: ClassVar[str] = "X-Fake-Signature"
    created: ClassVar[list[PaymentIntent]] = []
    refunds: ClassVar[list[RefundResult]] = []

    @staticmethod
    def sign(body: bytes) -> str:
        mac = hmac.new(settings.PAYMENT_WEBHOOK_SECRET.encode(), body, hashlib.sha256)
        return "sha256=" + mac.hexdigest()

    def create_payment(
        self, *, appointment_id: str, amount: int, currency: str = "PKR"
    ) -> PaymentIntent:
        intent = PaymentIntent(
            f"fake_{len(self.created) + 1}", "pending", "https://pay.fake/checkout"
        )
        self.created.append(intent)
        return intent

    def verify_webhook(self, headers: Mapping[str, str], body: bytes) -> WebhookEvent:
        given = headers.get(self.SIGNATURE_HEADER, "")
        if not given or not hmac.compare_digest(given, self.sign(body)):
            raise InvalidSignature
        try:
            data = json.loads(body)
            return WebhookEvent(
                event_id=str(data["event_id"]),
                type=str(data["type"]),
                appointment_id=str(data["appointment_id"]),
                provider_ref=str(data.get("provider_ref", "")),
            )
        except (ValueError, KeyError, TypeError) as exc:
            raise InvalidPayload from exc

    def refund(self, *, provider_ref: str, amount: int) -> RefundResult:
        result = RefundResult(f"refund_{provider_ref}", "succeeded")
        self.refunds.append(result)
        return result

    @classmethod
    def reset(cls) -> None:
        cls.created.clear()
        cls.refunds.clear()

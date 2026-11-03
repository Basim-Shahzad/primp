"""Scrub personal data and secrets from log text."""

import re

_EMAIL = re.compile(r"[A-Za-z0-9._%+\-']+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_PHONE = re.compile(r"(?<![\w])(?:\+?92|0)?3\d{2}[\s\-]?\d{7}(?!\d)")
_JWT = re.compile(r"eyJ[\w\-]+\.[\w\-]+\.[\w\-]+")
_BEARER = re.compile(r"(?i)\bbearer\s+[\w\-.~+/=]+")
_HOLD_PATH = re.compile(r"(/hold/)[^/\s'\"?]+")
_SENSITIVE_QUERY = re.compile(
    r"(?i)([?&](?:lat|lng|token|code|password|phone|email|cursor|id_token)=)[^&\s'\"]*"
)
_OTP_TEXT = re.compile(r"(?i)(code is )\d{4,8}")


def redact(text: str) -> str:
    text = _JWT.sub("[jwt]", text)
    text = _BEARER.sub("Bearer [token]", text)
    text = _HOLD_PATH.sub(r"\1[token]", text)
    text = _SENSITIVE_QUERY.sub(r"\1[redacted]", text)
    text = _OTP_TEXT.sub(r"\1[code]", text)
    text = _EMAIL.sub("[email]", text)
    return _PHONE.sub("[phone]", text)


def mask_phone(phone: str) -> str:
    """+923001234567 -> +92300*****67 (for the few places that must identify a number)."""
    if len(phone) < 8:
        return "[phone]"
    return phone[:6] + "*" * (len(phone) - 8) + phone[-2:]

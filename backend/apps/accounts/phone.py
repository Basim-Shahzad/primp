import re

from django.utils.translation import gettext_lazy as _

_NON_DIGIT = re.compile(r"[\s\-().]")


def normalize_pk_mobile(raw: str) -> str:
    """Normalise a Pakistani mobile number to +92XXXXXXXXXX or raise ValueError."""
    cleaned = _NON_DIGIT.sub("", raw.strip())
    digits = cleaned[1:] if cleaned.startswith("+") else cleaned
    if not digits.isdigit():
        raise ValueError(str(_("Enter a valid Pakistani mobile number.")))
    if digits.startswith("0092"):
        digits = digits[4:]
    elif digits.startswith("92") and len(digits) == 12:
        digits = digits[2:]
    elif digits.startswith("0") and len(digits) == 11:
        digits = digits[1:]
    if not re.fullmatch(r"3\d{9}", digits):
        raise ValueError(str(_("Enter a valid Pakistani mobile number.")))
    return f"+92{digits}"

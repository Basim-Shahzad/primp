"""Reusable input types for provider payloads."""

import re
from typing import Annotated

from django.utils.translation import gettext_lazy as _
from pydantic import AfterValidator, Field, StringConstraints

_E164 = re.compile(r"^\+[1-9]\d{7,14}$")
_STRIP = re.compile(r"[\s\-().]")


def to_e164(value: str) -> str:
    cleaned = _STRIP.sub("", value.strip())
    if cleaned == "":
        return ""
    if not _E164.match(cleaned):
        raise ValueError(str(_("Enter the number in international format, e.g. +923001234567.")))
    return cleaned


E164 = Annotated[str, AfterValidator(to_e164), Field(max_length=25)]
Text150 = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=150)]
Description = Annotated[str, StringConstraints(strip_whitespace=True, max_length=2000)]
Address = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=255)]

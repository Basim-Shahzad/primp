import pytest

from apps.accounts.phone import normalize_pk_mobile


@pytest.mark.parametrize(
    "raw",
    [
        "03001234567",
        "3001234567",
        "+923001234567",
        "923001234567",
        "00923001234567",
        "0300-1234567",
        "+92 300 1234567",
        " (0300) 123 4567 ",
    ],
)
def test_normalises_to_e164(raw: str) -> None:
    assert normalize_pk_mobile(raw) == "+923001234567"


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "abc",
        "0300123456",
        "030012345678",
        "+913001234567",
        "02112345678",
        "+92300123456a",
        "+9230012345678",
        "1234567890",
    ],
)
def test_rejects_invalid(raw: str) -> None:
    with pytest.raises(ValueError):
        normalize_pk_mobile(raw)

"""Identifier validators/normalisers backed by python-stdnum."""

from __future__ import annotations

from stdnum import iban, isin
from stdnum.exceptions import ValidationError


def normalise_isin(value: str) -> str | None:
    try:
        return isin.validate(value.replace(" ", ""))
    except ValidationError:
        return None


def normalise_iban(value: str) -> str | None:
    try:
        return iban.validate(value.replace(" ", ""))
    except ValidationError:
        return None

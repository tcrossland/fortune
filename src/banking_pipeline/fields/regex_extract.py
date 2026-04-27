"""Regex-based field extraction.

Kept intentionally generic; real precision comes from per-template rules under
``banking_pipeline.templates``. This module is the safety net that runs when no
template matches.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import dateparser

from banking_pipeline.fields.validators import normalise_iban, normalise_isin
from banking_pipeline.models import RawDocument, Transaction

_ISIN_RE = re.compile(r"\b([A-Z]{2}[A-Z0-9]{9}[0-9])\b")
_IBAN_RE = re.compile(r"\b([A-Z]{2}\d{2}[A-Z0-9]{10,30})\b")
_AMOUNT_RE = re.compile(
    r"(?P<currency>EUR|USD|GBP|CHF|JPY|\$|€|£)\s?(?P<amount>-?\d{1,3}(?:[.,\s]\d{3})*(?:[.,]\d{2})?)",
    re.I,
)
_DATE_HINT_RE = re.compile(
    r"\b(?:trade|value|settlement|posting|payment|transaction)\s*date[:\s]+"
    r"(?P<date>[0-9A-Za-z\-/., ]{6,20})",
    re.I,
)

_CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP"}


@dataclass
class RegexExtractor:
    """Low-confidence, best-effort field extractor."""

    def extract(self, doc: RawDocument) -> tuple[list[Transaction], float]:
        isin_match = _ISIN_RE.search(doc.text)
        iban_match = _IBAN_RE.search(doc.text)
        amount_match = _AMOUNT_RE.search(doc.text)
        date_match = _DATE_HINT_RE.search(doc.text)

        if not (amount_match and date_match):
            return [], 0.0

        amount_str = amount_match.group("amount").replace(" ", "").replace(",", ".")
        # If both '.' and ',' were present, the rightmost separator is the decimal mark;
        # strip the thousands grouping on the left of it.
        if amount_str.count(".") > 1:
            head, _, tail = amount_str.rpartition(".")
            amount_str = head.replace(".", "") + "." + tail
        try:
            amount = Decimal(amount_str)
        except InvalidOperation:
            return [], 0.0

        currency_raw = amount_match.group("currency").upper()
        currency = _CURRENCY_SYMBOLS.get(currency_raw, currency_raw)

        parsed_date = dateparser.parse(date_match.group("date"))
        if parsed_date is None:
            return [], 0.0

        tx = Transaction(
            trade_date=parsed_date.date(),
            narration=_first_line(doc.text),
            currency=currency,
            amount=amount,
            isin=normalise_isin(isin_match.group(1)) if isin_match else None,
            account_number=normalise_iban(iban_match.group(1)) if iban_match else None,
            source_path=doc.path,
        )
        # Confidence heuristic: full set of fields = high; missing pieces = lower.
        confidence = 0.4 + 0.15 * sum(
            v is not None for v in (isin_match, iban_match, date_match, amount_match)
        )
        return [tx], min(confidence, 0.95)


def _first_line(text: str) -> str:
    for line in text.splitlines():
        stripped = line.strip()
        if stripped:
            return stripped[:140]
    return ""

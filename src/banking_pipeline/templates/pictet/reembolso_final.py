"""``pictet.reembolso_final.v1`` — Spanish-locale structured-product maturity payout.

Spanish counterpart to :mod:`final_redemption`. Pictet emits this document
under ``HECHOS RELEVANTES / REEMBOLSO / Reembolso final`` when a
structured product (PWM Pictet Equity Certificate, etc.) reaches maturity
and the issuer pays the holder out in cash.

Field-label divergences from the regular :mod:`reembolso` advice
----------------------------------------------------------------
This is a security-event document, not a stock-exchange trade, so the
trade-advice labels are absent or renamed:

  - No ``Tipo de operación`` / ``Plaza bursátil`` / ``Fecha de la orden``.
  - Quantity is ``Cantidad`` (not ``Cantidad ejecutada``) and is printed
    signed negative — units leaving the portfolio.
  - Price is ``Precio de rembolso`` (note Pictet's apparent typo: missing
    the second ``e``); the helper's regex tolerates both spellings.
  - The portfolio block has two sub-sections: ``CANTIDAD DETENIDA en la
    cartera`` (held quantity, the units being redeemed) and ``SALIDA de
    la cartera`` (the same units leaving). The ``EFECTO CASH`` block
    carries a real portfolio identifier — distinct from switches, where
    the marker has no portfolio because no cash actually moves.

Narration is composed from the ``Reembolso - <fund>`` security-event
line that Pictet prints near the price block, mirroring the EN
``Redemption - <fund>`` form the existing :mod:`final_redemption`
extracts via :func:`find_subject_line`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_field,
    find_subject_line,
    find_transaction_number,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
    resolve_isin,
)

# ``Reembolso final`` is the load-bearing tell that separates this advice
# type from the regular ``Reembolso`` (fund redemption). Anchored to a
# full line via ``^...$`` + ``re.M`` so the section banner ``REEMBOLSO``
# (uppercase, standalone) on its own doesn't match.
_REEMBOLSO_FINAL_TITLE_RE = re.compile(r"^Reembolso\s+final\s*$", re.M | re.I)

# Pictet's apparent typo: ``Precio de rembolso`` (missing the second
# ``e`` after the first one). Tolerant of both spellings so corrected
# documents continue to parse. The amount-field regex below is built
# from the matched label, so we can't reuse ``find_amount_field``
# directly here.
_PRECIO_REEMBOLSO_RE = re.compile(
    r"^Precio\s+de\s+re?embolso\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$",
    re.M | re.I,
)


@dataclass
class PictetReembolsoFinalTemplate:
    template_id: str = "pictet.reembolso_final.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _REEMBOLSO_FINAL_TITLE_RE.search(text):
            return []

        # ``Precio de rembolso`` (with optional second ``e``) is the
        # load-bearing distinguishing field; bail if absent rather than
        # producing a transaction with possibly-wrong-meaning fields.
        price_match = _PRECIO_REEMBOLSO_RE.search(text)
        if price_match is None:
            return []
        security_currency = price_match.group(1)
        price = parse_pictet_amount(price_match.group(2))

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, ES_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)
        # Quantity uses ``Cantidad`` (not ``Cantidad ejecutada``) — printed
        # signed negative for the units leaving the portfolio.
        quantity_raw = find_field(text, "Cantidad")

        subject = find_subject_line(text, "Reembolso")
        narration = (
            f"Reembolso - {subject}"
            if subject
            else "Pictet reembolso final"
        )[:140]

        return [
            Transaction(
                trade_date=parse_pictet_date(trade_date_raw),
                settlement_date=(
                    parse_pictet_date(value_date_raw) if value_date_raw else None
                ),
                booking_date=(
                    parse_pictet_date(booking_date_raw)
                    if booking_date_raw
                    else None
                ),
                narration=narration,
                title="Reembolso final",
                currency=currency,
                amount=amount,
                isin=resolve_isin(text),
                quantity=parse_pictet_amount(quantity_raw) if quantity_raw else None,
                price=price,
                security_currency=security_currency,
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]

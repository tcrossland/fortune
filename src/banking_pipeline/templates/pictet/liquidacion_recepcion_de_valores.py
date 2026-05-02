"""``pictet.liquidacion_recepcion_de_valores.v1`` — securities transfer-in.

Pictet emits this document under ``LIQUIDACIÓN / RECEPCIÓN DE VALORES
(GRATUITA)`` as the actual booking advice for a free-of-payment
securities transfer from an external custodian (the paired
``LIQUIDACION_AVISO_PREVIO_RECEPCION`` advice announces the upcoming
arrival; this advice books one of the announced lots once it
physically arrives).

The document carries:

  - One ``ENTRADA en la cartera`` block per booked lot — fund name,
    quantity, and ISIN. Most fixtures show a single lot, but the
    aviso-previo can announce multiple and the recepcion booking
    advice fires once per lot.
  - A ``Transferencia`` section establishing the cost basis at the
    transfer's market value: ``Valor de mercado <CCY> <amount>``
    (source-custodian currency), ``Tipo de cambio (<src>/<dst>)
    <rate>`` (FX bridge), and ``Estimacion de transferencia <CCY>
    <amount>`` (the EUR equivalent that becomes the position's
    beancount cost basis).
  - A zero-amount ``EFECTO CASH`` block — the receipt is gratuita
    (free of payment), so no cash leg moves.

Renders through a dedicated :mod:`banking_pipeline.writer.builders.transfer_in`
builder rather than the regular security-trade path: the asset leg
uses beancount's total-cost ``{{<total> <ccy>, <lot_date>}}`` form
(no per-unit price quoted on the Pictet advice — the document
prints the total transfer value, not a unit cost), and the offset
posting lands on ``Equity:<prefix>:<portfolio>:Transfers`` instead
of an ``Assets:...`` cash account because there's no cash leg to
balance against.

Sign convention
---------------
The transferred-in position is positive (cash-equivalent flowing
into the asset account). The Equity:Transfers leg is negative
(value leaves the equity bucket and lands as an asset). The
extractor stores both signs as printed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_field,
    find_transaction_number,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
    resolve_isin,
)

# Title — load-bearing tell. ``GRATUITA`` distinguishes from any
# future ``RECEPCIÓN DE VALORES (CONTRA PAGO)`` or similar paid-
# transfer variants that would carry a non-zero cash leg and need a
# different builder.
_RECEPCION_TITLE_RE = re.compile(
    r"^RECEPCI[OÓ]N\s+DE\s+VALORES\s+\(GRATUITA\)\s*$", re.M | re.I
)

# ``Estimacion de transferencia <CCY> <amount>`` — the cost basis
# total in EUR. Pictet writes this without an accent on
# "Estimacion" in the source fixture; the optional accent keeps the
# regex tolerant of accented variants without forcing the issue.
# This is the most reliable cost-basis source on the document
# (``Valor de mercado`` is in the source-custodian currency, which
# we'd then need to convert; the FX rate is also printed but the
# resulting EUR figure may round differently from Pictet's own
# computation).
_ESTIMACION_RE = re.compile(
    r"^Estimaci[oó]n\s+de\s+transferencia\s+([A-Z]{3})\s+"
    r"(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$",
    re.M,
)

# ``Fecha`` line inside the Transferencia block — the cost-basis
# lot date. Distinct from the document-level ``Fecha de transacción``
# (they happen to coincide in the available fixture but are
# semantically different fields, and Pictet may diverge them on a
# transfer that's announced ahead of physical arrival).
_TRANSFERENCIA_FECHA_RE = re.compile(
    r"^Transferencia\s*\n\s*Fecha\s+(\d{2}\.\d{2}\.\d{4})", re.M
)


# Tail of the fund-name line: ``<fund name> <quantity>``. We strip
# the trailing Swiss-formatted number to recover both halves
# separately.
_QUANTITY_TAIL_RE = re.compile(
    r"\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$"
)
# ``ENTRADA en la cartera <portfolio>`` — same shape as
# :func:`find_switch_fund_name` accepts. We look up the line *after*
# this header to extract fund name + quantity.
_ENTRADA_RE = re.compile(r"^ENTRADA\s*en\s+la\s+cartera\b", re.I | re.M)


def _parse_first_entrada(text: str) -> tuple[str | None, Decimal | None]:
    """Return ``(fund_name, quantity)`` from the first ENTRADA block.

    The aviso_previo advice can announce multiple lots, but the
    recepcion always books a single lot per advice — the first
    ENTRADA block is the relevant one. Subsequent blocks (if any)
    would be informational only and would also produce their own
    paired recepcion advices.
    """

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if _ENTRADA_RE.match(line.strip()):
            if i + 1 >= len(lines):
                return None, None
            fund_line = lines[i + 1].strip()
            m = _QUANTITY_TAIL_RE.search(fund_line)
            if m:
                qty = parse_pictet_amount(m.group(1))
                fund_name = _QUANTITY_TAIL_RE.sub("", fund_line).strip()
                return (fund_name or None), qty
            return (fund_line or None), None
    return None, None


@dataclass
class PictetLiquidacionRecepcionDeValoresTemplate:
    template_id: str = "pictet.liquidacion_recepcion_de_valores.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _RECEPCION_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        # Cost-basis total — without it we can't build the
        # ``{{<total> <ccy>, <date>}}`` annotation, so the document
        # is unusable for this builder.
        estimacion_match = _ESTIMACION_RE.search(text)
        if estimacion_match is None:
            return []
        cost_currency = estimacion_match.group(1)
        cost_total = parse_pictet_amount(estimacion_match.group(2))

        fund_name, quantity = _parse_first_entrada(text)
        if quantity is None:
            return []

        isin = resolve_isin(text)
        if isin is None:
            return []

        # Lot date for the cost basis — prefer the explicit
        # ``Transferencia / Fecha`` line (the date the position was
        # marked at market value), fall back to the document trade
        # date when missing. The lot date is what beancount uses to
        # tag the inventory; downstream FIFO/LIFO reporting depends
        # on it being the actual transfer date, not Pictet's
        # internal booking date.
        transfer_fecha_match = _TRANSFERENCIA_FECHA_RE.search(text)
        trade_date = (
            parse_pictet_date(transfer_fecha_match.group(1))
            if transfer_fecha_match
            else parse_pictet_date(trade_date_raw)
        )

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)

        # Synthesised narration — the document carries no verb-led
        # headline. Use the fund name when present; fall back to a
        # generic label otherwise.
        narration = (fund_name or "Recepción de valores")[:140]

        # Sign convention: the asset side carries +quantity (units
        # arriving in the portfolio); we store the cost-basis total
        # as a negative ``amount`` so the writer can render the
        # Equity offset leg with that signed value directly. The
        # Equity:Transfers account is debited (-) by the same
        # amount the asset is credited (+) — value moves from
        # equity into asset.
        return [
            Transaction(
                trade_date=trade_date,
                settlement_date=(
                    parse_pictet_date(value_date_raw) if value_date_raw else None
                ),
                booking_date=(
                    parse_pictet_date(booking_date_raw)
                    if booking_date_raw
                    else None
                ),
                narration=narration,
                title="Recepción de valores (gratuita)",
                currency=cost_currency,
                amount=-cost_total,
                isin=isin,
                quantity=quantity,
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]

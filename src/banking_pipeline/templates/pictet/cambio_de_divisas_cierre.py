"""``pictet.cambio_de_divisas_cierre.v1`` — ES FX forward settlement advice.

Spanish counterpart to :mod:`settle_fx_forward`. Pictet's Madrid
branch emits this document under ``MERCADO DE DIVISAS / Cambio de
divisas a plazo (cierre)`` at the forward's maturity date: one
currency lands in the bought leg's current account, the other leaves
the sold leg's. The "loss" leg's ``Importe neto`` includes a
``Spread`` charge as a ``Costes`` line; we strip the spread back out
into a dedicated ``Expenses:<prefix>:Spread:<ccy>`` posting so the
audit detail isn't lost.

Shape mirrors :mod:`settle_fx_forward`: a *single* :class:`Transaction`
with the fee-bearing leg on ``currency``/``amount`` (signed as
printed), the other leg on ``counter_currency``/``counter_amount``
(also signed as printed), and ``fees``/``fees_currency`` carrying the
forward spread. The writer's ``_render_fx_settlement`` path uses all
four to emit a single beancount entry with an ``@@ <abs_gross>
<ccy>`` annotation on the counter leg, where ``gross = amount -
fees`` (signed arithmetic) reflects the cash exchange before the
spread is applied.

Field-label divergences from the EN sibling
-------------------------------------------
The skeleton is identical; only labels/markers change. The Pictet ES
advice prints the cost as ``Spread <CCY> <amount>`` rather than
``Forward spread <CCY> <amount>`` — same line position, just a
shorter label — so we keep a locale-specific spread regex here and
let the EN sibling keep its own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_cash_effect_legs,
    find_field,
    find_headline,
    find_transaction_number,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
)

# Title — load-bearing tell from the apertura variant (which uses
# ``apertura``) and from the spot variant (which uses ``al contado``).
# Anchored to a full line via ``^...$`` + ``re.M``; ``re.I`` keeps
# the gate tolerant if Pictet ever ships a mixed-case variant.
_CAMBIO_DE_DIVISAS_CIERRE_TITLE_RE = re.compile(
    r"^Cambio\s+de\s+divisas\s+a\s+plazo\s+\(cierre\)\s*$", re.M | re.I
)

# Standalone Spread line in the upper ``Costes`` block:
# ``Spread <CCY> <amount>``. The CCY identifies which leg the spread
# is charged in (matches one of the two ``EFECTO CASH`` blocks); the
# amount is signed-as-printed (Pictet writes it negative when the
# spread reduces the cash effect on that leg). Differs from the EN
# sibling's ``Forward spread <CCY> <amount>`` — same line position,
# shorter label.
_SPREAD_RE = re.compile(
    r"^Spread\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$",
    re.M,
)


@dataclass
class PictetCambioDeDivisasCierreTemplate:
    template_id: str = "pictet.cambio_de_divisas_cierre.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _CAMBIO_DE_DIVISAS_CIERRE_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text, ES_LABELS)
        if len(legs) != 2:
            return []

        # Identify which leg carries the forward spread. The upper
        # ``Costes`` block prints a single ``Spread <CCY> <amount>``
        # line; the CCY matches one of the two EFECTO CASH legs.
        # Without this we don't know which leg's net is fee-inclusive
        # (and therefore which leg's @@ value to derive from
        # gross-not-net).
        spread_match = _SPREAD_RE.search(text)
        if spread_match is None:
            return []
        fees_currency = spread_match.group(1)
        fees = parse_pictet_amount(spread_match.group(2))

        # Order the legs so the fee-bearing one lands on
        # ``currency``/``amount`` (the writer emits the Expenses
        # posting next to it); the other leg becomes the @@-bearing
        # counter leg.
        fee_leg = next((leg for leg in legs if leg.currency == fees_currency), None)
        if fee_leg is None:
            return []
        other_leg = next(leg for leg in legs if leg is not fee_leg)

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)
        narration = find_headline(text, ES_LABELS) or ""

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
                title="Cambio de divisas a plazo (cierre)",
                currency=fee_leg.currency,
                amount=fee_leg.amount,
                counter_currency=other_leg.currency,
                counter_amount=other_leg.amount,
                fees=fees,
                fees_currency=fees_currency,
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]

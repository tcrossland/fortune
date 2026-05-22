"""``pictet.settle_fx_forward.v1`` — FX forward settlement advice.

The cash-bearing companion to :mod:`fx_forward`. Pictet emits this
document under ``OTC DERIVATIVE / Settle FX forward`` at the forward's
maturity date: one currency lands in the bought leg's current account,
the other leaves the sold leg's. The "loss" leg's ``Net amount``
includes a ``Forward spread`` charge as a ``Costs`` line; we strip the
spread back out into a dedicated ``Expenses:<prefix>:Fees:<ccy>``
posting so the audit detail isn't lost.

Shape mirrors :mod:`internal_transfer`: a *single* :class:`Transaction`
with the fee-bearing leg on ``currency``/``amount`` (signed as printed),
the other leg on ``counter_currency``/``counter_amount`` (also signed
as printed), and ``fees``/``fees_currency`` carrying the forward
spread. The writer's ``_render_fx_settlement`` path uses all four to
emit a single beancount entry with an ``@@ <abs_gross> <ccy>``
annotation on the counter leg, where ``gross = amount - fees`` (signed
arithmetic) reflects the cash exchange before the spread is applied.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_cash_effect_legs,
    find_field,
    find_headline,
    find_transaction_number,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
)

_SETTLE_FX_FORWARD_TITLE_RE = re.compile(r"^Settle\s+FX\s+forward\s*$", re.M)

# Standalone Forward-spread line in the upper ``Costs`` block:
# ``Forward spread <CCY> <amount>``. The CCY identifies which leg the
# spread is charged in (matches one of the two ``CASH EFFECT`` blocks);
# the amount is signed-as-printed (Pictet writes it negative when the
# spread reduces the cash effect on that leg).
_FORWARD_SPREAD_RE = re.compile(
    r"^Forward\s+spread\s+([A-Z]{3})\s+(-?\d{1,3}(?:'\d{3})*(?:\.\d+)?)\s*$",
    re.M,
)


@dataclass
class PictetSettleFxForwardTemplate:
    template_id: str = "pictet.settle_fx_forward.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _SETTLE_FX_FORWARD_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text, EN_LABELS)
        if len(legs) != 2:
            return []

        # Identify which leg carries the forward spread. The upper
        # ``Costs`` block prints a single ``Forward spread <CCY> <amount>``
        # line; the CCY matches one of the two CASH EFFECT legs. Without
        # this we don't know which leg's net is fee-inclusive (and
        # therefore which leg's @@ value to derive from gross-not-net).
        spread_match = _FORWARD_SPREAD_RE.search(text)
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

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)
        narration = find_headline(text, EN_LABELS) or ""

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
                title="Settle FX forward",
                currency=fee_leg.currency,
                amount=fee_leg.amount,
                counter_currency=other_leg.currency,
                counter_amount=other_leg.amount,
                fees=fees,
                fees_currency=fees_currency,
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]

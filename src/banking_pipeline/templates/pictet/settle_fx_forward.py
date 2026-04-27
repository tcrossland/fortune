"""``pictet.settle_fx_forward.v1`` — FX forward settlement advice.

The cash-bearing companion to :mod:`fx_forward`. Pictet emits this document
under ``OTC DERIVATIVE / Settle FX forward`` at the forward's maturity
date: one currency lands in the bought leg's current account, the other
leaves the sold leg's. The "loss" leg's ``Net amount`` includes a
``Forward spread`` charge as a ``Costs`` line — Net is already all-in, so
we use it directly without separately attributing the spread.

The two transactions share narration so the settlement is rejoinable as
a single FX event downstream, just like spot.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_cash_effect_legs,
    find_field,
    find_headline,
    legs_to_transactions,
    parse_pictet_date,
)

_SETTLE_FX_FORWARD_TITLE_RE = re.compile(r"^Settle\s+FX\s+forward\s*$", re.M)


@dataclass
class PictetSettleFxForwardTemplate:
    template_id: str = "pictet.settle_fx_forward.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _SETTLE_FX_FORWARD_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text)
        if len(legs) != 2:
            return []

        value_date_raw = find_field(text, "Value date")
        narration = f"Pictet FX forward (settle) - {find_headline(text) or ''}".strip(
            " -"
        )

        return legs_to_transactions(
            legs,
            doc=doc,
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
        )

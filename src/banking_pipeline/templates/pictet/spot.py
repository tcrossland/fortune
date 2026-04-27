"""``pictet.spot.v1`` — FX spot trade.

Pictet emits this document under ``FOREIGN EXCHANGE / Spot`` for over-the-
counter spot FX trades. Two ``CASH EFFECT`` blocks land per document — the
sold currency leaves one current account, the bought currency lands in
another — and we extract one :class:`Transaction` per leg.

Narration is shared across both legs and carries the trade headline
(``Sell USD 69'920.99 - Buy EUR at 1.151695``) so callers can group the
two transactions back into a single FX event by source path or narration.
The execution rate stays embedded in the headline rather than being
exposed as a separate field — the model has no ``rate`` slot today.
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

# Title appears on its own line — distinguishes from FX forwards which
# share the same parent banner OTC DERIVATIVE / FOREIGN EXCHANGE.
_SPOT_TITLE_RE = re.compile(r"^Spot\s*$", re.M)


@dataclass
class PictetSpotTemplate:
    template_id: str = "pictet.spot.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _SPOT_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text)
        if len(legs) != 2:
            return []

        value_date_raw = find_field(text, "Value date")
        narration = f"Pictet spot - {find_headline(text) or ''}".strip(" -")

        return legs_to_transactions(
            legs,
            doc=doc,
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
        )

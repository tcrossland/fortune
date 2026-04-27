"""``pictet.fx_forward.v1`` — FX forward contract opening advice.

Pictet emits this document under ``OTC DERIVATIVE / FX forward`` when an
FX forward is *opened* — the contract is booked but no cash moves until
maturity (the ``CASH EFFECT`` blocks both carry zero amounts as a
deliberate signal). The matching cash settlement is recorded later by
:mod:`settle_fx_forward`.

We still emit two zero-amount transactions, one per leg, so the contract
opening is recorded in the audit trail. This is consistent with
:mod:`limit_extension`'s zero-amount emission for events that *happened*
even if no cash moved on the trade date. Beancount renderers can choose
to skip the postings or emit a ``note`` directive; the template's job is
to capture the event.
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

# Title on its own line. Pictet's settle advice carries ``Settle FX
# forward`` as a *different* title, so anchored matching distinguishes
# the two even though ``FX forward`` appears as a substring of the settle
# title elsewhere in the body.
_FX_FORWARD_TITLE_RE = re.compile(r"^FX\s+forward\s*$", re.M)
_SETTLE_FX_FORWARD_TITLE_RE = re.compile(r"^Settle\s+FX\s+forward\s*$", re.M)


@dataclass
class PictetFxForwardTemplate:
    template_id: str = "pictet.fx_forward.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # The settle advice's title is a superstring of this one; explicit
        # rejection prevents misrouting from extracting zero-amount legs
        # off a settle doc whose actual cash legs would otherwise be lost.
        if _SETTLE_FX_FORWARD_TITLE_RE.search(text):
            return []
        if not _FX_FORWARD_TITLE_RE.search(text):
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text)
        if len(legs) != 2:
            return []

        value_date_raw = find_field(text, "Value date")
        narration = f"Pictet FX forward (open) - {find_headline(text) or ''}".strip(" -")

        return legs_to_transactions(
            legs,
            doc=doc,
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
        )

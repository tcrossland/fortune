"""``pictet.spot.v1`` — FX spot trade.

Pictet emits this document under ``FOREIGN EXCHANGE / Spot`` for
over-the-counter spot FX trades. Two ``CASH EFFECT`` blocks land per
document — the sold currency leaves one current account, the bought
currency lands in another, both within the same Pictet portfolio.

The two legs map to a *single* :class:`Transaction` with the source
(debit) leg on ``currency``/``amount`` (signed negative — cash out) and
the destination (credit) leg on ``counter_currency`` /
``counter_amount`` (signed positive — cash in). The writer's
``_render_internal_transfer`` builder then emits a single beancount
entry with an ``@@ <abs_source> <src_ccy>`` annotation on the
destination leg — same shape as ``INTERNAL_TRANSFER``. Earlier this
template emitted two independent ``Transaction`` rows that the legacy
``_FX_LEG_TEMPLATE`` rendered as a pair of entries balanced against
``Equity:Uncategorized``; the single-Transaction shape lets beancount
cross-reconcile the FX relationship Pictet records on the document.

Narration is the trade headline (``Sell USD 69'920.99 - Buy EUR at
1.151695``); the execution rate stays embedded there rather than
becoming a separate field.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_cash_effect_legs,
    find_exchange_rate,
    find_field,
    find_headline,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
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

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        legs = find_cash_effect_legs(text, EN_LABELS)
        if len(legs) != 2:
            return []

        # Pictet prints the sold-currency leg first (negative amount,
        # cash out) and the bought-currency leg second (positive,
        # cash in). Sort defensively in case a future fixture inverts
        # the order so the renderer's debit→credit assumption holds.
        debit_leg = next((leg for leg in legs if leg.amount < 0), None)
        credit_leg = next((leg for leg in legs if leg.amount > 0), None)
        if debit_leg is None or credit_leg is None:
            return []

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)
        narration = (find_headline(text) or "Pictet spot")[:140]

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
                title="Spot",
                currency=debit_leg.currency,
                amount=debit_leg.amount,
                counter_currency=credit_leg.currency,
                counter_amount=credit_leg.amount,
                exchange_rate=find_exchange_rate(text, EN_LABELS),
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]

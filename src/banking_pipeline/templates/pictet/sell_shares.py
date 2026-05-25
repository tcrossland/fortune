"""``pictet.sell_shares.v1`` — direct equity sale advice.

The sell counterpart to :mod:`buy_shares`. Pictet emits this under the
``SECURITY / Sell Shares`` banner when a direct equity holding is sold
(Operation type ``Sell``, ``Asset type Equities``). Field layout mirrors
the buy advice — single CASH EFFECT block, ``Executed quantity`` printed
negative, proceeds positive — so the shared
:func:`extract_simple_trade_advice` helper does the parsing and this
module just declares the template's identity and the ``Operation type``
value it expects.

The ``Operation type`` guard is defence-in-depth against a classifier
mis-route from ``REDEMPTION_NOTICE`` (which uses the fund-redemption
``Sale``) — the very mis-route that previously dropped these advices
before this doctype existed; the asset-type discriminator (``Asset type
Equities``) lives in the classifier rule.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetSellSharesTemplate:
    template_id: str = "pictet.sell_shares.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Sell",),
            fallback_narration="Pictet shares sale",
            title="Sell Shares",
        )
        return [tx] if tx else []

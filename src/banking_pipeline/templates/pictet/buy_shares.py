"""``pictet.buy_shares.v1`` — direct equity purchase advice.

Pictet emits this document under the ``SECURITY / Buy Shares`` banner
when a direct equity purchase executes (Operation type ``Buy``,
``Asset type Equities``). Field layout matches the fund-subscription /
ETF / structured-products advices — single CASH EFFECT block on the
client's current account in the equity's quotation currency — so the
shared :func:`extract_simple_trade_advice` helper does the parsing
and this module just declares the template's identity and the
``Operation type`` value it expects.

The ``Operation type`` guard is defence-in-depth against a classifier
mis-route from the other ``Buy <type>`` variants (which all use
``Buy``) or from a fund subscription (which uses ``Purchase``); the
asset-type discriminator (``Asset type Equities``) lives in the
classifier rule, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetBuySharesTemplate:
    template_id: str = "pictet.buy_shares.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Buy",),
            fallback_narration="Pictet shares purchase",
            title="Buy Shares",
        )
        return [tx] if tx else []

"""``pictet.sell_etf.v1`` — ETF sale advice.

Pictet emits this document under ``SECURITY / Sell Exchange Traded Fund``
when an ETF holding is sold back to the market. Field layout mirrors
the buy advice — single CASH EFFECT block, ``Executed quantity``
(negative on sell), ``Execution price <ccy> <amount>`` in the trade
currency, optional fees — so the shared
:func:`extract_simple_trade_advice` helper does the parsing and this
module just declares the template's identity and the ``Operation
type`` value it expects.

The ``Operation type`` guard is defence-in-depth against a classifier
mis-route from ``SELL_STRUCTURED_PRODUCTS`` (which also uses ``Sell``)
or from ``REDEMPTION_NOTICE`` (which uses ``Sale``); the asset-type
discriminator (``Asset type Exchange Traded Fund``) lives in the
classifier rule, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetSellEtfTemplate:
    template_id: str = "pictet.sell_etf.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Sell",),
            fallback_narration="Pictet ETF sale",
            title="Sell Exchange Traded Fund",
        )
        return [tx] if tx else []

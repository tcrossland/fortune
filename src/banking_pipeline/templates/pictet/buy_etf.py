"""``pictet.buy_etf.v1`` — ETF purchase advice.

Pictet emits this document under the ``SECURITY / Buy Exchange Traded Fund``
banner when an ETF buy executes (Operation type ``Buy``). Field layout is
identical to the fund-subscription and structured-products advices — single
CASH EFFECT block on the client's current account in the ETF's quotation
currency — so the shared :func:`extract_simple_trade_advice` helper does the
parsing and this module just declares the template's identity and the
``Operation type`` value it expects.

The ``Operation type`` guard is defence-in-depth against a classifier
mis-route from BUY_STRUCTURED_PRODUCTS (which also uses "Buy") or from a
fund SUBSCRIPTION_NOTICE (which uses "Purchase"); the asset-type discriminator
lives in the classifier rule, not here.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetBuyEtfTemplate:
    template_id: str = "pictet.buy_etf.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Buy",),
            fallback_narration="Pictet ETF purchase",
            title="Buy Exchange Traded Fund",
        )
        return [tx] if tx else []

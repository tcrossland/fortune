"""``pictet.sell_structured_products.v1`` — structured-products sale advice.

Pictet emits this document under ``SECURITY / Sell structured products``
when a PWM equity certificate (PEC) or similar OTC structured product
is sold back. Field layout mirrors the matching buy advice — single
CASH EFFECT block, ``Executed quantity`` (negative on sell),
``Execution price <ccy> <amount>`` in the trade currency, optional
fees — so the shared :func:`extract_simple_trade_advice` helper does
the parsing and this module just declares the template's identity
plus the ``Operation type`` value it expects.

Distinction from ``SELL_BONDS``: structured products are sold in
unit count with the price quoted in the trade currency (``EUR
43.91``); bonds are sold in face-value nominal with a percentage
price (``102.902%``) and carry a dedicated accrued-interest leg in
the CASH EFFECT block. The two share the title verb but the asset
classification (``Asset type Structured products`` vs ``Asset type
Bonds``) and the unit-count vs nominal field separate them at the
classifier layer; the ``Operation type`` guard here is
defence-in-depth against a classifier mis-route.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetSellStructuredProductsTemplate:
    template_id: str = "pictet.sell_structured_products.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Sell",),
            fallback_narration="Pictet structured products sale",
            title="Sell structured products",
        )
        return [tx] if tx else []

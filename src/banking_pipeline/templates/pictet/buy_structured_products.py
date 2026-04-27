"""``pictet.buy_structured_products.v1`` — structured-products purchase advice.

Pictet emits this document under ``SECURITY / Buy structured products`` when
a PWM equity certificate (PEC) or similar OTC structured product is bought.
Field layout matches the fund-subscription advice closely; the meaningful
differences are an ``Asset type Structured products`` marker, a
``Maturity date`` line, and an ``Issuer`` (typically Banque Pictet itself).

Of those, only ``Operation type`` matters to the parser — the structured-
product asset-type checks live in the classifier rules, not here. The
``ISIN/Internal ref.`` field on these advices is often a Pictet-internal
code (``ZZ...``) rather than a real ISIN; the helper preserves whatever is
present, validating when possible.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetBuyStructuredProductsTemplate:
    template_id: str = "pictet.buy_structured_products.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Buy",),
            fallback_narration="Pictet structured products purchase",
        )
        return [tx] if tx else []

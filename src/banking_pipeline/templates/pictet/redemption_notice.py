"""``pictet.redemption_notice.v1`` — fund redemption advice.

The mirror of :mod:`subscription_notice`: Pictet emits this document under
``STOCK EXCHANGE / Redemption`` when a fund redemption leg executes
(Operation type ``Sale``). The cash leg is positive (proceeds in) and the
``Executed quantity`` is printed negative (units leaving the portfolio).
The shared :func:`extract_simple_trade_advice` helper preserves both signs
as printed so downstream code doesn't have to special-case them.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetRedemptionNoticeTemplate:
    template_id: str = "pictet.redemption_notice.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Sale",),
            fallback_narration="Pictet redemption",
        )
        return [tx] if tx else []

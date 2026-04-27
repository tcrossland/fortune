"""``pictet.subscription_notice.v1`` — fund subscription advice.

Pictet emits this document under the ``STOCK EXCHANGE / Subscription`` banner
when a fund subscription leg executes (Operation type ``Purchase``). One
advice maps to one :class:`~banking_pipeline.models.Transaction`: the cash
debit on the client's current account in the fund's quotation currency.

The shared :func:`extract_simple_trade_advice` helper does the actual field
parsing — this module just declares the template's identity and the
``Operation type`` value it expects, so a document classified under another
template (e.g. a ``Buy structured products`` advice) can't slip through and
get parsed by the wrong code path.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import extract_simple_trade_advice


@dataclass
class PictetSubscriptionNoticeTemplate:
    template_id: str = "pictet.subscription_notice.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        tx = extract_simple_trade_advice(
            doc,
            expected_operations=("Purchase",),
            fallback_narration="Pictet subscription",
            title="Subscription",
        )
        return [tx] if tx else []

"""``pictet.order_information_report.v1`` — pre-trade disclosure document.

Pictet emits this document **before** an order is placed, as a
regulatory-compliance disclosure of the proposed trade and its cost
structure. Unlike every other template in this package, the document
describes a **simulation**, not a historical event:

  - the ``Your trade instruction`` block lists the proposed BUY/SELL
    legs but no execution prices or settlement details,
  - the ``Costs simulation`` block estimates entry/recurring/exit costs
    over a hypothetical ``Investment Period``,
  - the actual trade, if it executes at all, will be recorded later by
    a separate ``subscription_notice`` / ``buy_etf`` / etc. advice.

There is therefore nothing transaction-shaped to extract. We register
the template anyway so a classifier-routed order-information report
short-circuits the LLM fallback rather than wastefully calling it on a
document we know carries no cash event. The template returns ``[]``.

Future enhancement: if the model gains a ``Note`` (or similar non-cash)
type, this is the document type that motivates it.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction


@dataclass
class PictetOrderInformationReportTemplate:
    template_id: str = "pictet.order_information_report.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Intentional no-op: see module docstring.
        return []

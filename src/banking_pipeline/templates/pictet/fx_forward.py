"""``pictet.fx_forward.v1`` — FX forward contract opening advice.

Pictet emits this document under ``OTC DERIVATIVE / FX forward`` when an
FX forward is *opened* — the contract is booked but no cash moves on
the trade date (both ``CASH EFFECT`` blocks deliberately carry zero
amounts as a signal that the cash leg lands at maturity, not now).
The matching cash settlement is recorded by the paired
``SETTLE_FX_FORWARD`` advice that fires at the maturity date and
references the same ``Contract no.`` / ``Unique trade ID``.

To avoid double-counting we emit no beancount entry for the opening:
the ``SETTLE_FX_FORWARD`` advice is the canonical paper trail for the
cash exchange, and a zero-amount memo entry on this side would
clutter the ledger without capturing meaningful state. Same precedent
as :mod:`interest_scale` (vs ``INTEREST_PAYMENT``) and :mod:`factura`
(vs ``DEBITO_DE_GASTOS``): when two documents describe the same
economic event, we book the cash-side one and skip the paper-trail
one. The classifier still routes ``FX_FORWARD`` documents correctly
so audit/diagnostic logs see them.

If a future use case wants to track longer-dated forwards as
contingent positions on the books (e.g. mark-to-market for 3-6 month
FX hedges), a dedicated ``Forward`` sub-account family with paired
postings between this opening and the settle advice would fit. For
now the no-entry convention is consistent with the rest of the
"two-document, one-cash-event" pairs we handle.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction


@dataclass
class PictetFxForwardTemplate:
    template_id: str = "pictet.fx_forward.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Intentionally empty: the matching ``SETTLE_FX_FORWARD`` advice
        # carries the cash leg for this contract at maturity. Emitting
        # zero-amount postings here would just clutter the ledger.
        return []

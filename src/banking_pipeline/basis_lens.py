"""The per-jurisdiction cost-basis seam for the holdings report.

A :class:`BasisLens` supplies each held security's cost basis under one tax
jurisdiction's rules. The holdings report joins a lens's output with the
statement-derived market value to show unrealised gain/loss, and cross-checks
the lens's quantity against the statement's.

This module is jurisdiction-neutral by design — it imports no tax code, so
each lens (UK section 104 in :mod:`banking_pipeline.tax.uk.basis`; a future
EUR/Spanish FIFO) depends on the seam without the seam depending on any
jurisdiction.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar, Protocol


@dataclass(frozen=True)
class HoldingBasis:
    """One security's cost basis under a particular jurisdiction lens.

    ``held_qty`` is the lens's own quantity — e.g. the UK section 104 residual
    pool — which the report cross-checks against the statement quantity.
    ``cost_amount`` is denominated in ``currency``. ``market_value`` is
    ``None`` when the lens defers to the statement mark (the same-currency
    case, e.g. a GBP lens against GBP statement marks); a non-GBP lens
    supplies its own market value, because statement-date FX differs between
    jurisdictions.

    ``cost_adjustment`` is the portion of ``cost_amount`` contributed by dated
    base-cost adjustments rather than the raw acquisition cost — for the UK
    lens, the ERI (excess reportable income) uplift a reporting fund adds to
    the section 104 pool. It's what separates the lens's cost from a broker's
    book cost, so the report can show it. ``0`` when the lens has none.
    """

    isin: str
    held_qty: Decimal
    cost_amount: Decimal
    currency: str
    market_value: Decimal | None = None
    cost_adjustment: Decimal = Decimal("0")


class BasisLens(Protocol):
    """A per-jurisdiction source of holdings cost basis.

    ``name`` identifies the lens (e.g. ``"uk-s104"``); ``currency`` is the ISO
    4217 code its amounts are denominated in. ``basis_for`` returns the cost
    basis of every currently-held security the lens knows about (quantity
    > 0), keyed by ISIN.
    """

    name: ClassVar[str]
    currency: ClassVar[str]

    def basis_for(self) -> dict[str, HoldingBasis]: ...

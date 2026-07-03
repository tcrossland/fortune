"""UK section 104 cost-basis lens for the holdings report.

Wraps :func:`banking_pipeline.tax.uk.sa108.match_history` and reads its
residual section 104 pool per security, so cost basis comes from the JSONL
sidecar substrate — never from ledger text. GBP throughout: market value is
left to the statement mark, so :attr:`HoldingBasis.market_value` is ``None``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar

from banking_pipeline.basis_lens import HoldingBasis
from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.fx.gbp_rates import GbpRateSource
from banking_pipeline.models import Transaction
from banking_pipeline.opening_positions import OpeningLot
from banking_pipeline.tax.uk.sa108 import match_history
from banking_pipeline.tax.uk.section_104 import PoolCostAdjustment


@dataclass(frozen=True)
class UkSection104Lens:
    """Cost basis from the UK section 104 pool (GBP), over the full sidecar
    transaction history.

    ``transactions`` should span the whole available history — the pool is
    cumulative. A fully-disposed holding (residual quantity 0) is omitted; the
    report shows currently-held securities only. Deeply-discounted holdings
    are included: they leave the CGT return but are still held.
    """

    transactions: list[Transaction]
    commodities: dict[str, CommodityMetadata]
    source: GbpRateSource | None = None
    opening_positions: dict[str, list[OpeningLot]] | None = None
    cost_adjustments: dict[str, list[PoolCostAdjustment]] | None = None

    name: ClassVar[str] = "uk-s104"
    currency: ClassVar[str] = "GBP"

    def basis_for(self) -> dict[str, HoldingBasis]:
        history = match_history(
            self.transactions,
            commodities=self.commodities,
            source=self.source,
            opening_positions=self.opening_positions,
            cost_adjustments=self.cost_adjustments,
        )
        return {
            isin: HoldingBasis(
                isin=isin,
                held_qty=pool.qty,
                cost_amount=pool.cost_gbp,
                currency="GBP",
                market_value=None,
            )
            for isin, pool in history.residual_pools.items()
            if pool.qty > 0
        }

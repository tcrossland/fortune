"""Pre-ledger / transferred-in opening positions for the section 104 pool.

The ledger only goes back to its earliest data (2021-07). Any holding
acquired before that, or transferred in from another custodian, has no
acquisition in the pipeline — so its allowable cost is understated (often
zero) and the CGT gain overstated. This module loads a user-maintained
table of opening lots (ISIN, acquisition date, quantity, GBP cost) that
:mod:`banking_pipeline.tax.uk.sa108` seeds into each security's section
104 pool before the ledger's own buys.

The file is gitignored (personal cost data); a committed
``data/opening-positions.example.toml`` documents the schema.
"""

from __future__ import annotations

import tomllib
from datetime import date
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, field_validator

from banking_pipeline.commodities_metadata import normalise_commodity_code


class OpeningLot(BaseModel):
    """One pre-ledger acquisition lot, in GBP (no rate conversion needed).

    ``cost_gbp`` is the total allowable cost (consideration + acquisition
    costs) for ``quantity`` units, already in sterling — these are
    historical figures the user supplies, not ledger-derived.
    """

    model_config = ConfigDict(frozen=True)

    isin: str
    acquired: date
    quantity: Decimal
    cost_gbp: Decimal

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, value: str) -> str:
        code = normalise_commodity_code(value)
        if code is None:
            raise ValueError(
                f"not a valid ISIN or 11-char commodity ref: {value!r}"
            )
        return code


def load_opening_positions(path: Path) -> dict[str, list[OpeningLot]]:
    """Parse ``path`` into ``{isin: [OpeningLot, ...]}``.

    A security can have several opening lots (e.g. tranches acquired on
    different dates), so values are lists.
    """

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    positions: dict[str, list[OpeningLot]] = {}
    for entry in raw.get("lot", []):
        lot = OpeningLot.model_validate(entry)
        positions.setdefault(lot.isin, []).append(lot)
    return positions

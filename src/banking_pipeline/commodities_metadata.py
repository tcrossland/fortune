"""Hand-curated commodity metadata for UK tax classification.

UK tax treats gains on UK-reporting-status offshore funds as CGT, but
gains on non-reporting funds as offshore income gains (income, not
capital). That distinction isn't derivable from a Pictet advice — it
depends on the fund's HMRC reporting status — so the user maintains a
``data/commodities.toml`` file mapping each held ISIN to its domicile,
reporting status, and asset class. :func:`load_commodities` parses it;
:mod:`banking_pipeline.portfolio_aggregate` surfaces it as beancount
``commodity`` directives so disposals can be partitioned at SA108 /
SA106 time.

The file is gitignored (it's personal holdings data); a committed
``data/commodities.example.toml`` documents the schema.
"""

from __future__ import annotations

import tomllib
from datetime import date
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from banking_pipeline.fields.validators import normalise_isin

ReportingStatus = Literal["reporting", "non-reporting", "uk-domestic", "unknown"]
AssetClass = Literal[
    "equity-etf", "bond", "equity-fund", "money-market", "other"
]


class CommodityMetadata(BaseModel):
    """One ``[[commodity]]`` entry from ``data/commodities.toml``.

    ``reporting_status`` drives the SA108-vs-SA106 partition downstream;
    ``domicile`` is the ISO 3166-1 alpha-2 country of the fund (used as a
    fallback source of withholding-tax country when an income advice
    doesn't print one). ``first_acquired`` dates the emitted beancount
    ``commodity`` directive.
    """

    model_config = ConfigDict(frozen=True)

    isin: str
    name: str
    domicile: str
    reporting_status: ReportingStatus
    asset_class: AssetClass
    first_acquired: date

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, value: str) -> str:
        normalised = normalise_isin(value)
        if normalised is None:
            raise ValueError(f"invalid ISIN: {value!r}")
        return normalised


def load_commodities(path: Path) -> dict[str, CommodityMetadata]:
    """Parse ``path`` into a ``{isin: CommodityMetadata}`` map.

    Raises ``ValueError`` on a duplicate ISIN — two entries for the same
    security are almost certainly a copy-paste error, and silently
    keeping the last would hide it. Malformed ISINs and unknown
    ``reporting_status`` / ``asset_class`` values surface as pydantic
    ``ValidationError`` from the model.
    """

    with path.open("rb") as fh:
        raw = tomllib.load(fh)

    commodities: dict[str, CommodityMetadata] = {}
    for entry in raw.get("commodity", []):
        meta = CommodityMetadata.model_validate(entry)
        if meta.isin in commodities:
            raise ValueError(
                f"duplicate commodity entry for ISIN {meta.isin} in {path}"
            )
        commodities[meta.isin] = meta
    return commodities

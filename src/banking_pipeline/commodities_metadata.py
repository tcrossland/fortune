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

import re
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

# Pictet structured products carry an 11-char internal reference instead
# of an ISIN (real ISINs are always 12 chars). They flow through the
# ledger as commodities, so commodity metadata must accept them too.
# Keying on the 11-char length keeps 12-char ISIN typos rejected.
_INTERNAL_REF_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}$")


def normalise_commodity_code(value: str) -> str | None:
    """Return the canonical commodity code, or ``None`` if unrecognised.

    A valid ISIN is checksum-validated and normalised; an 11-char
    ISIN-shaped code (a Pictet structured-product internal reference) is
    accepted as-is. Anything else — including a 12-char code that fails
    the ISIN checksum, i.e. a likely typo — returns ``None``.
    """

    cleaned = value.replace(" ", "").upper()
    real = normalise_isin(cleaned)
    if real is not None:
        return real
    if _INTERNAL_REF_RE.match(cleaned):
        return cleaned
    return None


class CommodityMetadata(BaseModel):
    """One ``[[commodity]]`` entry from ``data/commodities.toml``.

    ``reporting_status`` drives the SA108-vs-SA106 partition downstream;
    ``domicile`` is the ISO 3166-1 alpha-2 country of the fund (used as a
    fallback source of withholding-tax country when an income advice
    doesn't print one). ``first_acquired`` dates the emitted beancount
    ``commodity`` directive.

    ``isin`` is the ledger commodity code: usually a real ISIN, but also
    accepts an 11-char Pictet structured-product internal reference
    (those aren't ISINs but still need tax classification).
    """

    model_config = ConfigDict(frozen=True)

    isin: str
    name: str
    domicile: str
    reporting_status: ReportingStatus
    asset_class: AssetClass
    first_acquired: date
    # Deeply discounted security (broadly, a bond issued/acquired at a
    # discount above the de-minimis): its gain is taxed as income, not
    # CGT, and a loss is generally not allowable. User-asserted — it
    # depends on issue terms HMRC publishes, not derivable from price.
    deeply_discounted: bool = False
    # Offshore fund that is more than 60% invested in interest-bearing
    # assets (the UK "bond fund" rule): its distributions — and excess
    # reportable income — are taxed as foreign *interest*, not dividends,
    # so they belong in the SA106 interest box. User-asserted; it depends
    # on the fund's underlying asset mix, not derivable from the advice.
    distributions_as_interest: bool = False

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, value: str) -> str:
        code = normalise_commodity_code(value)
        if code is None:
            raise ValueError(
                f"not a valid ISIN or 11-char commodity ref: {value!r}"
            )
        return code


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

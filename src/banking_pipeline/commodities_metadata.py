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

# Non-ISIN ledger commodity codes that are nonetheless valid. The Vanguard
# ISA keys holdings on the fund *ticker* (its buy contract notes print no
# ISIN), so those tickers flow through as commodities and need metadata too.
# An explicit allow-list (rather than a loose short-code pattern) keeps a
# mistyped ISIN rejected. Mirror the tickers the Vanguard template resolves
# (``templates/vanguard_uk/_common._TICKER_BY_NAME``) when a new fund is held.
_TICKER_CODES = frozenset({"VGVA", "VMIG"})

# Fund-name fragments → issuer (fund house / manager). Matched as an
# upper-cased substring of the commodity ``name``, first hit wins, so a
# holding's issuer can be inferred from the name Pictet already prints
# without the user tagging all ~40 ISINs. An explicit ``issuer`` in
# ``commodities.toml`` always overrides this. Ordered so a specific
# fragment ("AB SICAV") precedes any shorter one it could collide with.
_ISSUER_PATTERNS: tuple[tuple[str, str], ...] = (
    ("PICTET", "Pictet"), ("PWM", "Pictet"),
    ("ISHARES", "iShares"), ("BLACKROCK", "BlackRock"),
    ("HSBC", "HSBC"), ("JPMF", "JPMorgan"), ("JPM ", "JPMorgan"),
    ("WISDOMTREE", "WisdomTree"),
    ("MULTI UNITS LUXEMBOURG", "Amundi"), ("AMUNDI", "Amundi"),
    ("NORDEA", "Nordea"),
    ("SISF", "Schroder"), ("SCHRODER", "Schroder"),
    ("SSGA", "State Street"), ("PIMCO", "PIMCO"), ("UBS", "UBS"),
    ("AB SICAV", "AllianceBernstein"), ("ABERDEEN", "abrdn"),
    ("AXA", "AXA"), ("BARINGS", "Barings"), ("BLUEBAY", "BlueBay"),
    ("CANDRIAM", "Candriam"), ("CHAHINE", "Chahine"),
    ("STURDZA", "E.I. Sturdza"), ("ELEVA", "Eleva"),
    ("FEDERAT", "Federated Hermes"), ("HERMES", "Federated Hermes"),
    ("KEMPEN", "Kempen"), ("LAZARD", "Lazard"), ("MIROVA", "Mirova"),
    ("MSIF", "Morgan Stanley"), ("MUZINICH", "Muzinich"),
    ("NINETY ONE", "Ninety One"), ("NN(L)", "NN"), ("ROBECO", "Robeco"),
    ("SWISSCANTO", "Swisscanto"), ("LEGAL & GENERAL", "Legal & General"),
    ("L&G", "Legal & General"), ("HANETF", "HANetf"), ("VANECK", "VanEck"),
    ("INVESCO", "Invesco"), ("COINSHARES", "CoinShares"),
    ("VANGUARD", "Vanguard"),
)


def infer_issuer(name: str) -> str | None:
    """Best-effort fund issuer from a commodity ``name``, or ``None``.

    A substring match against :data:`_ISSUER_PATTERNS` — enough to bucket
    the fund holdings without manual tagging. Returns ``None`` for names
    with no recognised house (direct equities, sovereign bonds, unrecognised
    funds), which the report shows in an ``unknown`` issuer bucket.
    """

    upper = name.upper()
    for fragment, issuer in _ISSUER_PATTERNS:
        if fragment in upper:
            return issuer
    return None


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
    if _INTERNAL_REF_RE.match(cleaned) or cleaned in _TICKER_CODES:
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
    # UK situs for the 4-year FIG regime: a non-UK asset's gain is
    # relievable under a FIG claim, a UK asset's never is. ``None`` (the
    # default) derives it — UK iff GB-domiciled or ``reporting_status``
    # is ``"uk-domestic"``. Set explicitly only to override a misleading
    # domicile (e.g. a GB-listed depositary receipt over a foreign asset).
    uk_situs: bool | None = None
    # Fund issuer / manager (iShares, Amundi, Pictet, …) for the
    # concentration report's issuer-exposure breakdown — single-provider /
    # counterparty risk that the domicile view can't see. ``None`` (the
    # default) infers it from ``name`` (see :func:`infer_issuer`); set it
    # explicitly to fix a name the inference can't place.
    issuer: str | None = None
    # Extra names this commodity is printed under on the Pictet P mandate's
    # by-name "Financial Statement" valuation, which lists holdings by an
    # abbreviated display name and NO ISIN (unlike the K statement's
    # ISIN-led rows). The balance parser resolves those names → this ISIN
    # via :func:`build_statement_name_index`. Only needed when the short
    # display form doesn't normalise-match ``name`` (e.g. a long
    # contract-note ``name`` like "HANetf ICAV - Sprott Global Uranium…"
    # vs the statement's "Hanetf-Sprott Glb Uran.Mini.Etf Usd"). A tuple so
    # the frozen model stays hashable; TOML lists coerce cleanly.
    statement_names: tuple[str, ...] = ()

    @property
    def resolved_issuer(self) -> str | None:
        """Effective issuer: the explicit field if set, else inferred from
        the fund name. ``None`` when neither resolves (an ``unknown`` bucket)."""

        return self.issuer or infer_issuer(self.name)

    @property
    def resolved_uk_situs(self) -> bool:
        """Effective UK-situs verdict: the explicit flag if set, else
        derived from domicile / reporting status."""

        if self.uk_situs is not None:
            return self.uk_situs
        return self.domicile.upper() == "GB" or self.reporting_status == "uk-domestic"

    @field_validator("isin")
    @classmethod
    def _validate_isin(cls, value: str) -> str:
        code = normalise_commodity_code(value)
        if code is None:
            raise ValueError(
                f"not a valid ISIN or 11-char commodity ref: {value!r}"
            )
        return code


# Runs of anything that isn't a letter or digit — collapsed to a single
# space so punctuation / spacing differences between the statement's
# display name ("Novo Nordisk 'B'", "Btc (Coinshares) -Etc- 21/Perp") and
# the stored ``name`` ("NOVO NORDISK 'B'", "BTC (COINSHARES) -ETC- 21/PERP")
# don't defeat the match. Accented Latin letters are kept as letters.
_NAME_SEP_RE = re.compile(r"[^0-9A-Za-zÀ-ÿ]+")


def normalise_security_name(name: str) -> str:
    """Canonical form of a security display name for cross-source matching.

    Upper-cases and collapses every run of non-alphanumerics to a single
    space, then strips. Two names that differ only in case, punctuation, or
    spacing normalise equal — enough to match the P statement's abbreviated
    display name to the ledger commodity ``name`` for the ~half that agree,
    while genuinely different names (a long contract-note ``name``) still
    diverge and need an explicit ``statement_names`` alias.
    """

    return _NAME_SEP_RE.sub(" ", name).upper().strip()


def build_statement_name_index(
    commodities: dict[str, CommodityMetadata],
) -> dict[str, str]:
    """Map ``normalise_security_name`` → ISIN for by-name balance matching.

    Indexes each commodity's ``name`` and every ``statement_names`` alias.
    The result feeds :mod:`banking_pipeline.balances_extract`, which resolves
    the Pictet P mandate's by-name holding rows (no ISIN on the statement) to
    a ledger commodity.

    Raises ``ValueError`` when two commodities normalise to the same name —
    an ambiguous mapping would silently assert a quantity against the wrong
    ISIN, so a curation clash must fail loudly (mirrors ``load_commodities``'
    duplicate-ISIN guard).
    """

    index: dict[str, str] = {}
    for meta in commodities.values():
        for raw in (meta.name, *meta.statement_names):
            key = normalise_security_name(raw)
            if not key:
                continue
            existing = index.get(key)
            if existing is not None and existing != meta.isin:
                raise ValueError(
                    f"ambiguous statement-name {raw!r} maps to both "
                    f"{existing} and {meta.isin}"
                )
            index[key] = meta.isin
    return index


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

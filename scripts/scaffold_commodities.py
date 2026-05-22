#!/usr/bin/env python3
"""Scaffold ``[[commodity]]`` entries for ISINs missing from the metadata.

Discovers the securities traded in the ledger that don't yet have an
entry in ``data/commodities.toml`` and prints ready-to-edit TOML blocks
with the *derivable* fields pre-filled:

    isin           the ISIN
    first_acquired earliest acquisition date for it in the ledger
    domicile       the ISIN's 2-letter country prefix (verify XS / ADRs)
    name           best-effort from the trade narration (verify)
    asset_class    inferred from the trade doctype (verify)
    reporting_status = "unknown"

``reporting_status`` is deliberately left ``unknown`` — set it yourself
from HMRC's *approved offshore reporting funds* list (offshore funds
only; direct shares and bonds are always CGT, so tag those ``reporting``
or, for UK securities, ``uk-domestic``).

Run from the repo root, after a rebuild so the sidecars are current, and
append to your metadata file::

    uv run python scripts/scaffold_commodities.py >> data/commodities.toml

Only ISINs absent from the existing ``data/commodities.toml`` are
emitted, so re-running never clobbers entries you've already filled in.
The TOML it prints is immediately valid (``unknown`` is a real status),
so the report just flags those holdings until you set the real status.
"""

from __future__ import annotations

import sys
import tomllib
from collections import defaultdict
from datetime import date
from pathlib import Path

from banking_pipeline import portfolio_aggregate
from banking_pipeline.commodities_metadata import normalise_commodity_code
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.transaction_sidecar import load_transactions
from banking_pipeline.writer.builders.security_trade import (
    SECURITY_BUY_TYPES,
)

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")

# Rough asset-class guess from the doctype that traded the security.
# Informational only (reporting_status drives the tax routing), so a
# best-effort default the user verifies is fine.
_ASSET_CLASS: dict[DocumentType, str] = {
    DocumentType.BUY_ETF: "equity-etf",
    DocumentType.SELL_ETF: "equity-etf",
    DocumentType.BUY_BONDS: "bond",
    DocumentType.SELL_BONDS: "bond",
    DocumentType.FINAL_REDEMPTION: "bond",
    DocumentType.REEMBOLSO_FINAL: "bond",
    DocumentType.SUBSCRIPTION_NOTICE: "equity-fund",
    DocumentType.REDEMPTION_NOTICE: "equity-fund",
    DocumentType.SUSCRIPCION: "equity-fund",
    DocumentType.REEMBOLSO: "equity-fund",
    DocumentType.SWITCH_ENTRADA: "equity-fund",
    DocumentType.SWITCH_SALIDA: "equity-fund",
}

# Dividend / distribution narrations read ``<title> - <fund name>``,
# which is a clean name source. Trade narrations bundle amounts and
# prices in, so we don't try to parse a name out of those.
_NAME_DOCTYPES = frozenset({
    DocumentType.DIVIDEND_NOTICE,
    DocumentType.DISTRIBUCION,
})


def _existing_isins(path: Path) -> set[str]:
    """ISINs already present in ``path`` (lenient — tolerates a file
    mid-edit by reading raw TOML rather than the validated model)."""

    if not path.is_file():
        return set()
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    return {
        str(entry["isin"])
        for entry in raw.get("commodity", [])
        if "isin" in entry
    }


def _sidecar_transactions(data_dir: Path) -> list[Transaction]:
    txns: list[Transaction] = []
    for path in sorted(data_dir.rglob("*.transactions.jsonl")):
        txns.extend(load_transactions(path))
    return txns


def _guess_name(txns: list[Transaction]) -> str:
    for tx in txns:
        if tx.document_type in _NAME_DOCTYPES and " - " in tx.narration:
            return tx.narration.split(" - ", 1)[1].strip()
    return ""


def _guess_asset_class(txns: list[Transaction]) -> str:
    for tx in txns:
        if tx.document_type in _ASSET_CLASS:
            return _ASSET_CLASS[tx.document_type]
    return "other"


def _first_acquired(txns: list[Transaction]) -> date | None:
    buys = [
        tx.trade_date
        for tx in txns
        if tx.document_type in SECURITY_BUY_TYPES
        or (tx.quantity is not None and tx.quantity > 0 and tx.amount < 0)
    ]
    if buys:
        return min(buys)
    dates = [tx.trade_date for tx in txns]
    return min(dates) if dates else None


def _emit(isin: str, txns: list[Transaction]) -> str:
    acquired = _first_acquired(txns)
    if acquired is not None:
        acquired_line = f"first_acquired = {acquired.isoformat()}"
    else:
        # No sidecar transactions for this ISIN — rebuild and re-run.
        acquired_line = "first_acquired = 1970-01-01  # TODO: rebuild, then re-run"
    return "\n".join([
        "[[commodity]]",
        f'isin = "{isin}"',
        f'name = "{_guess_name(txns)}"',
        f'domicile = "{isin[:2]}"',
        'reporting_status = "unknown"',
        f'asset_class = "{_guess_asset_class(txns)}"',
        acquired_line,
    ])


def main() -> int:
    metadata_path = DATA_DIR / "commodities.toml"
    known = _existing_isins(metadata_path)
    in_use = portfolio_aggregate.discover_isins(DATA_DIR)
    candidates = sorted(in_use - known)

    # Keep valid commodity codes — real ISINs and 11-char Pictet
    # structured-product refs (CommodityMetadata accepts both). Anything
    # else (a malformed code) is dropped so the output stays load-clean.
    missing = [c for c in candidates if normalise_commodity_code(c) is not None]
    unrecognised = [c for c in candidates if normalise_commodity_code(c) is None]

    if not missing:
        print("Nothing to scaffold — every in-use ISIN has metadata.", file=sys.stderr)
        if unrecognised:
            print(
                f"({len(unrecognised)} unrecognised commodity code(s) skipped: "
                f"{', '.join(unrecognised)})",
                file=sys.stderr,
            )
        return 0

    by_isin: dict[str, list[Transaction]] = defaultdict(list)
    for tx in _sidecar_transactions(DATA_DIR):
        if tx.isin in missing:
            by_isin[tx.isin].append(tx)

    print(
        "# Scaffolded by scripts/scaffold_commodities.py — VERIFY each field.\n"
        "# Set reporting_status from HMRC's approved offshore reporting funds\n"
        "# list (offshore funds only; direct shares/bonds = CGT).\n"
    )
    print("\n\n".join(_emit(isin, by_isin.get(isin, [])) for isin in missing))
    print(f"\nScaffolded {len(missing)} commodity code(s).", file=sys.stderr)
    if unrecognised:
        print(
            f"Skipped {len(unrecognised)} unrecognised code(s): "
            f"{', '.join(unrecognised)}",
            file=sys.stderr,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

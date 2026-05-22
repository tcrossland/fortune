#!/usr/bin/env python3
"""Fill empty ``name`` fields in ``data/commodities.toml`` from the ledger.

The scaffold leaves ``name = ""`` for most commodities (it only catches
the clean ``Dividend - <name>`` form). This recovers the security name
from the trade narrations in the JSONL sidecars, where it sits between
the quantity and the price, e.g.::

    Buy 22'500 iShares Asia Trust - ... ETF -HKD Counter-Inc- at HKD 10.00
    Purchase EUR 90'000.00 2.30% GERMANY 23/33 SR GREEN at 97.512%
    Compra 604 EUR PWM LG VOL BALANC (PICTET)21/22 a EUR 49.94
    Reembolso - EUR PWM LG VOL BALANC (PICTET)21/22

Only ``name = ""`` lines are touched — names you've already set are left
alone — so it's safe to re-run after a rebuild + scaffold. Edits are
made in place after the new content is validated as parseable TOML.

    uv run python scripts/fill_commodity_names.py
"""

from __future__ import annotations

import collections
import re
import sys
import tomllib
from pathlib import Path

from banking_pipeline.transaction_sidecar import load_transactions

DATA_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("data")
METADATA = DATA_DIR / "commodities.toml"

# Security name lives between the quantity and the price.
_EN_TRADE = re.compile(
    r"^(?:Buy|Sell|Sale|Purchase)\s+(?:[A-Z]{3}\s+)?-?[\d',.]+\s+(?P<name>.+?)\s+at\s+"
)
_ES_TRADE = re.compile(
    r"^(?:Compra|Venta)\s+-?[\d',.]+\s+(?P<name>.+?)\s+a\s+[A-Z]{3}\s"
)
# Clean ``<verb> - <name>`` forms (dividend / redemption narrations).
_DASH = re.compile(
    r"^(?:Dividend|Dividendo|Distribuci[oó]n|Reembolso(?:\s+final)?|"
    r"Final\s+redemption)\s+-\s+(?P<name>.+)$",
    re.IGNORECASE,
)

_ISIN_LINE = re.compile(r'^\s*isin\s*=\s*"([^"]+)"')
_EMPTY_NAME_LINE = re.compile(r'^(?P<indent>\s*)name\s*=\s*""\s*$')


def _name_from_narration(narration: str) -> str | None:
    for pattern in (_EN_TRADE, _ES_TRADE, _DASH):
        m = pattern.match(narration)
        if m:
            return m.group("name").strip()
    return None


def _names_by_isin(data_dir: Path) -> dict[str, str]:
    """Most common extracted name per ISIN across all sidecar narrations."""

    candidates: dict[str, collections.Counter[str]] = collections.defaultdict(
        collections.Counter
    )
    for path in sorted(data_dir.rglob("*.transactions.jsonl")):
        for tx in load_transactions(path):
            if not tx.isin:
                continue
            name = _name_from_narration(tx.narration)
            if name:
                candidates[tx.isin][name] += 1
    # Most frequent name wins; ties break toward the longer (more
    # specific) string.
    return {
        isin: max(counter, key=lambda n: (counter[n], len(n)))
        for isin, counter in candidates.items()
    }


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace('"', '\\"')


def main() -> int:
    if not METADATA.is_file():
        print(f"{METADATA} not found", file=sys.stderr)
        return 1

    names = _names_by_isin(DATA_DIR)

    out: list[str] = []
    current_isin: str | None = None
    filled = 0
    for line in METADATA.read_text(encoding="utf-8").splitlines():
        m_isin = _ISIN_LINE.match(line)
        if m_isin:
            current_isin = m_isin.group(1)
        m_name = _EMPTY_NAME_LINE.match(line)
        if m_name and current_isin and names.get(current_isin):
            out.append(f'{m_name.group("indent")}name = "{_escape(names[current_isin])}"')
            filled += 1
            continue
        out.append(line)

    new_text = "\n".join(out) + "\n"
    # Validate before overwriting — a bad escape would corrupt the file.
    tomllib.loads(new_text)
    METADATA.write_text(new_text, encoding="utf-8")

    print(f"Filled {filled} name(s) in {METADATA}.", file=sys.stderr)
    still_empty = [
        isin
        for isin in _isins_without_name(new_text)
        if isin not in names
    ]
    if still_empty:
        print(
            f"No name found for {len(still_empty)}: {', '.join(still_empty)}",
            file=sys.stderr,
        )
    return 0


def _isins_without_name(text: str) -> list[str]:
    parsed = tomllib.loads(text)
    return [c["isin"] for c in parsed.get("commodity", []) if not c.get("name")]


if __name__ == "__main__":
    raise SystemExit(main())

"""Portfolio aggregate source discovery."""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import portfolio_aggregate


def test_property_ledger_excluded_from_aggregate(tmp_path: Path) -> None:
    """``data/property.beancount`` is included directly by main.beancount, so
    the aggregate must neither source it (double-counting holdings) nor
    re-include it (double-include) — it's ignored entirely."""

    # A per-year ingest source.
    (tmp_path / "2025-K.beancount").write_text(
        "2025-01-02 * \"Pago\" \"x\"\n"
        "  Assets:Pic:K1:GBP   -100.00 GBP\n"
        "  Expenses:Pic:K1:Other\n",
        encoding="utf-8",
    )
    # The property ledger (self-contained: own commodity + opens).
    (tmp_path / "property.beancount").write_text(
        "2025-09-05 commodity ROCKLEAZE\n"
        "2025-09-05 open Assets:Property:Rockleaze ROCKLEAZE\n"
        "2025-09-05 open Equity:Property:Rockleaze\n",
        encoding="utf-8",
    )

    out, _ = portfolio_aggregate.generate(tmp_path)
    text = out.read_text(encoding="utf-8")

    assert 'include "2025-K.beancount"' in text
    assert 'include "property.beancount"' not in text
    # Its accounts aren't centrally opened by the aggregate either.
    assert "Assets:Property:Rockleaze" not in text

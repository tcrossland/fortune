"""Portfolio aggregate source discovery."""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import portfolio_aggregate


def test_ignored_ledger_excluded_from_aggregate(tmp_path: Path) -> None:
    """A file passed via ``ignore`` (e.g. the property ledger, which
    main.beancount includes directly) is neither sourced — double-counting
    holdings — nor re-included by the aggregate."""

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

    out, _ = portfolio_aggregate.generate(tmp_path, ignore=("property.beancount",))
    text = out.read_text(encoding="utf-8")

    assert 'include "2025-K.beancount"' in text
    assert 'include "property.beancount"' not in text
    # Its accounts aren't centrally opened by the aggregate either.
    assert "Assets:Property:Rockleaze" not in text


def test_zeroed_isin_closed_across_source_files(tmp_path: Path) -> None:
    """An ISIN asset account whose units net to zero across *separate*
    source files is closed by the aggregate, dated the day after the
    account's last posting."""

    (tmp_path / "2024-1.beancount").write_text(
        "2024-03-01 * \"BUY\"\n"
        "  Assets:Pic:P1:US0378331005:USD  100 US0378331005 {123.45 USD}\n"
        "  Assets:Pic:Cash:USD            -12345.00 USD\n",
        encoding="utf-8",
    )
    (tmp_path / "2024-2.beancount").write_text(
        "2024-09-15 * \"SELL\"\n"
        "  Assets:Pic:P1:US0378331005:USD  -100 US0378331005 {} @ 150.00 USD\n"
        "  Assets:Pic:Cash:USD             15000.00 USD\n",
        encoding="utf-8",
    )

    out, _ = portfolio_aggregate.generate(tmp_path)
    text = out.read_text(encoding="utf-8")

    assert "2024-09-16 close Assets:Pic:P1:US0378331005:USD" in text


def test_reacquired_isin_not_closed_across_source_files(tmp_path: Path) -> None:
    """A position wound down to zero in one source file but re-acquired in a
    *later* file nets to non-zero across the full history, so the aggregate
    must not close it — closing then re-buying would break bean-check."""

    (tmp_path / "2024-1.beancount").write_text(
        "2024-03-01 * \"BUY\"\n"
        "  Assets:Pic:P1:US0378331005:USD  100 US0378331005 {123.45 USD}\n"
        "  Assets:Pic:Cash:USD            -12345.00 USD\n"
        "\n"
        "2024-06-01 * \"SELL\"\n"
        "  Assets:Pic:P1:US0378331005:USD  -100 US0378331005 {} @ 150.00 USD\n"
        "  Assets:Pic:Cash:USD             15000.00 USD\n",
        encoding="utf-8",
    )
    (tmp_path / "2024-2.beancount").write_text(
        "2024-09-01 * \"RE-BUY\"\n"
        "  Assets:Pic:P1:US0378331005:USD  60 US0378331005 {140.00 USD}\n"
        "  Assets:Pic:Cash:USD            -8400.00 USD\n",
        encoding="utf-8",
    )

    out, _ = portfolio_aggregate.generate(tmp_path)
    text = out.read_text(encoding="utf-8")

    assert "close Assets:Pic:P1:US0378331005:USD" not in text

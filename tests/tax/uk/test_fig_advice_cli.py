"""End-to-end ``fig-advice`` CLI against a synthetic sidecar ledger."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.transaction_sidecar import dump_transactions

_COMMODITIES = """
[[commodity]]
isin = "LU1287023185"
name = "Lux fund"
domicile = "LU"
reporting_status = "reporting"
asset_class = "equity-fund"
first_acquired = 2018-03-15
"""


def _tx(**kw: object) -> Transaction:
    base: dict[str, object] = dict(
        trade_date=date(2025, 6, 1), narration="x", currency="GBP",
        amount=Decimal("0"), source_path=Path("t.pdf"),
    )
    base.update(kw)
    return Transaction(**base)  # type: ignore[arg-type]


def test_fig_advice_recommends_claiming_the_big_gain_year(
    tmp_path: Path, monkeypatch: object
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    lu = "LU1287023185"
    # Buy cheap pre-window; sell for a large foreign gain in 2025-26.
    txns = [
        _tx(document_type=DocumentType.BUY_ETF, isin=lu, quantity=Decimal("1000"),
            amount=Decimal("-10000"), trade_date=date(2022, 1, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=lu, quantity=Decimal("-1000"),
            amount=Decimal("80000"), trade_date=date(2025, 6, 1)),
    ]
    dump_transactions(txns, data_dir / "2025.transactions.jsonl")
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    # Arrival 2023-24 → eligible FIG window is 2025-26 and 2026-27.
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli.settings, "uk_residence_start_date", date(2023, 7, 14)
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "fig-advice", "--income", "0", "--source", str(data_dir),
            "--out", str(out_dir), "--commodities", str(commodities),
            "--rate-source", "null",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "recommended: claim [2025-26]" in result.output

    advice = (out_dir / "fig-advice.txt").read_text(encoding="utf-8")
    assert "Eligible FIG window: 2025-26, 2026-27" in advice
    assert "RECOMMENDED: claim [2025-26]" in advice
    # The big 2025-26 gain is relieved by claiming it → cheaper than not.
    assert "claim [2025-26]:" in advice


def test_fig_advice_no_window_errors(tmp_path: Path, monkeypatch: object) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    # No residence date configured → no FIG window.
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli.settings, "uk_residence_start_date", None
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["fig-advice", "--income", "0", "--source", str(data_dir)]
    )
    assert result.exit_code == 1
    assert "No FIG window" in result.output

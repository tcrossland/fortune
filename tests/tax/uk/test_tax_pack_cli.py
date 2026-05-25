"""End-to-end ``tax-pack`` CLI against a synthetic sidecar ledger."""

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
isin = "IE00B3VWN518"
name = "iShares Core MSCI World UCITS ETF"
domicile = "IE"
reporting_status = "reporting"
asset_class = "equity-etf"
first_acquired = 2018-03-15
"""


def _tx(**kw: object) -> Transaction:
    base: dict[str, object] = dict(
        trade_date=date(2025, 6, 1), narration="x", currency="GBP",
        amount=Decimal("0"), source_path=Path("t.pdf"),
    )
    base.update(kw)
    return Transaction(**base)  # type: ignore[arg-type]


def _build_ledger(data_dir: Path) -> None:
    rep = "IE00B3VWN518"
    txns = [
        _tx(document_type=DocumentType.BUY_ETF, isin=rep, quantity=Decimal("100"),
            amount=Decimal("-1000"), trade_date=date(2024, 5, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=rep, quantity=Decimal("-100"),
            amount=Decimal("5000"), trade_date=date(2025, 6, 1)),
        _tx(document_type=DocumentType.DIVIDEND_NOTICE, isin="US0378331005",
            title="Dividend", currency="USD", amount=Decimal("85.00"),
            booking_date=date(2025, 9, 1), trade_date=date(2025, 9, 1),
            gbp_rate=Decimal("0.80"), gross_income=Decimal("100.00"),
            withholding_tax=Decimal("15.00"), withholding_country="US"),
    ]
    dump_transactions(txns, data_dir / "2025.transactions.jsonl")


def test_tax_pack_end_to_end(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "tax-pack", "--year", "2025-26", "--source", str(data_dir),
            "--out", str(out_dir), "--commodities", str(commodities),
            "--rate-source", "null",
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out_dir / "tax-pack.md").read_text(encoding="utf-8")
    assert "# UK tax pack — 2025-26" in md
    assert "| 24 | Disposal proceeds | £5,000.00 |" in md
    # Gain 4,000 − 3,000 AEA = 1,000 taxable.
    assert "Taxable gain: £1,000.00" in md
    assert "Foreign tax (for Foreign Tax Credit Relief): £12.00" in md


def test_tax_pack_fig_claim_renders_designation(
    tmp_path: Path, monkeypatch: object
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli.settings, "uk_residence_start_date", date(2025, 4, 6)
    )
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli.settings, "fig_claim_years", frozenset({"2025-26"})
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "tax-pack", "--year", "2025-26", "--source", str(data_dir),
            "--out", str(out_dir), "--commodities", str(commodities),
            "--rate-source", "null",
        ],
    )
    assert result.exit_code == 0, result.output
    md = (out_dir / "tax-pack.md").read_text(encoding="utf-8")
    # The foreign IE disposal is relieved → FIG designation, not SA108.
    assert "## Foreign Income & Gains (FIG) claim — SA109" in md
    assert "IE00B3VWN518" in md.split("FIG) claim")[1]


def test_tax_pack_pre_residence_year_skipped(
    tmp_path: Path, monkeypatch: object
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)
    monkeypatch.setattr(  # type: ignore[attr-defined]
        cli.settings, "uk_residence_start_date", date(2025, 4, 6)
    )
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["tax-pack", "--year", "2024-25", "--source", str(data_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "before UK residence began" in result.output

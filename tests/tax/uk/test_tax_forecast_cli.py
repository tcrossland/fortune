"""End-to-end ``tax-forecast`` CLI against a synthetic sidecar ledger."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.transaction_sidecar import dump_transactions, load_transactions

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
        trade_date=date(2025, 6, 1),
        narration="x",
        currency="GBP",
        amount=Decimal("0"),
        source_path=Path("t.pdf"),
    )
    base.update(kw)
    return Transaction(**base)  # type: ignore[arg-type]


def _build_ledger(data_dir: Path) -> None:
    rep = "IE00B3VWN518"
    txns = [
        # buy 100 @ 1,000, sell 100 @ 5,000 in-year → gain 4,000.
        _tx(document_type=DocumentType.BUY_ETF, isin=rep, quantity=Decimal("100"),
            amount=Decimal("-1000"), trade_date=date(2024, 5, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=rep, quantity=Decimal("-100"),
            amount=Decimal("5000"), trade_date=date(2025, 6, 1)),
        # foreign WHT dividend (US, USD) in-year.
        _tx(document_type=DocumentType.DIVIDEND_NOTICE, isin="US0378331005",
            title="Dividend", currency="USD", amount=Decimal("85.00"),
            booking_date=date(2025, 9, 1), trade_date=date(2025, 9, 1),
            gbp_rate=Decimal("0.80"), gross_income=Decimal("100.00"),
            withholding_tax=Decimal("15.00"), withholding_country="US"),
    ]
    dump_transactions(txns, data_dir / "2025.transactions.jsonl")


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_tax_forecast_end_to_end(tmp_path: Path) -> None:
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
            "tax-forecast",
            "--year", "2025-26",
            "--income", "60000",
            "--source", str(data_dir),
            "--out", str(out_dir),
            "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output

    rows = {r["component"]: r for r in _read_csv(out_dir / "forecast.csv")}
    # Gain 4,000 − 3,000 AEA = 1,000 taxable; 60k income exhausts the
    # basic band, so CGT is 1,000 * 24% = 240.
    assert rows["capital gains"]["taxable_gbp"] == "1000.00"
    assert rows["capital gains"]["tax_gbp"] == "240.00"
    # Foreign dividend: 100 USD * 0.80 = 80 gross, higher-rate, under the
    # 500 allowance → no UK tax, so no FTCR either.
    assert rows["foreign dividends"]["taxable_gbp"] == "0.00"
    assert rows["foreign dividends"]["tax_gbp"] == "0.00"

    summary = (out_dir / "forecast-summary.txt").read_text(encoding="utf-8")
    assert "UK tax-liability forecast — 2025-26" in summary
    assert "ESTIMATED TOTAL LIABILITY" in summary


def test_tax_forecast_excludes_isa(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)
    isa = [
        tx.model_copy(update={"account_wrapper": "isa"})
        for tx in load_transactions(data_dir / "2025.transactions.jsonl")
    ]
    dump_transactions(isa, data_dir / "2025.transactions.jsonl")
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "tax-forecast",
            "--year", "2025-26",
            "--income", "60000",
            "--source", str(data_dir),
            "--out", str(out_dir),
            "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output
    rows = {r["component"]: r for r in _read_csv(out_dir / "forecast.csv")}
    # Everything is ISA-sheltered → no gains, no dividends.
    assert rows["capital gains"]["taxable_gbp"] == "0.00"
    assert rows["capital gains"]["tax_gbp"] == "0.00"
    assert rows["foreign dividends"]["taxable_gbp"] == "0.00"


def test_tax_forecast_fig_recommends_cheaper(
    tmp_path: Path, monkeypatch: object
) -> None:
    # FIG-eligible year: a tiny foreign gain doesn't justify forfeiting the
    # personal allowance, so "no claim" should be recommended.
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
        cli.settings, "fig_claim_years", frozenset()
    )

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "tax-forecast", "--year", "2025-26", "--income", "60000",
            "--source", str(data_dir), "--out", str(out_dir),
            "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output
    summary = (out_dir / "forecast-summary.txt").read_text(encoding="utf-8")
    assert "FIG claim decision" in summary
    assert "with claim:" in summary
    assert "without claim:" in summary
    assert "RECOMMENDED: no claim" in summary


def test_tax_forecast_pre_residence_year_skipped(
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
        ["tax-forecast", "--year", "2024-25", "--income", "60000",
         "--source", str(data_dir)],
    )
    assert result.exit_code == 0, result.output
    assert "before UK residence began" in result.output


def test_tax_forecast_warns_on_missing_rate_and_strict_fails(
    tmp_path: Path,
) -> None:
    # A USD disposal with no per-tx gbp_rate and no rate source can't be
    # converted → it's excluded (understating the estimate), so it must be
    # warned about, and --strict must fail.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rep = "IE00B3VWN518"
    txns = [
        _tx(document_type=DocumentType.BUY_ETF, isin=rep, quantity=Decimal("100"),
            amount=Decimal("-1000"), currency="USD", trade_date=date(2024, 5, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=rep, quantity=Decimal("-100"),
            amount=Decimal("1500"), currency="USD", trade_date=date(2025, 6, 1)),
    ]
    dump_transactions(txns, data_dir / "2025.transactions.jsonl")
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    runner = CliRunner()
    # Force the null rate source so the USD trade can't be converted,
    # regardless of any local .env HMRC configuration.
    args = [
        "tax-forecast", "--year", "2025-26", "--income", "60000",
        "--source", str(data_dir), "--out", str(out_dir),
        "--commodities", str(commodities), "--rate-source", "null",
    ]
    result = runner.invoke(cli.app, args)
    assert result.exit_code == 0, result.output
    summary = (out_dir / "forecast-summary.txt").read_text(encoding="utf-8")
    assert "missing GBP rate" in summary
    assert "USD 2024-05" in summary

    strict = runner.invoke(cli.app, [*args, "--strict"])
    assert strict.exit_code == 1


def test_tax_forecast_unknown_year_errors(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["tax-forecast", "--year", "2099-00", "--income", "60000",
         "--source", str(data_dir)],
    )
    assert result.exit_code != 0

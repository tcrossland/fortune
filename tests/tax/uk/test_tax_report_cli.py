"""End-to-end ``tax-report`` CLI against a synthetic sidecar ledger."""

from __future__ import annotations

import csv
from datetime import date
from decimal import Decimal
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.transaction_sidecar import dump_transactions, load_transactions


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
    """One reporting ISIN (acquire + same-day partial + pool disposal),
    one non-reporting disposal, one unknown-metadata disposal, and a
    foreign WHT dividend — written as a sidecar."""

    rep = "IE00B3VWN518"  # reporting (in commodities.toml below)
    non = "LU1287023185"  # non-reporting (in commodities.toml)
    unk = "US0378331005"  # no metadata → unknown

    txns = [
        # reporting: buy 100 @ 1000 (2024), sell 100 @ 1500 (in-year) → gain 500
        _tx(document_type=DocumentType.BUY_ETF, isin=rep, quantity=Decimal("100"),
            amount=Decimal("-1000"), trade_date=date(2024, 5, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=rep, quantity=Decimal("-100"),
            amount=Decimal("1500"), trade_date=date(2025, 6, 1)),
        # non-reporting: buy 10 @ 100, sell 10 @ 250 → gain 150 (offshore)
        _tx(document_type=DocumentType.BUY_ETF, isin=non, quantity=Decimal("10"),
            amount=Decimal("-100"), trade_date=date(2024, 1, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=non, quantity=Decimal("-10"),
            amount=Decimal("250"), trade_date=date(2025, 7, 1)),
        # unknown metadata: buy 5 @ 50, sell 5 @ 40 → loss -10, status unknown
        _tx(document_type=DocumentType.BUY_SHARES, isin=unk, quantity=Decimal("5"),
            amount=Decimal("-50"), trade_date=date(2024, 2, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=unk, quantity=Decimal("-5"),
            amount=Decimal("40"), trade_date=date(2025, 8, 1)),
        # foreign WHT dividend (US, USD) in-year
        _tx(document_type=DocumentType.DIVIDEND_NOTICE, isin=unk,
            title="Dividend", currency="USD", amount=Decimal("85.00"),
            booking_date=date(2025, 9, 1), trade_date=date(2025, 9, 1),
            gbp_rate=Decimal("0.80"), gross_income=Decimal("100.00"),
            withholding_tax=Decimal("15.00"), withholding_country="US"),
    ]
    dump_transactions(txns, data_dir / "2025.transactions.jsonl")


_COMMODITIES = """
[[commodity]]
isin = "IE00B3VWN518"
name = "iShares Core MSCI World UCITS ETF"
domicile = "IE"
reporting_status = "reporting"
asset_class = "equity-etf"
first_acquired = 2018-03-15

[[commodity]]
isin = "LU1287023185"
name = "Amundi Bond ETF"
domicile = "LU"
reporting_status = "non-reporting"
asset_class = "bond"
first_acquired = 2021-06-01
"""


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def test_tax_report_end_to_end(tmp_path: Path) -> None:
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
            "tax-report",
            "--year", "2025-26",
            "--source", str(data_dir),
            "--out", str(out_dir),
            "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output

    # --- SA108: only reporting / uk-domestic disposals -------------------
    sa108 = _read_csv(out_dir / "sa108-disposals.csv")
    assert len(sa108) == 1
    row = sa108[0]
    assert row["isin"] == "IE00B3VWN518"
    assert row["reporting_status"] == "reporting"
    assert row["gain_gbp"] == "500.00"
    assert row["match_type"] == "s104"
    # 2025-26 has no rate-change date → period column present but empty.
    assert row["period"] == ""

    # --- SA106: foreign WHT dividend converted to GBP --------------------
    sa106 = _read_csv(out_dir / "sa106-dividends.csv")
    assert len(sa106) == 1
    div = sa106[0]
    assert div["country"] == "US"
    assert div["gross_gbp"] == "80.00"
    assert div["wht_gbp"] == "12.00"

    # --- offshore income gains: non-reporting fund disposal --------------
    oig = _read_csv(out_dir / "sa106-offshore-income-gains.csv")
    assert len(oig) == 1
    assert oig[0]["isin"] == "LU1287023185"
    assert oig[0]["gain_gbp"] == "150.00"
    # The non-reporting disposal must NOT appear on the CGT (SA108) file.
    assert all(r["isin"] != "LU1287023185" for r in sa108)

    # --- summary reports offshore + flags unclassified -------------------
    summary = (out_dir / "summary.txt").read_text(encoding="utf-8")
    assert "SA106 offshore income gains" in summary
    assert "WARN_UNCLASSIFIED" in summary  # unknown US0378331005 disposal
    assert "US0378331005" in summary

    # --- CGT allowances: 500 gain is under the 3,000 AEA → nothing taxable
    assert "CGT allowances and loss relief" in summary
    assert "taxable gain: 0.00 GBP" in summary
    cf = _read_csv(out_dir / "cgt-loss-carryforward.csv")
    assert len(cf) == 1
    assert cf[0]["tax_year"] == "2025-26"
    assert cf[0]["taxable_total"] == "0.00"
    assert cf[0]["annual_exempt_amount"] == "3000.00"
    assert cf[0]["losses_carried_forward"] == "0.00"


def test_isa_wrapped_transactions_excluded_from_tax_report(tmp_path: Path) -> None:
    """An ISA (``account_wrapper="isa"``) is tax-free: its disposals must
    not reach SA108 and its dividends must not reach SA106. Same ledger as
    the end-to-end test, but every transaction is ISA-wrapped, so every
    report comes back empty."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)

    # Re-stamp the synthetic ledger as ISA-held and rewrite the sidecar.
    isa_txns = [
        tx.model_copy(update={"account_wrapper": "isa"})
        for tx in load_transactions(data_dir / "2025.transactions.jsonl")
    ]
    dump_transactions(isa_txns, data_dir / "2025.transactions.jsonl")

    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "tax-report",
            "--year", "2025-26",
            "--source", str(data_dir),
            "--out", str(out_dir),
            "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output

    # Nothing sheltered should surface on any schedule.
    assert _read_csv(out_dir / "sa108-disposals.csv") == []
    assert _read_csv(out_dir / "sa106-dividends.csv") == []
    assert _read_csv(out_dir / "sa106-offshore-income-gains.csv") == []

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


def test_summary_flags_expired_loss_claim(tmp_path: Path) -> None:
    """The summary renders a WARN_LOSS_CLAIM_WINDOW block when a
    brought-forward loss was relieved past its 4-year notification window."""

    import dataclasses

    from banking_pipeline.tax.uk.cgt_allowance import (
        LossClaimWarning,
        apply_cgt_allowances,
    )
    from banking_pipeline.tax.uk.eri import EriResult
    from banking_pipeline.tax.uk.sa106 import Sa106Report
    from banking_pipeline.tax.uk.sa108 import Sa108Report

    allowance = dataclasses.replace(
        apply_cgt_allowances(
            tax_year="2025-26", gains_pre=Decimal("8000"), gains_post=Decimal(0),
            current_year_losses=Decimal(0), brought_forward=Decimal("5000"),
            annual_exempt_amount=Decimal(0), rate_split=False,
        ),
        expired_loss_claims=(
            LossClaimWarning(
                arising_year="2020-21", deadline=date(2025, 4, 5),
                amount_used=Decimal("5000"), used_in_year="2025-26",
            ),
        ),
    )
    out = tmp_path / "summary.txt"
    cli._write_tax_summary(
        out, "2025-26", Sa108Report(rows=[]), Sa106Report(dividends=[]),
        EriResult(rows=[]), allowance,
    )
    summary = out.read_text(encoding="utf-8")
    assert "WARN_LOSS_CLAIM_WINDOW" in summary
    assert "loss from 2020-21" in summary
    assert "5 Apr 2025" in summary


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


def test_tax_report_fig_claim_relieves_foreign_to_designation(
    tmp_path: Path, monkeypatch: object
) -> None:
    """Under a FIG claim, foreign disposals/income move off SA108/SA106
    onto fig-designation.csv; UK-situs items stay."""

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
            "tax-report", "--year", "2025-26", "--source", str(data_dir),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output

    # The foreign reporting disposal (IE) is relieved → SA108 is empty.
    assert _read_csv(out_dir / "sa108-disposals.csv") == []
    # The non-reporting offshore gain (LU) is relieved → OIG is empty.
    assert _read_csv(out_dir / "sa106-offshore-income-gains.csv") == []

    designation = _read_csv(out_dir / "fig-designation.csv")
    by_isin = {r["isin"]: r for r in designation}
    assert by_isin["IE00B3VWN518"]["category"] == "capital gain"
    assert by_isin["IE00B3VWN518"]["kind"] == "gain"
    assert by_isin["IE00B3VWN518"]["amount_gbp"] == "500.00"
    assert by_isin["LU1287023185"]["category"] == "offshore income gain"
    assert by_isin["LU1287023185"]["kind"] == "gain"
    assert by_isin["LU1287023185"]["amount_gbp"] == "150.00"
    assert by_isin["US0378331005"]["category"] == "foreign dividend"
    assert by_isin["US0378331005"]["kind"] == "income"

    summary = (out_dir / "summary.txt").read_text(encoding="utf-8")
    assert "Foreign Income & Gains (FIG) claim" in summary
    # 80 dividend income, 500 + 150 gains, no foreign loss here.
    assert "foreign income relieved: 80.00 GBP" in summary
    assert "non-UK gains relieved: 650.00 GBP" in summary
    assert "disallowed foreign losses (loss relief forfeited): 0.00 GBP" in summary
    # The unclassified US0378331005 disposal defaults to UK situs under the
    # claim → flagged as possibly missing relief.
    assert "WARN_UNCLASSIFIED" in summary
    assert "MISSING RELIEF" in summary


def test_tax_report_fig_claim_surfaces_disallowed_loss(
    tmp_path: Path, monkeypatch: object
) -> None:
    """A foreign disposal at a loss is *disallowed* under a FIG claim (its
    loss relief is forfeited). It must be bucketed ``kind="loss"`` in the
    designation and reported as a separate subtotal in the summary, not
    netted silently into the relieved-gains figure."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    rep = "IE00B3VWN518"  # foreign (IE), reporting
    txns = [
        # buy 100 @ 1000 (pre-residence), sell 100 @ 700 in-year → loss -300
        _tx(document_type=DocumentType.BUY_ETF, isin=rep, quantity=Decimal("100"),
            amount=Decimal("-1000"), trade_date=date(2024, 5, 1)),
        _tx(document_type=DocumentType.SELL_ETF, isin=rep, quantity=Decimal("-100"),
            amount=Decimal("700"), trade_date=date(2025, 6, 1)),
        # a foreign WHT dividend so there's relieved income alongside
        _tx(document_type=DocumentType.DIVIDEND_NOTICE, isin="US0378331005",
            title="Dividend", currency="USD", amount=Decimal("85.00"),
            booking_date=date(2025, 9, 1), trade_date=date(2025, 9, 1),
            gbp_rate=Decimal("0.80"), gross_income=Decimal("100.00"),
            withholding_tax=Decimal("15.00"), withholding_country="US"),
    ]
    dump_transactions(txns, data_dir / "2025.transactions.jsonl")
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
            "tax-report", "--year", "2025-26", "--source", str(data_dir),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output

    designation = _read_csv(out_dir / "fig-designation.csv")
    by_isin = {r["isin"]: r for r in designation}
    loss = by_isin[rep]
    assert loss["kind"] == "loss"
    assert loss["category"] == "capital gain"
    assert loss["amount_gbp"] == "-300.00"
    assert by_isin["US0378331005"]["kind"] == "income"

    summary = (out_dir / "summary.txt").read_text(encoding="utf-8")
    # The loss is surfaced separately, not netted into income or gains.
    assert "foreign income relieved: 80.00 GBP" in summary
    assert "non-UK gains relieved: 0.00 GBP" in summary
    assert (
        "disallowed foreign losses (loss relief forfeited): -300.00 GBP"
        in summary
    )
    assert "net foreign income + gains relieved: -220.00 GBP" in summary


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


# --- P0: --strict gates on silent-understatement modes ---------------------


def _run_tax_report(
    data_dir: Path, tmp_path: Path, *, strict: bool
) -> object:
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    args = [
        "tax-report", "--year", "2025-26",
        "--source", str(data_dir), "--out", str(tmp_path / "report"),
        "--commodities", str(commodities),
    ]
    if strict:
        args.append("--strict")
    return CliRunner().invoke(cli.app, args)


def test_strict_fails_on_unclassified_disposal(tmp_path: Path) -> None:
    # _build_ledger has an unknown-metadata disposal (US0378331005) that is
    # excluded from every figure — --strict must catch it; plain run must not.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _build_ledger(data_dir)

    lenient = _run_tax_report(data_dir, tmp_path, strict=False)
    assert lenient.exit_code == 0, lenient.output

    strict = _run_tax_report(data_dir, tmp_path, strict=True)
    assert strict.exit_code == 1, strict.output
    assert "unclassified disposal" in strict.output


def test_strict_fails_on_unmatched_zero_cost_disposal(tmp_path: Path) -> None:
    # A reporting ISIN sold with no prior acquisition → matched at zero cost.
    # reporting_status is known, so the *only* blocker is the unmatched leg.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dump_transactions(
        [_tx(document_type=DocumentType.SELL_ETF, isin="IE00B3VWN518",
             quantity=Decimal("-100"), amount=Decimal("1500"),
             trade_date=date(2025, 6, 1))],
        data_dir / "2025.transactions.jsonl",
    )

    assert _run_tax_report(data_dir, tmp_path, strict=False).exit_code == 0
    strict = _run_tax_report(data_dir, tmp_path, strict=True)
    assert strict.exit_code == 1, strict.output
    assert "unmatched disposal" in strict.output
    assert "zero cost" in strict.output


def test_strict_passes_on_a_clean_ledger(tmp_path: Path) -> None:
    # Reporting ISIN, fully matched, all-GBP (no rate gaps) → no blockers.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    dump_transactions(
        [
            _tx(document_type=DocumentType.BUY_ETF, isin="IE00B3VWN518",
                quantity=Decimal("100"), amount=Decimal("-1000"),
                trade_date=date(2024, 5, 1)),
            _tx(document_type=DocumentType.SELL_ETF, isin="IE00B3VWN518",
                quantity=Decimal("-100"), amount=Decimal("1500"),
                trade_date=date(2025, 6, 1)),
        ],
        data_dir / "2025.transactions.jsonl",
    )
    result = _run_tax_report(data_dir, tmp_path, strict=True)
    assert result.exit_code == 0, result.output


def test_unknown_status_disposal_excluded_from_all_figures(tmp_path: Path) -> None:
    # A disposal whose ISIN has no metadata (reporting_status unknown) must
    # not reach SA108, offshore income gains, OR the loss-carry-forward
    # chain — its £8,000 loss is neither taxed nor carried; only flagged.
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    unk = "US0378331005"  # absent from _COMMODITIES → unknown
    dump_transactions(
        [
            _tx(document_type=DocumentType.BUY_SHARES, isin=unk,
                quantity=Decimal("100"), amount=Decimal("-10000"),
                trade_date=date(2024, 1, 1)),
            _tx(document_type=DocumentType.SELL_ETF, isin=unk,
                quantity=Decimal("-100"), amount=Decimal("2000"),
                trade_date=date(2025, 6, 1)),  # loss -8000
        ],
        data_dir / "2025.transactions.jsonl",
    )
    commodities = tmp_path / "commodities.toml"
    commodities.write_text(_COMMODITIES, encoding="utf-8")
    out_dir = tmp_path / "report"

    result = CliRunner().invoke(
        cli.app,
        [
            "tax-report", "--year", "2025-26", "--source", str(data_dir),
            "--out", str(out_dir), "--commodities", str(commodities),
        ],
    )
    assert result.exit_code == 0, result.output

    # Excluded from CGT and offshore-income-gains.
    assert _read_csv(out_dir / "sa108-disposals.csv") == []
    assert _read_csv(out_dir / "sa106-offshore-income-gains.csv") == []
    # The £8,000 loss is NOT carried forward (would be 8000 if it leaked in).
    chain = _read_csv(out_dir / "cgt-loss-carryforward.csv")
    assert chain[0]["losses_carried_forward"] == "0.00"
    # Surfaced as a warning instead.
    summary = (out_dir / "summary.txt").read_text(encoding="utf-8")
    assert "WARN_UNCLASSIFIED" in summary and unk in summary

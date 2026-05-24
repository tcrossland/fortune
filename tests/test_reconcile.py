"""Statement-balance reconciliation.

The pure logic — parsing assertions and ``bean-check`` failure lines,
diffing, coverage-gap detection, rendering — is exercised without the
``bean-check`` binary by feeding canned text. One end-to-end test drives
the CLI against a temp ledger and is skipped when ``bean-check`` isn't
installed (mirroring how the wrapper degrades gracefully).
"""

from __future__ import annotations

import shutil
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import cli, reconcile
from banking_pipeline.reconcile import Status

runner = CliRunner()


# --- parse_assertions -----------------------------------------------------


def test_parse_assertions_records_line_numbers_and_skips_noise() -> None:
    text = (
        ";; header comment\n"
        "\n"
        "2026-01-01 balance Assets:Pic:K1:GBP  57909.10 GBP\n"
        "2026-01-01 balance Assets:Pic:K1:LU2601001147  2248.13866 LU2601001147\n"
        ";; trailing comment\n"
    )
    assertions = reconcile.parse_assertions(text)

    assert len(assertions) == 2
    gbp, fund = assertions
    assert gbp.date == "2026-01-01"
    assert gbp.account == "Assets:Pic:K1:GBP"
    assert gbp.quantity == Decimal("57909.10")
    assert gbp.commodity == "GBP"
    assert gbp.line == 3  # 1-based; lines 1-2 are comment + blank
    assert fund.line == 4
    assert fund.quantity == Decimal("2248.13866")


# --- parse_bean_check_failures --------------------------------------------

_FAILURE_OUTPUT = (
    "/proj/data/balances.beancount:4: Balance failed for "
    "'Assets:Pic:K1:GBP': expected 999.00 GBP != accumulated "
    "500.00000 GBP (499.00000 too little)\n"
    "\n"
    "   2024-04-01 balance Assets:Pic:K1:GBP                999.00 GBP\n"
    "\n"
    "/proj/data/balances.beancount:5: Balance failed for "
    "'Assets:Pic:K1:IE00BKRCQ001': expected 7.000 IE00BKRCQ001 != "
    "accumulated 10.000 IE00BKRCQ001 (3.000 too much)\n"
)


def test_parse_bean_check_failures_extracts_line_account_actual() -> None:
    failures = reconcile.parse_bean_check_failures(_FAILURE_OUTPUT)

    assert set(failures) == {4, 5}
    assert failures[4].account == "Assets:Pic:K1:GBP"
    assert failures[4].actual == Decimal("500.00000")
    assert failures[5].actual == Decimal("10.000")  # "too much" → over


def test_parse_bean_check_failures_basename_filter() -> None:
    # A failure from a different included file is ignored when filtering.
    assert reconcile.parse_bean_check_failures(
        _FAILURE_OUTPUT, balances_name="other.beancount"
    ) == {}
    assert set(
        reconcile.parse_bean_check_failures(
            _FAILURE_OUTPUT, balances_name="balances.beancount"
        )
    ) == {4, 5}


def test_parse_bean_check_failures_ignores_non_failure_lines() -> None:
    text = (
        "Some unrelated warning about prices\n"
        "/proj/data/balances.beancount:9: Some other error, not a balance\n"
    )
    assert reconcile.parse_bean_check_failures(text) == {}


# --- reconcile ------------------------------------------------------------


def _assertion(date: str, account: str, qty: str, line: int) -> reconcile.Assertion:
    return reconcile.Assertion(date, account, Decimal(qty), account.split(":")[-1], line)


def test_reconcile_marks_drift_and_ok() -> None:
    expected = [
        _assertion("2024-02-01", "Assets:Pic:K1:GBP", "500.00", 1),
        _assertion("2024-04-01", "Assets:Pic:K1:GBP", "999.00", 2),
    ]
    failures = {2: reconcile.Failure(2, "Assets:Pic:K1:GBP", Decimal("500.00"))}

    rows = reconcile.reconcile(expected, failures)

    ok, drift = rows
    assert ok.status is Status.OK
    assert ok.actual is None
    assert ok.diff is None
    assert drift.status is Status.DRIFT
    assert drift.actual == Decimal("500.00")
    assert drift.diff == Decimal("-499.00")  # actual - expected, ledger short


def test_reconcile_account_mismatch_guard_treats_as_ok() -> None:
    # A failure at the same line but a different account must not be
    # attributed to this assertion (defends against cross-file line
    # collisions when the basename filter is off).
    expected = [_assertion("2024-04-01", "Assets:Pic:K1:GBP", "999.00", 2)]
    failures = {2: reconcile.Failure(2, "Assets:Pic:K2:EUR", Decimal("0"))}

    (row,) = reconcile.reconcile(expected, failures)
    assert row.status is Status.OK


# --- coverage gaps --------------------------------------------------------


def test_find_coverage_gaps_detects_missing_month() -> None:
    # Assertion dates are one day after statement month-end, so
    # 2024-02-01 covers Jan, 2024-04-01 covers Mar → Feb is missing.
    assertions = [
        _assertion("2024-02-01", "Assets:Pic:K1:GBP", "1", 1),
        _assertion("2024-04-01", "Assets:Pic:K1:GBP", "1", 2),
    ]
    assert reconcile.find_coverage_gaps(assertions) == {"K1": ["2024-02"]}


def test_find_coverage_gaps_none_when_contiguous() -> None:
    assertions = [
        _assertion("2024-02-01", "Assets:Pic:K1:GBP", "1", 1),
        _assertion("2024-03-01", "Assets:Pic:K1:GBP", "1", 2),
    ]
    assert reconcile.find_coverage_gaps(assertions) == {}


def test_find_coverage_gaps_spans_year_boundary() -> None:
    # 2025-01-01 covers Dec 2024; 2025-03-01 covers Feb 2025 →
    # Jan 2025 missing across the year boundary.
    assertions = [
        _assertion("2025-01-01", "Assets:Pic:K1:GBP", "1", 1),
        _assertion("2025-03-01", "Assets:Pic:K1:GBP", "1", 2),
    ]
    assert reconcile.find_coverage_gaps(assertions) == {"K1": ["2025-01"]}


# --- earliest drift -------------------------------------------------------


def test_earliest_drift_picks_first_date_per_account() -> None:
    rows = [
        reconcile.ReconRow(
            "2024-04-01", "Assets:Pic:K1:GBP", "GBP",
            Decimal("1"), Decimal("0"), Status.DRIFT,
        ),
        reconcile.ReconRow(
            "2024-02-01", "Assets:Pic:K1:GBP", "GBP",
            Decimal("1"), Decimal("0"), Status.DRIFT,
        ),
    ]
    assert reconcile.earliest_drift(rows) == {"Assets:Pic:K1:GBP": "2024-02-01"}


# --- rendering ------------------------------------------------------------


def test_render_csv_emits_all_rows_with_status() -> None:
    expected = [
        _assertion("2024-02-01", "Assets:Pic:K1:GBP", "500.00", 1),
        _assertion("2024-04-01", "Assets:Pic:K1:GBP", "999.00", 2),
    ]
    failures = {2: reconcile.Failure(2, "Assets:Pic:K1:GBP", Decimal("500.00"))}
    report = reconcile.build_report(expected, failures)

    csv_text = reconcile.render_csv(report)
    lines = csv_text.strip().splitlines()
    assert lines[0] == "date,account,commodity,expected,actual,diff,status"
    assert lines[1].endswith(",ok")
    assert lines[2].endswith(",drift")
    assert "-499.00" in lines[2]


def test_build_report_has_drift_flag() -> None:
    expected = [_assertion("2024-04-01", "Assets:Pic:K1:GBP", "999.00", 1)]
    clean = reconcile.build_report(expected, {})
    assert clean.has_drift is False
    assert clean.ok_count == 1

    drifted = reconcile.build_report(
        expected, {1: reconcile.Failure(1, "Assets:Pic:K1:GBP", Decimal("0"))}
    )
    assert drifted.has_drift is True


# --- end-to-end CLI -------------------------------------------------------

_LEDGER = """\
option "operating_currency" "GBP"
option "booking_method" "FIFO"
2024-01-01 open Assets:Pic:K1:GBP
2024-01-01 open Equity:Opening

2024-01-15 * "deposit"
  Assets:Pic:K1:GBP   1000.00 GBP
  Equity:Opening

include "balances.beancount"
"""

_BALANCES = """\
;; assertions
2024-02-01 balance Assets:Pic:K1:GBP  1000.00 GBP
2024-04-01 balance Assets:Pic:K1:GBP  999.00 GBP
"""


@pytest.mark.skipif(
    shutil.which("bean-check") is None, reason="bean-check binary not installed"
)
def test_cli_reconcile_detects_drift(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.beancount"
    balances = tmp_path / "balances.beancount"
    out = tmp_path / "out"
    ledger.write_text(_LEDGER, encoding="utf-8")
    balances.write_text(_BALANCES, encoding="utf-8")

    result = runner.invoke(
        cli.app,
        [
            "reconcile",
            str(ledger),
            "--balances",
            str(balances),
            "--output",
            str(out),
        ],
    )

    # The 2024-04-01 assertion (999.00) drifts from the ledger's 1000.00,
    # so the command exits nonzero and writes the report.
    assert result.exit_code == 1
    summary = (out / "summary.txt").read_text(encoding="utf-8")
    assert "DRIFT" in summary
    assert "Assets:Pic:K1:GBP" in summary
    drift_csv = (out / "drift.csv").read_text(encoding="utf-8")
    assert ",drift" in drift_csv
    assert ",ok" in drift_csv  # the 2024-02-01 assertion reconciles


@pytest.mark.skipif(
    shutil.which("bean-check") is None, reason="bean-check binary not installed"
)
def test_cli_reconcile_clean_ledger_exits_zero(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.beancount"
    balances = tmp_path / "balances.beancount"
    ledger.write_text(_LEDGER, encoding="utf-8")
    # Only the assertion that matches the ledger's actual balance.
    balances.write_text(
        ";; assertions\n2024-02-01 balance Assets:Pic:K1:GBP  1000.00 GBP\n",
        encoding="utf-8",
    )

    result = runner.invoke(
        cli.app,
        ["reconcile", str(ledger), "--balances", str(balances), "--output", str(tmp_path / "out")],
    )
    assert result.exit_code == 0

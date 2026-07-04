"""CLI tests for ``banking-pipeline completeness``.

End-to-end through the command wiring: ``_statement_text`` reads a ``.txt``
verbatim, so a synthetic statement text plus a synthetic
``*.transactions.jsonl`` sidecar exercises discovery, parse, diff, render,
and exit codes without any PDF. All figures are invented.
"""

from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli

# A one-currency current-account statement: a deposit and a subscription,
# internally consistent so the parser's running-balance self-check passes.
_STATEMENT = """\
Financial statement in EUR
K-999999.001

Current account statement in EUR
K-999999.001.00.EUR

From 1 January 2099 to 31 December 2099

01.01.2099 Balance carried forward 0.00
05.01.2099 Bonificación 05.01.2099 100'000.00 ^ 100'000.00
10.02.2099 Suscripción 100 ACME 12.02.2099 40'000.00 60'000.00
"""


def _sidecar(rows: list[dict[str, object]]) -> str:
    header = json.dumps({"_schema": "banking-pipeline/transactions/v4"})
    return "\n".join([header, *(json.dumps(r) for r in rows)]) + "\n"


def _write_case(tmp_path: Path, sidecar_rows: list[dict[str, object]]) -> Path:
    data = tmp_path / "data"
    data.mkdir()
    (data / "2099-K.transactions.jsonl").write_text(
        _sidecar(sidecar_rows), encoding="utf-8"
    )
    (tmp_path / "Financial-statement-20991231.txt").write_text(
        _STATEMENT, encoding="utf-8"
    )
    return data


def _run(tmp_path: Path, *extra: str) -> object:
    return CliRunner().invoke(
        cli.app,
        [
            "completeness",
            "--statement",
            str(tmp_path / "Financial-statement-20991231.txt"),
            "--source",
            str(tmp_path / "data"),
            "--out",
            str(tmp_path / "out"),
            *extra,
        ],
    )


_COMPLETE = [
    {
        "document_type": "pago_interna",
        "account_number": "K-999999.001",
        "currency": "EUR",
        "settlement_date": "2099-01-05",
        "amount": "100000.00",
    },
    {
        "document_type": "suscripcion",
        "account_number": "K-999999.001",
        "currency": "EUR",
        "settlement_date": "2099-02-12",
        "amount": "-40000.00",
    },
]


# Output files are keyed by ``<portfolio>-<period-end>`` (here the
# placeholder K999999001 + 2099-12-31), so successive runs — or two
# portfolios sharing a period — don't clobber.
_SUMMARY = "summary-K999999001-2099-12-31.txt"
_FINDINGS = "findings-K999999001-2099-12-31.csv"


def test_clean_diff_exits_zero(tmp_path: Path) -> None:
    _write_case(tmp_path, _COMPLETE)
    result = _run(tmp_path)
    assert result.exit_code == 0, result.output
    assert "0 missing, 0 unmatched" in result.output
    summary = (tmp_path / "out" / _SUMMARY).read_text()
    assert "matched 2 line(s)" in summary
    assert (tmp_path / "out" / _FINDINGS).exists()


def test_missing_advice_exits_nonzero(tmp_path: Path) -> None:
    """Drop the subscription from the sidecar — its statement line has no
    ingested advice, so the command reports MISSING and exits non-zero."""

    _write_case(tmp_path, _COMPLETE[:1])
    result = _run(tmp_path)
    assert result.exit_code == 1, result.output
    summary = (tmp_path / "out" / _SUMMARY).read_text()
    assert "MISSING IN LEDGER (1)" in summary
    assert "Suscripción 100 ACME" in summary
    csv_text = (tmp_path / "out" / _FINDINGS).read_text()
    assert "missing_in_ledger" in csv_text


def test_unmatched_only_fails_under_strict(tmp_path: Path) -> None:
    """An in-period sidecar event with no statement line is UNMATCHED: a
    clean exit by default, non-zero under --strict."""

    extra_event = {
        "document_type": "suscripcion",
        "account_number": "K-999999.001",
        "currency": "EUR",
        "settlement_date": "2099-03-01",
        "amount": "-7777.00",
        "narration": "ghost buy",
    }
    _write_case(tmp_path, [*_COMPLETE, extra_event])

    assert _run(tmp_path).exit_code == 0
    strict = _run(tmp_path, "--strict")
    assert strict.exit_code == 1, strict.output
    assert "UNMATCHED IN LEDGER (1)" in (tmp_path / "out" / _SUMMARY).read_text()


def test_other_portfolio_rows_ignored(tmp_path: Path) -> None:
    """A sidecar row for a different portfolio must not satisfy this
    statement's lines — otherwise a same-amount coincidence would mask a
    genuine gap."""

    wrong_portfolio = [
        {**row, "account_number": "P-123456.002"} for row in _COMPLETE
    ]
    _write_case(tmp_path, wrong_portfolio)
    result = _run(tmp_path)
    assert result.exit_code == 1, result.output
    assert "MISSING IN LEDGER (2)" in (tmp_path / "out" / _SUMMARY).read_text()


def test_two_statements_write_separate_files(tmp_path: Path) -> None:
    """A second statement with a different period end lands in its own file
    rather than clobbering the first."""

    data = _write_case(tmp_path, _COMPLETE)
    other = tmp_path / "Financial-statement-20981231.txt"
    other.write_text(_STATEMENT.replace("2099", "2098"), encoding="utf-8")
    (data / "2098-K.transactions.jsonl").write_text(
        _sidecar([{**r, "settlement_date": r["settlement_date"].replace("2099", "2098")}
                  for r in _COMPLETE]),
        encoding="utf-8",
    )

    result = CliRunner().invoke(
        cli.app,
        [
            "completeness",
            "--statement", str(tmp_path / "Financial-statement-20991231.txt"),
            "--statement", str(other),
            "--source", str(data),
            "--out", str(tmp_path / "out"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert (tmp_path / "out" / "summary-K999999001-2099-12-31.txt").exists()
    assert (tmp_path / "out" / "summary-K999999001-2098-12-31.txt").exists()


def test_discover_skips_superseded_cash_statements(tmp_path: Path) -> None:
    """Scanning a tree for statements must ignore keep-latest ``_superseded/``
    copies, else old cash-statement exports write stale duplicate reports."""

    from banking_pipeline.cli.reports import _discover_financial_statements

    cash = tmp_path / "cash-statements"
    (cash / "_superseded").mkdir(parents=True)
    live = cash / "Cash statement by value date 20260703.csv"
    live.write_text("x", encoding="utf-8")
    (cash / "_superseded" / "Cash statement by value date 20260630.csv").write_text(
        "x", encoding="utf-8"
    )
    assert _discover_financial_statements(tmp_path) == [live]

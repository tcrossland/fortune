"""The reports post-step wired into ``rebuild``.

Covers the ``[post.reports]`` config schema and the rebuild orchestration:
the analytical reports regenerate into the configured ``reports/`` dirs,
before reconcile/check, and dry-run only previews the step.
"""

from __future__ import annotations

import textwrap
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.batch_config import BatchConfig, PostSteps, ReportsStep
from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.transaction_sidecar import dump_transactions

runner = CliRunner()


# --- config schema --------------------------------------------------------


def test_reports_step_defaults() -> None:
    step = ReportsStep()
    assert step.enabled is False
    # Each report on by default once the step is enabled.
    assert step.income is True
    assert step.concentration is True
    assert step.net_worth is True
    assert step.allocation is True
    assert step.portfolio_allocation is True
    assert step.statements == []
    assert step.income_period == "tax-year"
    assert PostSteps().reports == ReportsStep()


def test_reports_step_parses_from_toml() -> None:
    cfg = BatchConfig.model_validate(
        {
            "sources": [{"label": "x", "glob": "nope/*.pdf"}],
            "post": {
                "prices": False,
                "portfolio": False,
                "reports": {
                    "enabled": True,
                    "concentration": False,
                    "income_period": "calendar",
                    "statements": ["~/stmts/*.pdf"],
                },
            },
        }
    )
    assert cfg.post.reports.enabled is True
    assert cfg.post.reports.concentration is False
    assert cfg.post.reports.income_period == "calendar"
    assert cfg.post.reports.statements == ["~/stmts/*.pdf"]


def test_reports_step_rejects_bad_period() -> None:
    with pytest.raises(ValidationError):
        ReportsStep.model_validate({"enabled": True, "income_period": "monthly"})


def test_reports_enabled_without_sources_rejected() -> None:
    # reports.enabled counts as "ingesting" — needs at least one source.
    with pytest.raises(ValidationError):
        BatchConfig.model_validate({"post": {"reports": {"enabled": True}}})


# --- rebuild integration --------------------------------------------------


def _project_with_sidecar(tmp_path: Path) -> Path:
    """A project whose data dir holds one dividend sidecar, with a rebuild
    config that runs only the income report (no statements needed)."""

    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    txs = [
        Transaction(
            trade_date=date(2025, 6, 1), booking_date=date(2025, 6, 1),
            narration="Dividend", title="Dividend", currency="GBP",
            amount=Decimal("50.00"), isin="GB00B16KPT44",
            document_type=DocumentType.DIVIDEND_NOTICE, source_path=Path("d.pdf"),
        )
    ]
    dump_transactions(txs, root / "data" / "x.transactions.jsonl")
    config = textwrap.dedent("""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "x"
        glob = "nope/*.pdf"

        [post]
        prices = false
        portfolio = false
        balances = false

        [post.reports]
        enabled = true
        income = true
        concentration = false
        net_worth = false
        allocation = false
        portfolio_allocation = false

        [post.check]
        enabled = false
    """)
    (root / "banking-pipeline.toml").write_text(config, encoding="utf-8")
    return root


def test_rebuild_writes_income_report(tmp_path: Path) -> None:
    root = _project_with_sidecar(tmp_path)
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "reports income" in flat
    md = root / "reports" / "income" / "income.md"
    csv = root / "reports" / "income" / "income.csv"
    assert md.exists()
    assert csv.exists()
    assert "# Income by source" in md.read_text(encoding="utf-8")
    # The disabled reports didn't write.
    assert not (root / "reports" / "concentration").exists()


def test_rebuild_reports_dry_run_previews_only(tmp_path: Path) -> None:
    root = _project_with_sidecar(tmp_path)
    result = runner.invoke(
        cli.app, ["rebuild", "--project-root", str(root), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "reports income" in flat
    # Nothing written on a dry run.
    assert not (root / "reports").exists()


# --- trial-balance in [post.reports] (B3) ---------------------------------


def test_reports_step_trial_balance_defaults_off() -> None:
    step = ReportsStep()
    assert step.trial_balance is False
    assert step.trial_balance_ledger == ""


def test_reports_step_parses_trial_balance() -> None:
    cfg = BatchConfig.model_validate(
        {
            "sources": [{"label": "x", "glob": "nope/*.pdf"}],
            "post": {
                "prices": False, "portfolio": False,
                "reports": {
                    "enabled": True, "income": False,
                    "trial_balance": True,
                    "trial_balance_ledger": "main.beancount",
                },
            },
        }
    )
    assert cfg.post.reports.trial_balance is True
    assert cfg.post.reports.trial_balance_ledger == "main.beancount"


def test_rebuild_trial_balance_skips_gracefully_on_bad_ledger(tmp_path: Path) -> None:
    # trial-balance is ledger-based (bean-query). A ledger that won't load
    # (or a missing binary) must warn + skip, never fail the rebuild.
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    config = textwrap.dedent("""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "x"
        glob = "nope/*.pdf"

        [post]
        prices = false
        portfolio = false
        balances = false

        [post.reports]
        enabled = true
        income = false
        concentration = false
        net_worth = false
        allocation = false
        portfolio_allocation = false
        trial_balance = true
        trial_balance_ledger = "does-not-exist.beancount"

        [post.check]
        enabled = false
    """)
    (root / "banking-pipeline.toml").write_text(config, encoding="utf-8")

    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "trial-balance" in flat
    assert "trial-balance skipped" in flat
    # Nothing written for the skipped report.
    assert not (root / "reports" / "trial-balance").exists()


# --- balance-sheet in [post.reports] --------------------------------------


def test_reports_step_balance_sheet_defaults_off() -> None:
    assert ReportsStep().balance_sheet is False


def test_reports_step_parses_balance_sheet() -> None:
    cfg = BatchConfig.model_validate(
        {
            "sources": [{"label": "x", "glob": "nope/*.pdf"}],
            "post": {
                "prices": False, "portfolio": False,
                "reports": {
                    "enabled": True, "income": False,
                    "balance_sheet": True,
                    "trial_balance_ledger": "main.beancount",
                },
            },
        }
    )
    assert cfg.post.reports.balance_sheet is True


def test_rebuild_balance_sheet_skips_gracefully_on_bad_ledger(tmp_path: Path) -> None:
    # Ledger-based (bean-query), like trial-balance: a missing ledger must
    # warn + skip, never fail the rebuild or write a partial artifact.
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    config = textwrap.dedent("""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "x"
        glob = "nope/*.pdf"

        [post]
        prices = false
        portfolio = false
        balances = false

        [post.reports]
        enabled = true
        income = false
        concentration = false
        net_worth = false
        allocation = false
        portfolio_allocation = false
        balance_sheet = true
        trial_balance_ledger = "does-not-exist.beancount"

        [post.check]
        enabled = false
    """)
    (root / "banking-pipeline.toml").write_text(config, encoding="utf-8")

    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])
    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "balance-sheet" in flat
    assert "balance-sheet skipped" in flat
    assert not (root / "reports" / "balance-sheet").exists()

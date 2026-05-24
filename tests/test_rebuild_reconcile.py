"""The reconcile post-step wired into ``rebuild``.

Covers the ``[post.reconcile]`` config schema and the rebuild
orchestration: reconcile runs before ``check``, writes its report, and
gates the rebuild on drift / coverage gaps. The integration tests drive
the real ``rebuild`` command and are skipped when ``bean-check`` isn't
installed (the step degrades to a warning without it).
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.batch_config import BatchConfig, PostSteps, ReconcileStep

runner = CliRunner()


# --- config schema --------------------------------------------------------


def test_reconcile_step_defaults() -> None:
    step = ReconcileStep()
    assert step.enabled is False
    assert step.ledger == ""
    assert step.balances == ""
    assert step.strict is False
    # Present on PostSteps with the same disabled default.
    assert PostSteps().reconcile == ReconcileStep()


def test_reconcile_step_parses_from_toml() -> None:
    cfg = BatchConfig.model_validate(
        {
            "post": {
                "prices": False,
                "portfolio": False,
                "reconcile": {
                    "enabled": True,
                    "ledger": "main.beancount",
                    "balances": "data/balances.beancount",
                    "strict": True,
                },
            }
        }
    )
    assert cfg.post.reconcile.enabled is True
    assert cfg.post.reconcile.ledger == "main.beancount"
    assert cfg.post.reconcile.strict is True


def test_reconcile_step_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        ReconcileStep.model_validate({"enabled": True, "bogus": 1})


# --- rebuild integration --------------------------------------------------

_PORTFOLIO = """\
option "operating_currency" "GBP"
option "booking_method" "FIFO"
2024-01-01 open Assets:Pic:K1:GBP
2024-01-01 open Equity:Opening
2024-01-15 * "deposit"
  Assets:Pic:K1:GBP   1000.00 GBP
  Equity:Opening
include "balances.beancount"
"""


def _project(tmp_path: Path, balances_body: str, *, strict: bool = False) -> Path:
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    (root / "data" / "portfolio.beancount").write_text(_PORTFOLIO, encoding="utf-8")
    (root / "data" / "balances.beancount").write_text(balances_body, encoding="utf-8")
    config = textwrap.dedent(f"""
        data_dir = "data"
        clean_glob = ""

        [post]
        prices = false
        portfolio = false
        balances = false

        [post.reconcile]
        enabled = true
        ledger = ""
        balances = ""
        strict = {str(strict).lower()}

        [post.check]
        enabled = true
        ledger = ""
    """)
    (root / "banking-pipeline.toml").write_text(config, encoding="utf-8")
    return root


@pytest.mark.skipif(
    shutil.which("bean-check") is None, reason="bean-check binary not installed"
)
def test_rebuild_reconcile_fails_on_drift(tmp_path: Path) -> None:
    # 1000.00 in the ledger, asserted 999.00 → drift.
    root = _project(
        tmp_path,
        ";; assertions\n2024-04-01 balance Assets:Pic:K1:GBP  999.00 GBP\n",
    )
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 1, result.output
    flat = " ".join(result.output.split())
    assert "reconcile" in flat
    assert "DRIFT" in flat
    # Report written under the project's reports/reconciliation.
    assert (root / "reports" / "reconciliation" / "summary.txt").exists()
    assert (root / "reports" / "reconciliation" / "drift.csv").exists()


@pytest.mark.skipif(
    shutil.which("bean-check") is None, reason="bean-check binary not installed"
)
def test_rebuild_reconcile_clean_then_check(tmp_path: Path) -> None:
    # Asserted balance matches the ledger → reconcile OK, then check runs.
    root = _project(
        tmp_path,
        ";; assertions\n2024-02-01 balance Assets:Pic:K1:GBP  1000.00 GBP\n",
    )
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    # Both steps ran: reconcile reported all-OK, then check passed.
    assert "OK: 1 of 1 assertions within tolerance" in flat
    assert "check" in flat


def test_rebuild_reconcile_dry_run_previews_step(tmp_path: Path) -> None:
    # Dry-run needs no binary: it only prints the planned steps.
    root = _project(
        tmp_path,
        ";; assertions\n2024-04-01 balance Assets:Pic:K1:GBP  999.00 GBP\n",
    )
    result = runner.invoke(
        cli.app, ["rebuild", "--project-root", str(root), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "reconcile" in flat
    # No report written on a dry run.
    assert not (root / "reports").exists()

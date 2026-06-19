"""The completeness post-step wired into ``rebuild``.

Covers the ``[post.completeness]`` config schema and the rebuild
orchestration: the cross-check runs before ``check``, writes its
per-statement reports, and gates the rebuild on findings (MISSING always;
UNMATCHED under strict). No ``bean-check`` binary needed — ``[post.check]``
is disabled, and the statement is a ``.txt`` (read verbatim) so no PDF
loader is exercised either.
"""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.batch_config import BatchConfig, CompletenessStep, PostSteps

runner = CliRunner()


# --- config schema --------------------------------------------------------


def test_completeness_step_defaults() -> None:
    step = CompletenessStep()
    assert step.enabled is False
    assert step.statements == []
    assert step.strict is False
    assert PostSteps().completeness == CompletenessStep()


def test_completeness_step_parses_from_toml() -> None:
    cfg = BatchConfig.model_validate(
        {
            "post": {
                "prices": False,
                "portfolio": False,
                "completeness": {
                    "enabled": True,
                    "statements": ["archive/**/Financial-statement-*.pdf"],
                    "strict": True,
                },
            }
        }
    )
    assert cfg.post.completeness.enabled is True
    assert cfg.post.completeness.statements == [
        "archive/**/Financial-statement-*.pdf"
    ]
    assert cfg.post.completeness.strict is True


def test_completeness_step_rejects_unknown_keys() -> None:
    with pytest.raises(ValidationError):
        CompletenessStep.model_validate({"enabled": True, "bogus": 1})


# --- rebuild integration --------------------------------------------------

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

_DEPOSIT = {
    "document_type": "pago_interna",
    "account_number": "K-999999.001",
    "currency": "EUR",
    "settlement_date": "2099-01-05",
    "amount": "100000.00",
}
_SUBSCRIPTION = {
    "document_type": "suscripcion",
    "account_number": "K-999999.001",
    "currency": "EUR",
    "settlement_date": "2099-02-12",
    "amount": "-40000.00",
}


def _project(tmp_path: Path, sidecar_rows: list[dict[str, object]]) -> Path:
    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    (root / "statements").mkdir()
    header = json.dumps({"_schema": "banking-pipeline/transactions/v4"})
    body = "\n".join([header, *(json.dumps(r) for r in sidecar_rows)]) + "\n"
    (root / "data" / "2099-K.transactions.jsonl").write_text(body, encoding="utf-8")
    (root / "statements" / "Financial-statement-20991231.txt").write_text(
        _STATEMENT, encoding="utf-8"
    )
    config = textwrap.dedent("""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "noop"
        glob = "no-such-dir/*.pdf"

        [post]
        prices = false
        portfolio = false
        balances = false

        [post.completeness]
        enabled = true
        statements = ["statements/Financial-statement-*.txt"]

        [post.check]
        enabled = false
    """)
    (root / "banking-pipeline.toml").write_text(config, encoding="utf-8")
    return root


def test_rebuild_completeness_clean(tmp_path: Path) -> None:
    root = _project(tmp_path, [_DEPOSIT, _SUBSCRIPTION])
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])
    assert result.exit_code == 0, result.output
    assert "completeness" in " ".join(result.output.split())
    assert (
        root / "reports" / "completeness" / "summary-K999999001-2099-12-31.txt"
    ).exists()


def test_rebuild_completeness_fails_on_missing(tmp_path: Path) -> None:
    # Drop the subscription advice → its statement line is MISSING in ledger,
    # which fails the rebuild even without --strict.
    root = _project(tmp_path, [_DEPOSIT])
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])
    assert result.exit_code == 1, result.output
    summary = (
        root / "reports" / "completeness" / "summary-K999999001-2099-12-31.txt"
    ).read_text()
    assert "MISSING IN LEDGER (1)" in summary


def test_rebuild_completeness_dry_run_previews_step(tmp_path: Path) -> None:
    root = _project(tmp_path, [_DEPOSIT])
    result = runner.invoke(
        cli.app, ["rebuild", "--project-root", str(root), "--dry-run"]
    )
    assert result.exit_code == 0, result.output
    assert "completeness" in " ".join(result.output.split())
    # No report written on a dry run.
    assert not (root / "reports").exists()

"""``run_bean_check`` invocation contract.

Beancount v3's ``bean-check`` has no warnings-as-errors flag — the v2-era
``-w`` was removed and passing it is a hard usage error (``rc=2``). These
tests pin that ``strict`` never adds ``-w`` (the regression that broke
``--strict --check`` under the v3 pin) and that a clean ledger still
validates.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from banking_pipeline import bean_check

_VALID_LEDGER = """\
2020-01-01 open Assets:Cash
2020-01-01 open Equity:Opening
2020-01-02 * "opening balance"
  Assets:Cash      10.00 USD
  Equity:Opening
"""


def test_strict_does_not_pass_w_flag(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = tmp_path / "x.beancount"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        bean_check, "find_bean_check", lambda: Path("/usr/bin/bean-check")
    )

    captured: dict[str, list[str]] = {}

    def _fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bean_check.subprocess, "run", _fake_run)

    result = bean_check.run_bean_check(ledger, strict=True)

    assert "-w" not in captured["cmd"]
    assert captured["cmd"][-1] == str(ledger)
    assert result.returncode == 0


def test_extra_args_passed_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ledger = tmp_path / "x.beancount"
    ledger.write_text("", encoding="utf-8")
    monkeypatch.setattr(
        bean_check, "find_bean_check", lambda: Path("/usr/bin/bean-check")
    )
    captured: dict[str, list[str]] = {}

    def _fake_run(cmd: list[str], **_kw: object) -> subprocess.CompletedProcess[str]:
        captured["cmd"] = cmd
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(bean_check.subprocess, "run", _fake_run)

    bean_check.run_bean_check(ledger, extra_args=("--auto",))
    assert "--auto" in captured["cmd"]


def test_strict_check_runs_clean_on_valid_ledger(tmp_path: Path) -> None:
    """End-to-end against the real binary: a valid ledger passes under
    strict (proves the ``-w`` removal — previously this returned rc=2)."""

    if bean_check.find_bean_check() is None:
        pytest.skip("bean-check not installed")

    ledger = tmp_path / "valid.beancount"
    ledger.write_text(_VALID_LEDGER, encoding="utf-8")

    result = bean_check.run_bean_check(ledger, strict=True)

    assert result.ok, result.stderr
    assert result.returncode == 0

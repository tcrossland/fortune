"""Settings sourcing — the [settings] TOML table + env precedence."""

from __future__ import annotations

from pathlib import Path

import pytest

from banking_pipeline.config import Settings

_TOML = (
    "[settings]\n"
    'gbp_rate_source = "hmrc-monthly"\n'
    "fig_claim_years = [\"2025-26\", \"2026-27\"]\n"
    "\n"
    "[settings.counterparty_account_map]\n"
    '"FOO LTD" = "External:Foo"\n'
)


@pytest.fixture(autouse=True)
def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (
        "BANKPIPE_GBP_RATE_SOURCE",
        "BANKPIPE_FIG_CLAIM_YEARS",
        "BANKPIPE_COUNTERPARTY_ACCOUNT_MAP",
    ):
        monkeypatch.delenv(var, raising=False)


def test_reads_settings_table(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "banking-pipeline.toml").write_text(_TOML, encoding="utf-8")

    s = Settings()
    assert s.gbp_rate_source == "hmrc-monthly"
    assert s.fig_claim_years == frozenset({"2025-26", "2026-27"})
    # A TOML table maps straight onto the dict field (no JSON-in-env).
    assert s.counterparty_account_map == {"FOO LTD": "External:Foo"}


def test_env_overrides_toml(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "banking-pipeline.toml").write_text(_TOML, encoding="utf-8")
    monkeypatch.setenv("BANKPIPE_GBP_RATE_SOURCE", "null")

    assert Settings().gbp_rate_source == "null"


def test_missing_toml_falls_back_to_defaults(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)  # no banking-pipeline.toml present
    s = Settings()
    assert s.gbp_rate_source == "null"
    assert s.counterparty_account_map == {
        "AEAT": "External:Tax:AEAT",
        "IBM": "External:Earnout:IBM",
    }


def test_rebuild_tables_ignored_by_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A file with only rebuild (BatchConfig) tables and no [settings]
    contributes nothing — Settings just uses defaults."""

    monkeypatch.chdir(tmp_path)
    (tmp_path / "banking-pipeline.toml").write_text(
        'data_dir = "data"\nclean_glob = "20*.beancount"\n', encoding="utf-8"
    )
    assert Settings().gbp_rate_source == "null"

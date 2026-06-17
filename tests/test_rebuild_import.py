"""The optional ``[import]`` pre-ingest step wired into ``rebuild``.

Covers the ``[import]`` config schema (defaults, TOML parsing via the
``import`` alias) and the rebuild orchestration: when enabled the step
files fresh downloads into the dated archive before the ingest globs run,
and a config that leaves it off (the default) moves no files.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.batch_config import BatchConfig, ImportStep

runner = CliRunner()


# --- config schema --------------------------------------------------------


def test_import_step_defaults() -> None:
    step = ImportStep()
    assert step.enabled is False
    assert step.source_glob == ""
    assert step.source_dir == ""
    assert step.archive_dir == ""
    assert step.pattern == "*.pdf"
    # A config that doesn't mention [import] carries a disabled step.
    cfg = BatchConfig.model_validate(
        {"sources": [{"label": "x", "glob": "nope/*.pdf"}]}
    )
    assert cfg.import_step == ImportStep()


def test_import_step_parses_from_toml_alias() -> None:
    # The TOML key is ``[import]`` (a Python keyword) — it maps to the
    # ``import_step`` attribute via the field alias.
    cfg = BatchConfig.model_validate(
        {
            "import": {
                "enabled": True,
                "source_glob": "~/Downloads/files-*.zip",
                "archive_dir": "~/Archive/Pictet",
            },
            "sources": [{"label": "x", "glob": "nope/*.pdf"}],
            "post": {"prices": False, "portfolio": False},
        }
    )
    assert cfg.import_step.enabled is True
    assert cfg.import_step.source_glob == "~/Downloads/files-*.zip"
    assert cfg.import_step.archive_dir == "~/Archive/Pictet"


def test_import_step_default_when_absent() -> None:
    cfg = BatchConfig.model_validate(
        {"sources": [{"label": "x", "glob": "nope/*.pdf"}]}
    )
    assert cfg.import_step.enabled is False


# --- rebuild integration --------------------------------------------------


def _project(tmp_path: Path, import_table: str) -> Path:
    """A minimal rebuild project with every downstream step disabled, so
    the test exercises only the import step."""

    root = tmp_path / "project"
    (root / "data").mkdir(parents=True)
    config = textwrap.dedent(
        f"""
        data_dir = "data"
        clean_glob = ""

        {import_table}

        [[sources]]
        label = "x"
        glob = "nope/*.pdf"

        [post]
        prices = false
        portfolio = false
        balances = false

        [post.check]
        enabled = false
        """
    )
    (root / "banking-pipeline.toml").write_text(config, encoding="utf-8")
    return root


def test_rebuild_runs_import_step_when_enabled(tmp_path: Path) -> None:
    # An empty source dir means the filing pass finds nothing — enough to
    # prove the step ran and reported, without needing PDF fixtures.
    incoming = tmp_path / "incoming"
    incoming.mkdir()
    archive_dir = tmp_path / "archive"
    import_table = textwrap.dedent(
        f"""
        [import]
        enabled = true
        source_dir = "{incoming}"
        archive_dir = "{archive_dir}"
        """
    )
    root = _project(tmp_path, import_table)

    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "import 1 source(s)" in flat
    assert "0 filed, 0 skipped, 0 unmatched, 0 error(s)" in flat


def test_rebuild_skips_import_step_by_default(tmp_path: Path) -> None:
    root = _project(tmp_path, "")
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "import" not in flat


def test_rebuild_import_enabled_but_unconfigured_warns(tmp_path: Path) -> None:
    # Enabled with no source/archive (and no import_* settings) → a
    # warning, not a crashed rebuild.
    root = _project(tmp_path, "[import]\nenabled = true")
    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "import skipped" in flat

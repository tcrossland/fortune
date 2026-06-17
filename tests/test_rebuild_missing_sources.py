"""The missing-source guard in ``rebuild``.

Protects against the data-loss footgun: a ``[[sources]]`` glob that
matches zero files (a moved or unsynced source) whose existing output the
clean step would delete and the ingest step couldn't regenerate. The
guard aborts *before* any deletion. A genuinely-new empty year (no output
yet) is unaffected and still just warns.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from typer.testing import CliRunner

from banking_pipeline import cli

runner = CliRunner()


def _project(tmp_path: Path, *, label: str, precreate_output: bool) -> Path:
    """A rebuild project with one source pointing at a nonexistent glob.

    When ``precreate_output`` is set, ``<data_dir>/<label>.beancount``
    exists (so the clean step would delete it — the dangerous case);
    otherwise it's absent (a new empty year — safe)."""

    root = tmp_path / "project"
    data = root / "data"
    data.mkdir(parents=True)
    if precreate_output:
        (data / f"{label}.beancount").write_text(
            "; existing ledger\n", encoding="utf-8"
        )
    config = textwrap.dedent(
        f"""
        data_dir = "data"
        clean_glob = "20*.beancount"

        [[sources]]
        label = "{label}"
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


def test_rebuild_aborts_when_clean_would_wipe_unregenerable_output(
    tmp_path: Path,
) -> None:
    root = _project(tmp_path, label="2025-P", precreate_output=True)
    output = root / "data" / "2025-P.beancount"

    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 2, result.output
    assert "matched zero files" in result.output
    # Crucially, nothing was deleted.
    assert output.exists()
    assert output.read_text(encoding="utf-8") == "; existing ledger\n"


def test_rebuild_allows_drop_with_flag(tmp_path: Path) -> None:
    root = _project(tmp_path, label="2025-P", precreate_output=True)
    output = root / "data" / "2025-P.beancount"

    result = runner.invoke(
        cli.app,
        ["rebuild", "--project-root", str(root), "--allow-missing-sources"],
    )

    assert result.exit_code == 0, result.output
    assert "proceeding past sources" in result.output
    # The stale output was deleted (clean ran) and not regenerated.
    assert not output.exists()


def test_rebuild_new_empty_year_is_not_flagged(tmp_path: Path) -> None:
    # No existing output → not in the clean set → just a zero-match warning.
    root = _project(tmp_path, label="2027-P", precreate_output=False)

    result = runner.invoke(cli.app, ["rebuild", "--project-root", str(root)])

    assert result.exit_code == 0, result.output
    assert "matched zero files" in result.output  # the ingest-loop warning
    assert "would delete" not in result.output


def test_rebuild_guard_also_fires_on_dry_run(tmp_path: Path) -> None:
    # The guard runs before clean, so a dry-run surfaces the danger too.
    root = _project(tmp_path, label="2025-P", precreate_output=True)
    output = root / "data" / "2025-P.beancount"

    result = runner.invoke(
        cli.app, ["rebuild", "--project-root", str(root), "--dry-run"]
    )

    assert result.exit_code == 2, result.output
    assert output.exists()

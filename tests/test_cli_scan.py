"""Tests for the ``scan`` subcommand.

The command walks a directory, classifies each ``*.pdf``, and prints one row
per file. PDF loading is the only thing that really touches disk binary
formats, so we monkeypatch :func:`banking_pipeline.cli.load_pdf` to read the
``.pdf`` file's bytes as UTF-8 text — our fixture tree ships real banking text
under ``.txt``, and copying those into ``.pdf`` files in a tempdir is the
lightest way to exercise the walker without carrying real PDFs into the repo.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import cli
from banking_pipeline.models import RawDocument


@pytest.fixture
def scan_tree(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Build a scannable tree under ``<tmp>/scan``:

    * one ``.pdf`` at the root (exercises the default non-recursive behaviour)
    * three more ``.pdf``\\s buried in nested folders (so ``--recursive``
      actually has something to find)
    * a deliberately-broken ``.pdf`` (exercises the error-handling path)
    * a ``README.md`` at the root (confirms non-PDFs are filtered out)
    """
    root = tmp_path / "scan"
    (root / "en" / "pictet").mkdir(parents=True)
    (root / "es" / "pictet").mkdir(parents=True)

    # Top-level file — the one case a non-recursive scan should see.
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "subscription_notice.txt",
        root / "subscription.pdf",
    )
    # Nested files — only discoverable with --recursive.
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "redemption_notice.txt",
        root / "en" / "pictet" / "redemption.pdf",
    )
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "fx_forward.txt",
        root / "en" / "pictet" / "fx_forward.pdf",
    )
    shutil.copy(
        fixtures_dir / "es" / "pictet" / "switch_salida.txt",
        root / "es" / "pictet" / "switch_salida.pdf",
    )
    # A bogus file that should surface as an error row, not abort the scan.
    (root / "es" / "pictet" / "broken.pdf").write_bytes(b"")
    # A non-PDF peer that should be ignored entirely.
    (root / "README.md").write_text("not a PDF", encoding="utf-8")

    return root


@pytest.fixture(autouse=True)
def _fake_pdf_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route ``load_pdf`` to a text reader. Empty files raise, matching the
    "one bad PDF shouldn't kill the run" contract we advertise.
    """

    def fake(path: Path) -> RawDocument:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("empty document")
        return RawDocument(path=path, text=text, page_count=1)

    monkeypatch.setattr(cli, "load_pdf", fake)


def test_scan_text_mode_lists_every_pdf(scan_tree: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["scan", "--recursive", str(scan_tree)])

    assert result.exit_code == 0, result.output
    # One row per PDF (broken.pdf included as an error row); README.md excluded.
    assert "subscription.pdf" in result.output
    assert "redemption.pdf" in result.output
    assert "fx_forward.pdf" in result.output
    assert "switch_salida.pdf" in result.output
    assert "broken.pdf" in result.output
    assert "README.md" not in result.output

    # The good rows should include their decided doctype token somewhere on
    # the line; that's the whole reason the command exists.
    assert "subscription_notice" in result.output
    assert "redemption_notice" in result.output
    assert "fx_forward" in result.output
    assert "switch_salida" in result.output

    # The broken file must surface as an error, not a classification.
    assert "ERROR" in result.output


def test_scan_json_mode_emits_one_object_per_file(scan_tree: Path) -> None:
    runner = CliRunner()
    result = runner.invoke(cli.app, ["scan", "-r", str(scan_tree), "--json"])

    assert result.exit_code == 0, result.output
    lines = [ln for ln in result.output.splitlines() if ln.startswith("{")]
    # Four classifications (subscription + redemption + fx_forward + switch_salida)
    # + one error row for broken.pdf.
    assert len(lines) == 5

    rows = [json.loads(ln) for ln in lines]
    by_name = {Path(r["path"]).name: r for r in rows}

    # Good rows carry language/bank/document_type triples with confidences.
    redemption = by_name["redemption.pdf"]
    assert redemption["language"]["value"] == "en"
    assert redemption["bank"]["value"] == "pictet"
    assert redemption["document_type"]["value"] == "redemption_notice"
    assert 0.0 <= redemption["document_type"]["confidence"] <= 1.0
    assert redemption["document_type"]["template_id"] == "pictet.redemption_notice.v1"

    salida = by_name["switch_salida.pdf"]
    assert salida["language"]["value"] == "es"
    assert salida["bank"]["value"] == "pictet"
    assert salida["document_type"]["value"] == "switch_salida"

    # The broken row is shaped differently: an ``error`` key instead of a
    # classification. Keeping that contract makes JSONL consumers able to
    # branch on key presence rather than parsing free text.
    broken = by_name["broken.pdf"]
    assert "error" in broken
    assert "language" not in broken


def test_scan_writes_to_output_file(scan_tree: Path, tmp_path: Path) -> None:
    runner = CliRunner()
    out = tmp_path / "results.jsonl"
    result = runner.invoke(
        cli.app, ["scan", "-r", str(scan_tree), "--json", "-o", str(out)]
    )

    assert result.exit_code == 0, result.output
    assert out.exists()
    # One JSON object per non-skipped file, same count as the in-memory version.
    lines = [ln for ln in out.read_text().splitlines() if ln.strip()]
    assert len(lines) == 5
    for line in lines:
        json.loads(line)  # must parse


# --- Recursion gating -------------------------------------------------------

def test_scan_default_is_non_recursive(scan_tree: Path) -> None:
    """Without ``--recursive``, only the top-level PDF should be picked up.
    The nested files (redemption, fx_forward, switch_salida, broken) live in
    ``en/pictet/`` and ``es/pictet/`` and must be invisible to the default run.
    """
    runner = CliRunner()
    result = runner.invoke(cli.app, ["scan", str(scan_tree)])

    assert result.exit_code == 0, result.output
    assert "subscription.pdf" in result.output
    # Nested files must NOT appear.
    assert "redemption.pdf" not in result.output
    assert "fx_forward.pdf" not in result.output
    assert "switch_salida.pdf" not in result.output
    assert "broken.pdf" not in result.output
    # Summary line should reflect the single-file scan.
    assert "Scanned 1 file" in result.output


def test_scan_recursive_short_flag_matches_long_flag(scan_tree: Path) -> None:
    """``-r`` must behave identically to ``--recursive`` — both descend."""
    runner = CliRunner()
    long_form = runner.invoke(cli.app, ["scan", "--recursive", str(scan_tree), "--json"])
    short_form = runner.invoke(cli.app, ["scan", "-r", str(scan_tree), "--json"])

    assert long_form.exit_code == 0, long_form.output
    assert short_form.exit_code == 0, short_form.output

    def _paths(output: str) -> set[str]:
        return {
            json.loads(ln)["path"]
            for ln in output.splitlines()
            if ln.startswith("{")
        }

    assert _paths(long_form.output) == _paths(short_form.output)

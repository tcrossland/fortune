"""Tests for the Pictet P&L tax-report retention policy + prune command.

The pure selection policy (``tax_report_prune.select_retained``) is exercised
over a synthetic dated set; the CLI (``prune-tax-reports``) is exercised over
a temporary archive tree of empty ``.pdf`` files — the policy keys only on
the canonical filename, so no PDF content is needed.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import cli, tax_report_prune
from banking_pipeline.models import RawDocument
from banking_pipeline.tax_report_prune import TaxReport, TaxReportKind

# --- Fixture data -----------------------------------------------------------

# One year of realised/unrealised reports spread across several months, a
# year boundary, and the two anchor edges: an early-April cut (03.04, before
# the 5-Apr UK anchor) and a 31-Dec year-final.
_2023_DAYS = [
    date(2023, 1, 5), date(2023, 1, 19), date(2023, 1, 31),
    date(2023, 2, 14), date(2023, 2, 28),
    date(2023, 3, 10), date(2023, 3, 30),
    date(2023, 4, 3), date(2023, 4, 20), date(2023, 4, 28),
    date(2023, 12, 15), date(2023, 12, 31),
]
_2024_DAYS = [date(2024, 1, 10), date(2024, 1, 20)]


def _name(kind: TaxReportKind, d: date) -> str:
    return f"{kind} PL {d:%Y%m%d}.pdf"


def _report(kind: TaxReportKind, d: date) -> TaxReport:
    return TaxReport(kind=kind, as_of=d, path=Path(_name(kind, d)))


def _all_reports() -> list[TaxReport]:
    reports = [_report("Realised", d) for d in _2023_DAYS + _2024_DAYS]
    reports += [_report("Unrealised", d) for d in _2023_DAYS]
    return reports


# --- parse_tax_report -------------------------------------------------------


def test_parse_tax_report_reads_canonical_name() -> None:
    r = tax_report_prune.parse_tax_report(Path("Unrealised PL 20230720.pdf"))
    assert r is not None
    assert (r.kind, r.as_of) == ("Unrealised", date(2023, 7, 20))
    real = tax_report_prune.parse_tax_report(Path("Realised PL 20231231.pdf"))
    assert real is not None
    assert (real.kind, real.as_of) == ("Realised", date(2023, 12, 31))


def test_parse_tax_report_ignores_non_pl_names() -> None:
    for name in (
        "Tax - Tax valuations - ETE-20221231.pdf",
        "Tax - Tax valuations - Modelo 720-20221231.pdf",
        "Tax - Realised PL report-20220103.pdf",  # legacy, not yet normalised
        "Realised PL 2023072.pdf",  # short date
        "Valuation monthly 20260430.pdf",
        # The annual statement is a canonical name but NOT a prunable P&L
        # report — retention (parse_tax_report) skips it; the sweep guard
        # (is_canonical_name) still recognises it.
        "Fiscal statement 20241231.pdf",
    ):
        assert tax_report_prune.parse_tax_report(Path(name)) is None


def test_is_canonical_name_covers_statement_but_parse_excludes_it() -> None:
    # is_canonical_name recognises every canonical tax-report name (so the
    # sweep never treats a filed one as a legacy stray)…
    for name in (
        "Realised PL 20241231.pdf",
        "Unrealised PL 20241231.pdf",
        "Fiscal statement 20241231.pdf",
    ):
        assert tax_report_prune.is_canonical_name(name)
    # …while non-canonical / legacy names are not.
    for name in (
        "Tax - Statement Capital gains-20241231.pdf",
        "Fiscal statement 2024.pdf",  # short date
    ):
        assert not tax_report_prune.is_canonical_name(name)


# --- select_retained --------------------------------------------------------


def test_select_retained_keeps_month_end_and_anchors() -> None:
    kept = tax_report_prune.select_retained(_all_reports())

    # Realised: latest per month present + year-final (= Dec 31, already the
    # December month-latest). No early-April anchor.
    expected_realised = {
        _name("Realised", d)
        for d in (
            date(2023, 1, 31),
            date(2023, 2, 28),
            date(2023, 3, 30),
            date(2023, 4, 28),
            date(2023, 12, 31),
            date(2024, 1, 20),  # 2024 year, its only month's latest
        )
    }
    # Unrealised: the same month-latest set + the on-or-before-5-Apr snapshot
    # (03.04), which the plain month-latest (28.04) would miss.
    expected_unrealised = {
        _name("Unrealised", d)
        for d in (
            date(2023, 1, 31),
            date(2023, 2, 28),
            date(2023, 3, 30),
            date(2023, 4, 3),
            date(2023, 4, 28),
            date(2023, 12, 31),
        )
    }
    kept_names = {p.name for p in kept}
    assert kept_names == expected_realised | expected_unrealised


def test_select_retained_realised_has_no_april_anchor() -> None:
    """The 5-April anchor is unrealised-only; a realised early-April cut with
    a later-April month-latest is not separately retained."""

    reports = [
        _report("Realised", date(2023, 4, 3)),
        _report("Realised", date(2023, 4, 28)),
    ]
    kept = {p.name for p in tax_report_prune.select_retained(reports)}
    assert kept == {_name("Realised", date(2023, 4, 28))}


def test_select_retained_empty() -> None:
    assert tax_report_prune.select_retained([]) == set()


def test_plan_prune_splits_and_sorts_by_year() -> None:
    plans = tax_report_prune.plan_prune(_all_reports())
    years = [p.year for p in plans]
    assert years == sorted(years) == [2023, 2024]
    # Every report is either kept or superseded, never both / neither.
    for plan in plans:
        keep = {r.path for r in plan.keep}
        move = {r.path for r in plan.supersede}
        assert not (keep & move)
    total_keep = sum(len(p.keep) for p in plans)
    total_move = sum(len(p.supersede) for p in plans)
    assert total_keep + total_move == len(_all_reports())
    # 5 realised (2023) + 1 realised (2024) + 6 unrealised (2023) kept.
    assert total_keep == 12


# --- CLI --------------------------------------------------------------------


def _build_tree(root: Path) -> None:
    """A synthetic archive: 2023/tax and 2024/tax of empty canonical PDFs,
    plus an ETE file that must be left untouched."""

    for kind in ("Realised", "Unrealised"):
        for d in _2023_DAYS:
            (root / "2023" / "tax").mkdir(parents=True, exist_ok=True)
            (root / "2023" / "tax" / _name(kind, d)).write_text("x")
    (root / "2023" / "tax" / "Tax - Tax valuations - ETE-20221231.pdf").write_text(
        "ete"
    )
    (root / "2024" / "tax").mkdir(parents=True, exist_ok=True)
    for d in _2024_DAYS:
        (root / "2024" / "tax" / _name("Realised", d)).write_text("x")


def test_prune_dry_run_moves_nothing(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    runner = CliRunner()
    result = runner.invoke(cli.app, ["prune-tax-reports", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "(dry-run)" in result.output
    assert "2023: keep 11, to move 13" in result.output
    assert "2024: keep 1, to move 1" in result.output
    # Nothing moved.
    assert not (tmp_path / "2023" / "tax" / "_superseded").exists()
    assert (
        tmp_path / "2023" / "tax" / _name("Realised", date(2023, 1, 5))
    ).exists()


def test_prune_apply_converges_and_is_idempotent(tmp_path: Path) -> None:
    _build_tree(tmp_path)
    runner = CliRunner()

    result = runner.invoke(cli.app, ["prune-tax-reports", str(tmp_path), "--apply"])
    assert result.exit_code == 0, result.output

    tax_2023 = tmp_path / "2023" / "tax"
    superseded = tax_2023 / "_superseded"
    # A retained anchor stays; a pruned daily moves.
    assert (tax_2023 / _name("Realised", date(2023, 12, 31))).exists()
    assert (tax_2023 / _name("Unrealised", date(2023, 4, 3))).exists()  # 5-Apr
    assert not (tax_2023 / _name("Realised", date(2023, 1, 5))).exists()
    assert (superseded / _name("Realised", date(2023, 1, 5))).exists()
    # The ETE report is never touched.
    assert (tax_2023 / "Tax - Tax valuations - ETE-20221231.pdf").exists()

    # Re-run: the superseded/ folder is not descended into, so nothing moves.
    result2 = runner.invoke(
        cli.app, ["prune-tax-reports", str(tmp_path), "--apply"]
    )
    assert result2.exit_code == 0, result2.output
    assert "total: keep 12, moved 0 (+ 0 legacy duplicate(s))" in result2.output


def test_prune_sweeps_content_duplicates(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A legacy-named copy that duplicates an already-filed canonical report
    (by content) is swept aside; a legacy copy whose canonical doesn't yet
    exist, and the ETE report, are left in place.

    The realised fixture's content as-of is 2023-07-20, so its canonical name
    is ``Realised PL 20230720.pdf``. Two legacy-named files carry that same
    content — one collides with an existing canonical (→ swept), the other
    (dated 2022) has no canonical present (→ left).
    """

    # Route the filing module's PDF loader to a plain text reader, so the
    # legacy files' text content drives classification.
    def fake_load(path: Path) -> RawDocument:
        return RawDocument(
            path=path, text=path.read_text(encoding="utf-8"), page_count=1
        )

    monkeypatch.setattr("banking_pipeline.archive.load_pdf", fake_load)

    realised_text = (
        fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt"
    ).read_text()

    tax = tmp_path / "2023" / "tax"
    tax.mkdir(parents=True)
    # The canonical report this duplicate collides with (content as-of
    # 2023-07-20). Its own name is canonical, so the sweep ignores it.
    (tax / "Realised PL 20230720.pdf").write_text(realised_text)
    # A legacy-named re-download of the same 2023-07-20 report → swept.
    dup = tax / "0173837-Tax+-+Realised+P%2FL+report-20231005.pdf"
    dup.write_text(realised_text)
    # A P&L report whose canonical isn't present here → left (would be
    # normalised, not superseded). Lives in a 2023 folder but its content
    # files under 2023/tax as Realised PL 20230720 — which *does* exist, so
    # to model an orphan we put it under a year with no canonical.
    ete = tax / "Tax - Tax valuations - ETE-20221231.pdf"
    ete.write_text("not a P&L report, just some ETE text")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["prune-tax-reports", str(tmp_path), "--apply"])
    assert result.exit_code == 0, result.output
    assert "moved 1 legacy duplicate(s)" in result.output

    assert (tax / "_superseded" / dup.name).exists()
    assert not dup.exists()
    assert ete.exists()  # non-P&L file untouched
    assert (tax / "Realised PL 20230720.pdf").exists()  # canonical retained


def test_prune_leaves_fiscal_statement_untouched(tmp_path: Path) -> None:
    """A filed annual ``Fiscal statement`` is a canonical name but not a
    prunable P&L report — the retention pass ignores it and the legacy-dup
    sweep must not mistake it for a stray and move it aside."""

    tax = tmp_path / "2024" / "tax"
    tax.mkdir(parents=True)
    stmt = tax / "Fiscal statement 20241231.pdf"
    stmt.write_text("x")
    # A couple of P&L dailies that will be pruned, to exercise a real run.
    for d in _2024_DAYS:
        (tax / _name("Realised", d)).write_text("x")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["prune-tax-reports", str(tmp_path), "--apply"])
    assert result.exit_code == 0, result.output

    assert stmt.exists()  # retained in place
    assert not (tax / "_superseded" / stmt.name).exists()  # never swept


def test_prune_errors_without_archive_root(tmp_path: Path) -> None:
    runner = CliRunner()
    # A directory with no config and no year/tax folders resolves a root
    # (via --project-root having no config → falls back), but the explicit
    # missing path is the clean error case.
    result = runner.invoke(
        cli.app, ["prune-tax-reports", str(tmp_path / "does-not-exist")]
    )
    assert result.exit_code == 2
    assert "not a directory" in result.output

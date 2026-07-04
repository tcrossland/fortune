"""Tests for the new ``--statements-dir`` / ``--statements-recursive``
flags on the ``prices`` subcommand and the matching ``price_statements``
glob list on the rebuild config.

Same monkeypatch trick as :mod:`test_cli_scan`: PDF loading is faked to
read each ``.pdf`` file's bytes as UTF-8 text, so we can drop fixture
``.txt`` content into a tempdir tree under ``.pdf`` filenames and
exercise the walker without carrying real PDFs in the repo.
"""

from __future__ import annotations

import shutil
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import cli, extractors, prices_extract
from banking_pipeline.models import DocumentType, RawDocument
from banking_pipeline.prices_extract import PriceRow


@pytest.fixture(autouse=True)
def _fake_pdf_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every ``load_pdf`` call (both the one bound at module-load
    time on :mod:`cli` and the lazily-imported one inside
    :func:`prices_extract.generate`) at a simple text reader so we can
    exercise the walker with fixture ``.txt`` content stored in
    ``.pdf`` files.
    """

    def fake(path: Path) -> RawDocument:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("empty document")
        return RawDocument(path=path, text=text, page_count=1)

    monkeypatch.setattr(cli._main, "load_pdf", fake)
    monkeypatch.setattr(extractors, "load_pdf", fake)
    monkeypatch.setattr(extractors.pdf_text, "load_pdf", fake)


@pytest.fixture
def statements_tree(tmp_path: Path, fixtures_dir: Path) -> Path:
    """Build a directory tree containing one of every statement variant
    plus a non-statement PDF; return the root.

    Layout::

        statements/
            monthly_top.pdf            (top-level — non-recursive sees this)
            other.pdf                  (a non-statement, must be filtered out)
            quarterly/
                quarterly.pdf          (must be filtered out — no Portfolio valuation)
            nested/
                monthly_nested.pdf     (only seen with --statements-recursive)
                annual.pdf             (must be filtered out per user clarification)
    """

    root = tmp_path / "statements"
    (root / "quarterly").mkdir(parents=True)
    (root / "nested").mkdir(parents=True)

    monthly = fixtures_dir / "en" / "pictet" / "monthly_statement.txt"
    quarterly = fixtures_dir / "en" / "pictet" / "quarterly_statement.txt"
    annual = fixtures_dir / "en" / "pictet" / "annual_statement.txt"
    other = fixtures_dir / "en" / "pictet" / "redemption_notice.txt"

    shutil.copy(monthly, root / "monthly_top.pdf")
    shutil.copy(other, root / "other.pdf")
    shutil.copy(quarterly, root / "quarterly" / "quarterly.pdf")
    shutil.copy(monthly, root / "nested" / "monthly_nested.pdf")
    shutil.copy(annual, root / "nested" / "annual.pdf")

    return root


# --- PRICED_STATEMENT_DOCTYPES constant ------------------------------------


def test_priced_statement_doctypes_carry_per_asset_prices() -> None:
    """Only statements with a per-asset valuation table belong here, so
    the directory walker doesn't waste time parsing the Pictet annual /
    quarterly PDFs (regulatory / ESG pages only). Pictet's monthly
    statements (EN + ES) and Vanguard's ISA regular statement qualify."""

    assert frozenset({
        DocumentType.MONTHLY_STATEMENT,
        DocumentType.ESTADO_MENSUAL,
        DocumentType.VANGUARD_REGULAR_STATEMENT,
    }) == prices_extract.PRICED_STATEMENT_DOCTYPES


# --- prices --statements-dir -----------------------------------------------


def test_prices_statements_dir_default_is_non_recursive(
    statements_tree: Path, tmp_path: Path
) -> None:
    """Without ``--statements-recursive`` only the top-level monthly
    statement is consumed. The non-statement ``other.pdf`` at the same
    level is classified-and-rejected, the quarterly under
    ``quarterly/`` is invisible to the walker, and the nested
    monthly + annual under ``nested/`` are also invisible.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "prices",
            str(data_dir),
            "--statements-dir",
            str(statements_tree),
        ],
    )

    assert result.exit_code == 0, result.output
    # Discovery diagnostic mentions one statement (the top-level monthly).
    assert "Discovered 1 monthly statement(s)" in result.output
    # The output file exists and contains price directives sourced from
    # the monthly fixture's ``As at 31 December 2025`` page.
    out = (data_dir / "prices.beancount").read_text(encoding="utf-8")
    assert "2025-12-31 price LU2601001147" in out
    # Each statement-derived directive carries a trailing
    # ``; source: <pdf>`` comment pointing back at its origin —
    # makes the prices file its own audit trail.
    assert "; source: monthly_top.pdf" in out


def test_prices_statements_dir_recursive_picks_up_nested_monthly(
    statements_tree: Path, tmp_path: Path
) -> None:
    """``--statements-recursive`` descends into ``nested/`` and picks
    up ``monthly_nested.pdf``, but still rejects the annual sibling and
    the quarterly under ``quarterly/``.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "prices",
            str(data_dir),
            "--statements-dir",
            str(statements_tree),
            "--statements-recursive",
        ],
    )

    assert result.exit_code == 0, result.output
    # Two monthly statements (top-level + nested), zero annual / quarterly.
    assert "Discovered 2 monthly statement(s)" in result.output
    assert "(recursive)" in result.output


def test_prices_statements_dir_combined_with_explicit_statement(
    statements_tree: Path, tmp_path: Path, fixtures_dir: Path
) -> None:
    """``--statement`` and ``--statements-dir`` are additive — the
    explicit file is merged with whatever the walker finds.
    """

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    explicit = fixtures_dir / "en" / "pictet" / "monthly_statement.txt"
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "prices",
            str(data_dir),
            "--statement",
            str(explicit),
            "--statements-dir",
            str(statements_tree),
        ],
    )

    assert result.exit_code == 0, result.output
    # 1 explicit + 1 discovered = 2 merged.
    assert "2 statement(s) merged" in result.output


def test_discover_filename_glob_fast_path_prunes_before_classify(
    statements_tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ``--statements-glob`` fast path prunes the walk by filename
    *before* any PDF is opened: only files matching the glob are loaded +
    classified, and the kept set is identical to the classify-every-PDF
    default — just cheaper. This is what avoids text-extracting the whole
    archive to find the ~monthly statements.
    """

    from banking_pipeline.cli._main import _discover_priced_statements

    opened: list[str] = []
    delegate = cli._main.load_pdf  # the fixture's text-reading fake

    def recording(path: Path) -> RawDocument:
        opened.append(path.name)
        return delegate(path)

    monkeypatch.setattr(cli._main, "load_pdf", recording)

    # Fast path: only the two ``*monthly*`` files are ever opened; the
    # non-statement, quarterly, and annual PDFs are pruned by name and
    # never classified.
    fast = _discover_priced_statements(
        statements_tree, recursive=True, pattern="*monthly*.pdf"
    )
    assert {p.name for p in fast} == {"monthly_top.pdf", "monthly_nested.pdf"}
    assert sorted(opened) == ["monthly_nested.pdf", "monthly_top.pdf"]

    # Default path (pattern ``*.pdf``): every PDF is opened and the
    # classifier does the filtering — same kept set, more work.
    opened.clear()
    full = _discover_priced_statements(
        statements_tree, recursive=True, pattern="*.pdf"
    )
    assert {p.name for p in full} == {"monthly_top.pdf", "monthly_nested.pdf"}
    assert len(opened) == 5  # all PDFs opened just to classify + discard 3


def test_latest_statements_per_group_keeps_newest_per_dir() -> None:
    """The ``latest_only`` pre-open prune: keep the newest file (by the
    ``YYYYMMDD`` in the name) per directory, all files sharing a directory's
    max date, and any undated file (can't rank)."""

    from banking_pipeline.cli._main import _latest_statements_per_group

    a, b = Path("2026/K/reports"), Path("2026/P/reports")
    paths = [
        a / "Valuation-monthly-20260131.pdf",  # superseded within dir a
        a / "Valuation-monthly-20260228.pdf",  # newest in dir a
        b / "Valuation-monthly-20260228.pdf",  # newest in dir b
        b / "Valuation-monthly-20260131.pdf",  # superseded within dir b
        a / "cover-note.pdf",                    # undated — kept (unrankable)
    ]
    kept = {p.name for p in _latest_statements_per_group(paths)}
    # Newest per dir (a's 0228 and b's 0228), plus the undated file; the two
    # January statements are pruned.
    assert kept == {"Valuation-monthly-20260228.pdf", "cover-note.pdf"}

    # Two portfolios marked the same month-end in one directory: both kept
    # (equal max date), so neither portfolio's latest is dropped.
    shared = Path("flat/reports")
    both = [
        shared / "K-monthly-20260228.pdf",
        shared / "P-monthly-20260228.pdf",
        shared / "K-monthly-20260131.pdf",
    ]
    kept2 = {p.name for p in _latest_statements_per_group(both)}
    assert kept2 == {"K-monthly-20260228.pdf", "P-monthly-20260228.pdf"}


def test_discover_latest_only_prunes_superseded_before_classify(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``latest_only=True`` opens only the newest statement per directory —
    the superseded monthlies are pruned by filename date and never
    classified. This is the ``holdings`` fast path: parse the current
    snapshot per portfolio, not the whole history.
    """

    from banking_pipeline.cli._main import _discover_priced_statements

    monthly = fixtures_dir / "en" / "pictet" / "monthly_statement.txt"
    k_dir = tmp_path / "K-999999.001" / "reports"
    p_dir = tmp_path / "P-999999.002" / "reports"
    k_dir.mkdir(parents=True)
    p_dir.mkdir(parents=True)
    for d, dates in ((k_dir, ("20260131", "20260228", "20260331")),
                     (p_dir, ("20260228", "20260331"))):
        for ymd in dates:
            shutil.copy(monthly, d / f"Valuation-monthly-{ymd}.pdf")

    opened: list[str] = []
    delegate = cli._main.load_pdf

    def recording(path: Path) -> RawDocument:
        opened.append(path.name)
        return delegate(path)

    monkeypatch.setattr(cli._main, "load_pdf", recording)

    discovered = _discover_priced_statements(
        tmp_path, recursive=True, pattern="*monthly*.pdf", latest_only=True
    )
    # Only the March statement in each dir is opened + kept; the 3 earlier
    # monthlies are pruned before classification.
    assert sorted(opened) == [
        "Valuation-monthly-20260331.pdf",
        "Valuation-monthly-20260331.pdf",
    ]
    assert {p.name for p in discovered} == {"Valuation-monthly-20260331.pdf"}


def test_configured_statement_paths_expands_balance_statements(
    statements_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The zero-config fallback: with no ``--statement`` / ``--statements-dir``
    the report CLIs expand the rebuild's ``balance_statements`` globs from
    ``banking-pipeline.toml`` in the cwd — so an ad-hoc report reuses the
    canonical statement set (and doesn't silently drop non-``monthly`` files
    the way a bare glob default would)."""

    from banking_pipeline.cli._main import _configured_statement_paths

    project = tmp_path / "project"
    project.mkdir()
    (project / "src_pdfs").mkdir()  # empty source dir keeps the config valid
    config = textwrap.dedent(f"""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "ingest"
        glob = "src_pdfs/*.pdf"

        [post]
        balance_statements = [
            "{statements_tree}/**/*monthly*.pdf",
            "{statements_tree}/other.pdf",
        ]
    """)
    (project / "banking-pipeline.toml").write_text(config, encoding="utf-8")

    monkeypatch.chdir(project)
    paths = _configured_statement_paths()
    # Both globs expand and merge — the two monthly statements plus the
    # explicitly-named non-monthly file (the whole point: the config list can
    # name files a single ``*monthly*`` glob would miss, e.g. the ISA dir).
    assert {p.name for p in paths} == {
        "monthly_top.pdf",
        "monthly_nested.pdf",
        "other.pdf",
    }


def test_configured_statement_paths_no_config_returns_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``banking-pipeline.toml`` in the cwd → the fallback yields nothing,
    so the caller falls through to its "no statements given" error unchanged."""

    from banking_pipeline.cli._main import _configured_statement_paths

    monkeypatch.chdir(tmp_path)
    assert _configured_statement_paths() == []


# --- batch_config price_statements + rebuild plumbing ----------------------


def test_rebuild_price_statements_filters_to_monthly_only(
    statements_tree: Path, tmp_path: Path
) -> None:
    """Configure a rebuild with ``price_statements`` pointing at the
    full statements tree (recursive glob, mixed doctypes). The prices
    step must classify each match and only feed the monthly statements
    into the price extractor.

    No ``[[sources]]`` are configured here — the rebuild's own validator
    requires sources when post-steps are enabled, so we add a single
    no-op source pointing at an empty directory and expect a warning,
    then run the prices post-step against an empty data dir.
    """

    project_root = tmp_path / "project"
    project_root.mkdir()
    data_dir = project_root / "data"
    # Empty source-PDF directory — keeps the rebuild config valid while
    # the ingest step does nothing.
    (project_root / "src_pdfs").mkdir()

    config = textwrap.dedent(f"""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "ingest"
        glob = "src_pdfs/*.pdf"

        [post]
        prices = true
        portfolio = false
        balances = false
        price_statements = ["{statements_tree}/**/*.pdf"]

        [post.check]
        enabled = false
    """)
    (project_root / "banking-pipeline.toml").write_text(config, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["rebuild", "--project-root", str(project_root)],
    )

    assert result.exit_code == 0, result.output
    # Collapse Rich's wrapped output into a single string so we can
    # match contiguous text without caring about where the renderer
    # decided to break lines. Tree contains 5 PDFs (top-level monthly,
    # top-level non-statement, quarterly, nested monthly, nested
    # annual) but only the two monthlies should pass the classifier
    # filter.
    flat = " ".join(result.output.split())
    assert "2 of 5 matched statement(s) classified as monthly" in flat
    assert "2 statement(s) merged" in flat
    out = (data_dir / "prices.beancount").read_text(encoding="utf-8")
    # Both monthly statements share the same fixture text → same
    # (date, ISIN) rows; merging is deduped so just confirm the
    # canonical row appears once.
    assert out.count("2025-12-31 price LU2601001147") == 1


def test_rebuild_price_statements_empty_keeps_legacy_behaviour(
    tmp_path: Path,
) -> None:
    """When ``price_statements`` is omitted (or empty), the rebuild
    prices step runs trade-only — no statement files, and no
    ``of N matched statement(s)`` cell in the diagnostic line.
    """

    project_root = tmp_path / "project"
    project_root.mkdir()
    (project_root / "src_pdfs").mkdir()

    config = textwrap.dedent("""
        data_dir = "data"
        clean_glob = ""

        [[sources]]
        label = "ingest"
        glob = "src_pdfs/*.pdf"

        [post]
        prices = true
        portfolio = false
        balances = false

        [post.check]
        enabled = false
    """)
    (project_root / "banking-pipeline.toml").write_text(config, encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        ["rebuild", "--project-root", str(project_root)],
    )

    assert result.exit_code == 0, result.output
    flat = " ".join(result.output.split())
    assert "matched statement(s)" not in flat
    assert "statement(s) merged" not in flat


# --- render(): per-line source comments ------------------------------------


def test_render_emits_source_comment_when_present() -> None:
    """A PriceRow with ``source`` set must render with a trailing
    ``; source: <name>`` comment so the prices file points back at its
    origin without needing an external audit log."""

    rows = [
        PriceRow(
            date="2025-12-31",
            commodity="LU2601001147",
            price="113.63",
            currency="GBP",
            source="2025-12-31_K-123456.001.pdf",
        )
    ]
    out = prices_extract.render(rows)
    assert (
        "2025-12-31 price LU2601001147  113.63 GBP  "
        "; source: 2025-12-31_K-123456.001.pdf"
    ) in out


def test_render_omits_source_comment_when_absent() -> None:
    """Backward-compat: rows constructed without a source render as
    bare directives. Library callers that don't bother with provenance
    still get a valid beancount file."""

    rows = [
        PriceRow(
            date="2025-12-31",
            commodity="LU2601001147",
            price="113.63",
            currency="GBP",
        )
    ]
    out = prices_extract.render(rows)
    # Inspect just the directive line, not the header (which mentions
    # ``; source:`` in its documentation prose).
    directive_lines = [
        ln for ln in out.splitlines() if ln.startswith("2025-12-31 ")
    ]
    assert directive_lines == ["2025-12-31 price LU2601001147  113.63 GBP"]


# --- merge_prices(): collision warnings ------------------------------------


def test_merge_prices_warns_on_price_collision() -> None:
    """When two rows share ``(date, commodity)`` but disagree on
    price, a structlog warning must fire so silent drift becomes
    visible. The warning carries both prices and both sources.

    Uses :func:`structlog.testing.capture_logs` rather than pytest's
    ``caplog`` fixture because structlog's default factory writes to
    stderr, not to stdlib ``logging`` — ``caplog`` only sees the
    latter.
    """

    from structlog.testing import capture_logs

    older = PriceRow(
        date="2025-12-31",
        commodity="LU2601001147",
        price="113.63",
        currency="GBP",
        source="2025.beancount",
    )
    newer = PriceRow(
        date="2025-12-31",
        commodity="LU2601001147",
        price="113.99",
        currency="GBP",
        source="monthly_2025-12.pdf",
    )

    with capture_logs() as logs:
        merged = prices_extract.merge_prices([older], [newer])

    # Last-write-wins on the merge itself: ``newer`` was passed second.
    assert merged == [newer]
    collision = next(
        (log for log in logs if log["event"] == "prices_extract.price_collision"),
        None,
    )
    assert collision is not None, f"no collision warning fired; got: {logs}"
    assert collision["log_level"] == "warning"
    assert collision["old_price"] == "113.63"
    assert collision["new_price"] == "113.99"
    assert collision["old_source"] == "2025.beancount"
    assert collision["new_source"] == "monthly_2025-12.pdf"


def test_merge_prices_silent_on_identical_overwrite() -> None:
    """Same-price overwrites are silent. Re-running the rebuild against
    overlapping sources is a normal operation and shouldn't generate
    warning noise."""

    from structlog.testing import capture_logs

    row = PriceRow(
        date="2025-12-31",
        commodity="LU2601001147",
        price="113.63",
        currency="GBP",
        source="2025.beancount",
    )
    duplicate = row._replace(source="monthly_2025-12.pdf")

    with capture_logs() as logs:
        merged = prices_extract.merge_prices([row], [duplicate])

    assert merged == [duplicate]
    assert not any(
        log["event"] == "prices_extract.price_collision" for log in logs
    )


# --- extract_prices_from_statement(): doctype tightening -------------------


def test_extract_prices_from_statement_skips_non_monthly_doctype(
    fixtures_dir: Path,
) -> None:
    """When a non-monthly doctype is provided, the parser must
    short-circuit to ``[]`` without scanning. The fixture text used
    here is a real monthly statement (so the lenient path *would*
    yield rows), proving the early-return is doctype-driven, not
    text-driven."""

    text = (fixtures_dir / "en" / "pictet" / "monthly_statement.txt").read_text(
        encoding="utf-8"
    )

    # Sanity: lenient call (no doctype) yields rows from this fixture.
    assert prices_extract.extract_prices_from_statement(text)

    # Tightened call with a non-monthly doctype: zero rows.
    assert (
        prices_extract.extract_prices_from_statement(
            text, doctype=DocumentType.QUARTERLY_STATEMENT
        )
        == []
    )
    assert (
        prices_extract.extract_prices_from_statement(
            text, doctype=DocumentType.ANNUAL_STATEMENT
        )
        == []
    )


def test_extract_prices_from_statement_threads_source_into_rows(
    fixtures_dir: Path,
) -> None:
    """The ``source`` kwarg must be threaded onto every emitted
    :class:`PriceRow` so downstream rendering can produce per-line
    provenance comments."""

    text = (fixtures_dir / "en" / "pictet" / "monthly_statement.txt").read_text(
        encoding="utf-8"
    )
    rows = prices_extract.extract_prices_from_statement(
        text,
        doctype=DocumentType.MONTHLY_STATEMENT,
        source="monthly_2025-12.pdf",
    )
    assert rows, "fixture should produce at least one priced ISIN"
    assert all(r.source == "monthly_2025-12.pdf" for r in rows)


def test_explicit_statement_with_wrong_doctype_skipped(
    statements_tree: Path, tmp_path: Path, fixtures_dir: Path
) -> None:
    """Passing an annual / quarterly fixture explicitly via
    ``--statement`` no longer silently parses to zero rows: the parser
    receives the doctype from cli's ``_classify_paths`` and skips with
    an info log. The output prices file then reflects only the
    monthly statement that was also passed."""

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monthly = fixtures_dir / "en" / "pictet" / "monthly_statement.txt"
    quarterly = fixtures_dir / "en" / "pictet" / "quarterly_statement.txt"
    runner = CliRunner()
    result = runner.invoke(
        cli.app,
        [
            "prices",
            str(data_dir),
            "--statement",
            str(monthly),
            "--statement",
            str(quarterly),
        ],
    )

    assert result.exit_code == 0, result.output
    # Both files were "merged" in the sense that the CLI counts them
    # as supplied statements; the doctype short-circuit happens inside
    # extract_prices_from_statement, not at the count-statements layer.
    assert "2 statement(s) merged" in result.output
    out = (data_dir / "prices.beancount").read_text(encoding="utf-8")
    # Prices come only from the monthly fixture: it carries
    # ``2025-12-31`` for LU2601001147 — the quarterly fixture is
    # anonymised (``99 December 9999``) and would silently yield
    # zero rows in the lenient path too, but the doctype skip
    # makes that intent explicit.
    assert "2025-12-31 price LU2601001147" in out

"""The ``prune-tax-reports`` retention command.

Trims the archived Pictet IRPF P&L reports (``<year>/tax/``) to the
principled subset defined in :mod:`banking_pipeline.tax_report_prune` —
month-end + year-end / 5-April anchors — moving the daily noise into a
``_superseded/`` sibling. **Dry-run by default**; ``--apply`` performs the
moves. A move, never a delete: the pruned files stay recoverable (and
Dropbox keeps version history on top).

Deliberately *not* wired into ``rebuild``: import over-collects (it files
every daily) and prune trims, so a re-import of an old batch would re-add
dailies a later prune removes — a convergent but churny loop. Keeping prune
a manual, on-demand step avoids that churn until it's trusted.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Annotated

import typer

from banking_pipeline import archive, tax_report_prune
from banking_pipeline.batch_config import load_config
from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.cli._main import _configure_logging, app, err_console
from banking_pipeline.cli_options import VerboseOpt
from banking_pipeline.config import settings
from banking_pipeline.tax_report_prune import SUPERSEDED_DIRNAME


def _superseded_duplicates(
    tax_dir: Path, archive_root: Path, classifier: LayeredClassifier
) -> list[Path]:
    """Non-canonically-named tax-report files in ``tax_dir`` that duplicate an
    already-filed canonical report.

    Reuses :func:`archive.file_documents` in dry-run, which dates a report by
    the **effective date** in its filename (Pictet's Publication/Effective
    date; see ``archive._effective_date_from_filename``). Its ``"skip"`` status
    is exactly "this document's canonical destination already exists" — i.e. a
    redundant copy of that effective date. A file whose canonical does *not*
    yet exist (``"move"``) or that isn't a tax report (``"no-match"``, e.g. an
    unfiled doctype) is left untouched. (Legacy copies re-downloaded on
    different days carry *different* effective dates, so they don't collapse
    together here — the retention policy's month-end dedup handles that
    volume.)
    """

    candidates = [
        path
        for path in sorted(tax_dir.glob("*.pdf"))
        if not tax_report_prune.is_canonical_name(path.name)
    ]
    if not candidates:
        return []
    plans = archive.file_documents(
        candidates, archive_root, dry_run=True, classifier=classifier
    )
    return [plan.source for plan in plans if plan.status == "skip"]


def _resolve_archive_root(root: Path | None, project_root: Path) -> Path | None:
    """The archive root to prune: an explicit argument wins, else the
    ``[import] archive_dir`` from the rebuild config, else the
    ``import_archive_dir`` setting. ``None`` when nothing resolves."""

    if root is not None:
        return root.expanduser()
    try:
        cfg = load_config(project_root)
    except FileNotFoundError:
        cfg = None
    if cfg is not None and cfg.import_step.archive_dir:
        return Path(cfg.import_step.archive_dir).expanduser()
    return settings.import_archive_dir


def _tax_dirs(archive_root: Path) -> list[Path]:
    """Every ``<year>/tax/`` folder under ``archive_root`` (years sorted)."""

    dirs: list[Path] = []
    for year_dir in sorted(archive_root.glob("*")):
        tax_dir = year_dir / "tax"
        if tax_dir.is_dir():
            dirs.append(tax_dir)
    return dirs


@app.command("prune-tax-reports")
def prune_tax_reports(
    root: Annotated[
        Path | None,
        typer.Argument(
            help="Archive root containing <year>/tax/ folders. Defaults to "
            "the [import] archive_dir (or the import_archive_dir setting).",
        ),
    ] = None,
    apply: Annotated[
        bool,
        typer.Option(
            "--apply",
            help="Move superseded reports into <year>/tax/_superseded/. "
            "Without this flag the command only prints the plan.",
        ),
    ] = False,
    project_root: Annotated[Path | None, typer.Option(hidden=True)] = None,
    verbose: VerboseOpt = False,
) -> None:
    """Prune archived Pictet Realised/Unrealised P&L reports to policy.

    Keeps the latest report per calendar month per kind, plus the realised
    year-final and the unrealised on-or-before-5-April snapshot; moves the
    rest to ``<year>/tax/_superseded/``. Dry-run by default — pass
    ``--apply`` to move. Only canonically-named ``<Realised|Unrealised> PL
    <YYYYMMDD>.pdf`` files are pruned by the retention policy; ``ETE`` /
    ``Modelo 720`` / any other name and the ``_superseded/`` folder itself
    are left untouched, so a second run is a no-op.

    It also sweeps aside legacy-named duplicates — a pre-normalisation copy
    (``Tax - Realised PL report-<date>.pdf`` and the URL-encoded variant) that
    duplicates an already-filed canonical report. A copy is dated by the
    effective date in its filename (via ``file_documents``), so it's a
    duplicate only when a canonical of that same effective date already
    exists. This tidies the archive after the one-off normalise pass; a legacy
    file whose canonical doesn't yet exist is never moved.
    """

    _configure_logging(verbose)
    project_root = project_root or Path.cwd()

    archive_root = _resolve_archive_root(root, project_root)
    if archive_root is None:
        err_console.print(
            "[red]error:[/red] no archive root — pass one, or set "
            "[import] archive_dir / import_archive_dir."
        )
        raise typer.Exit(2)
    if not archive_root.is_dir():
        err_console.print(f"[red]error:[/red] {archive_root} is not a directory.")
        raise typer.Exit(2)

    tag = "" if apply else " [dim](dry-run)[/dim]"
    err_console.print(f"[bold]prune-tax-reports[/bold] {archive_root}{tag}")

    def move_aside(path: Path, superseded_dir: Path) -> None:
        """Move ``path`` into ``superseded_dir`` (created on demand). No-op
        under dry-run. A pre-existing same-named copy is never overwritten;
        that leaves ``path`` live, so warn loudly rather than silently drop
        it (unreachable in normal use — canonical/legacy names are unique per
        folder — but a manual collision shouldn't pass silently)."""

        if verbose:
            err_console.print(f"    → {SUPERSEDED_DIRNAME}/{path.name}")
        if apply:
            superseded_dir.mkdir(parents=True, exist_ok=True)
            dest = superseded_dir / path.name
            if dest.exists():
                err_console.print(
                    f"[yellow]warning:[/yellow] {SUPERSEDED_DIRNAME}/{path.name} "
                    f"already exists; left {path.name} in place."
                )
                return
            shutil.move(str(path), str(dest))

    classifier = LayeredClassifier()
    total_keep = total_move = total_dup = 0
    for tax_dir in _tax_dirs(archive_root):
        superseded_dir = tax_dir / SUPERSEDED_DIRNAME

        # First, sweep aside legacy-named copies that duplicate an already-
        # filed canonical report (keyed on the effective date in the filename)
        # so only the canonical set remains to prune. Guarded: a file whose
        # canonical doesn't yet exist, and non-tax files, are left.
        legacy_dups = _superseded_duplicates(tax_dir, archive_root, classifier)

        reports = tax_report_prune.discover_reports(tax_dir)
        if not reports and not legacy_dups:
            continue

        year_label = tax_dir.parent.name
        verb = "moved" if apply else "to move"
        if legacy_dups:
            total_dup += len(legacy_dups)
            err_console.print(
                f"  {year_label}: {verb} {len(legacy_dups)} legacy duplicate(s)"
            )
            for path in legacy_dups:
                move_aside(path, superseded_dir)

        # ``plan.year`` is the content-derived as-of year (how reports are
        # grouped / retained); ``superseded_dir`` is this folder's. They
        # match unless a report is filed in the wrong year's folder — in
        # which case it's still moved within its own tax_dir (recoverable).
        for plan in tax_report_prune.plan_prune(reports):
            total_keep += len(plan.keep)
            total_move += len(plan.supersede)
            err_console.print(
                f"  {plan.year}: keep {len(plan.keep)}, "
                f"{verb} {len(plan.supersede)}"
            )
            for report in plan.supersede:
                move_aside(report.path, superseded_dir)

    verb = "moved" if apply else "to move"
    err_console.print(
        f"[bold]total:[/bold] keep {total_keep}, {verb} {total_move} "
        f"(+ {total_dup} legacy duplicate(s))"
    )
    if not apply and (total_move or total_dup):
        err_console.print("[dim]re-run with --apply to move the superseded files.[/dim]")

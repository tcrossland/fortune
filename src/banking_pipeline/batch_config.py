"""TOML-driven rebuild orchestration config.

Drives the ``banking-pipeline rebuild`` command — replaces the
historical ``run.sh`` shell script. The rebuild config lives in
``banking-pipeline.toml`` (gitignored; copy from
``banking-pipeline.example.toml``) so personal Dropbox / iCloud paths
don't need to land in the repo.

Schema is intentionally narrow:

* ``data_dir`` — where per-batch beancount outputs are written.
* ``clean_glob`` — glob of stale outputs deleted before rebuild.
* ``[import]`` — optional pre-ingest archive step (off by default):
  files fresh downloads into the dated archive tree before the
  ``[[sources]]`` globs read from it.
* ``[[sources]]`` — one entry per ingest call. Each entry has a
  ``label`` (becomes ``<data_dir>/<label>.beancount``) and a
  ``glob`` resolved against the project root.
* ``[post]`` — toggles for the prices / portfolio / balances post-steps.

Validation happens at load time via Pydantic — duplicate labels, missing
sources, and unreadable globs all surface a clear error before any work
starts.
"""

from __future__ import annotations

import tomllib
from collections.abc import Iterable
from itertools import chain
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Path the rebuild command looks for by default. Sits next to
# ``pyproject.toml`` so it's easy to find; gitignored so personal paths
# stay local. ``.example.toml`` is committed as a template.
DEFAULT_CONFIG_FILENAME = "banking-pipeline.toml"
EXAMPLE_CONFIG_FILENAME = "banking-pipeline.example.toml"


class Source(BaseModel):
    """One ingest call: label + PDF glob."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Output filename stem: ``<data_dir>/<label>.beancount``. Must be
    # filesystem-safe (no slashes, no path separators).
    label: str

    # PDF source glob. ``~`` is expanded to the user's home directory;
    # otherwise paths are taken as-is and resolved relative to the
    # project root by the caller. Any pattern :meth:`pathlib.Path.glob`
    # accepts works (``**``, ``*.pdf``, ``[KP]-*.001`` etc.).
    glob: str

    @field_validator("label")
    @classmethod
    def _validate_label(cls, v: str) -> str:
        if not v:
            raise ValueError("label must not be empty")
        if "/" in v or "\\" in v:
            raise ValueError(
                f"label {v!r} contains a path separator; "
                "labels become filenames and must be path-safe"
            )
        return v

    def expand(self, project_root: Path) -> list[Path]:
        """Return every PDF the glob matches, sorted for stable output.

        Resolves ``~`` to the user's home directory; relative paths are
        resolved against ``project_root``. Returns an empty list when
        nothing matches — the caller decides whether that's an error
        (typical for ``rebuild``) or expected (a year-partition that
        hasn't received any documents yet).
        """

        expanded = Path(self.glob).expanduser()
        if expanded.is_absolute():
            # ``Path.glob`` doesn't support absolute patterns directly;
            # split into the longest static parent and the trailing
            # pattern, then glob from there.
            anchor, pattern = _split_anchor(expanded)
            return sorted(anchor.glob(pattern))
        return sorted(project_root.glob(self.glob))


class CheckStep(BaseModel):
    """Configuration for the ``bean-check`` validation step.

    ``bean-check`` is the official beancount validator — it loads the
    ledger, follows ``include`` directives, runs every plugin, and
    reports balance / inventory / parse errors. We shell out to it
    rather than linking against ``beancount`` itself (which is GPL-2.0;
    see the README for the licence story).

    The default ledger is ``<data_dir>/portfolio.beancount`` because
    that's the aggregate file the ``portfolio`` step writes — it
    ``include``s every per-year output, the prices file, and the
    balances file (when present), so checking it transitively checks
    everything the rebuild produced.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Whether to run bean-check at the end of every rebuild. Default
    # true: the validator catches the regression class — missed
    # ingests, writer bugs, balance drift — that would otherwise only
    # surface on the next ledger load.
    enabled: bool = True

    # Ledger entry point. Empty string (the default) is interpreted as
    # ``<data_dir>/portfolio.beancount``. Set to a different path to
    # check against a parent ledger that ``include``s the rebuild
    # output (e.g. a master ``main.beancount`` carrying user-curated
    # opens / commodities / metadata).
    ledger: str = ""

    # When true, treat ``bean-check`` warnings (in addition to errors)
    # as a failed check. Off by default because beancount emits
    # warnings on a wide range of benign conditions (missing prices for
    # holdings on certain dates, etc.) that would noise up the rebuild
    # output. Turn on when you want a strict CI gate.
    strict: bool = False


class ReconcileStep(BaseModel):
    """Configuration for the statement-balance reconciliation step.

    Reconciliation runs ``bean-check`` over the ledger, parses its
    balance-assertion failures, and writes a drift report (see
    :mod:`banking_pipeline.reconcile`). In the rebuild it runs *before*
    the plain ``check`` step, because ``bean-check`` itself exits
    nonzero on drift — running first guarantees the localised report
    (drift rows + earliest-drift + coverage gaps) is produced, and lets
    reconcile gate on coverage gaps that ``check`` structurally can't
    see (a missing assertion isn't an error).

    Off by default: it needs balance assertions to compare against, so
    it's only useful once the ``balances`` step (or a hand-maintained
    ``balances.beancount``) is in play.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Whether to reconcile at the end of every rebuild. Default false:
    # without a ``balances.beancount`` there's nothing to compare, and
    # the step degrades to a no-op warning rather than failing.
    enabled: bool = False

    # Ledger entry point. Empty string (the default) resolves to
    # ``<data_dir>/portfolio.beancount`` — the same resolution as
    # ``[post.check]``. Must ``include`` the balance assertions for
    # reconcile to see drift (the portfolio aggregate and a parent
    # ``main.beancount`` both do).
    ledger: str = ""

    # Statement-asserted balances file (the expected side). Empty string
    # (the default) resolves to ``<data_dir>/balances.beancount`` — what
    # the ``balances`` step writes.
    balances: str = ""

    # When true, escalate coverage gaps (statement months with no
    # assertion) to a failed rebuild. Drift always fails regardless of
    # this flag; gaps are warnings unless this is set.
    strict: bool = False


class ReportsStep(BaseModel):
    """Configuration for the read-only analytical report post-steps.

    Regenerates the Markdown/CSV reports from the freshly-built ingest
    output plus the statement archive, into the configured ``reports/``
    directories (``income_reports_dir`` etc. in ``[settings]``). Runs
    *before* reconcile/check so the reports always land even when
    bean-check later exits nonzero on drift.

    Off by default: it's optional output, and the valuation reports need a
    statement glob to value holdings against.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Whether to regenerate the reports at the end of every rebuild.
    enabled: bool = False

    # Per-report toggles (each on by default once the step is enabled).
    # ``income`` reads only the sidecars under ``data_dir``; the next four
    # value the statement archive (see :attr:`statements`).
    income: bool = True
    concentration: bool = True
    net_worth: bool = True
    allocation: bool = True
    portfolio_allocation: bool = True

    # ``trial_balance`` is opt-in (default off): unlike the others it reads
    # the *ledger* via ``bean-query`` (not the statement archive), so it
    # needs the bean-query binary and a tolerance-bearing root ledger. When
    # the binary is missing or the ledger won't load it warns and skips
    # rather than failing the rebuild.
    trial_balance: bool = False
    # Ledger to query for the trial balance. Empty → the ``[post.check]``
    # ledger (resolved the same way); it doubles as the source of the
    # ``inferred_tolerance_default`` options so the isolated query balances.
    trial_balance_ledger: str = ""

    # ``mandate_scorecard`` is opt-in (default off): like ``trial_balance`` it
    # reads the *ledger* via ``bean-query`` (for the ``Expenses:Pic`` costs),
    # but it also needs the statement archive for the average-invested
    # denominator. Reuses ``trial_balance_ledger`` (falling back to the
    # ``[post.check]`` ledger). Warns and skips on a missing binary / ledger.
    mandate_scorecard: bool = False

    # Statement PDF globs for the valuation reports (concentration /
    # net-worth / allocation / portfolio-allocation). Resolved like
    # :class:`Source.glob`. When empty, falls back to
    # :attr:`PostSteps.balance_statements` — the valuation reports want the
    # same statement archive the balances step consumes.
    statements: list[str] = Field(default_factory=list)

    # Grouping period for the income report: ``"tax-year"`` or
    # ``"calendar"`` (see the ``income`` command's ``--period``).
    income_period: str = "tax-year"

    @field_validator("income_period")
    @classmethod
    def _validate_income_period(cls, v: str) -> str:
        if v not in ("tax-year", "calendar"):
            raise ValueError(
                f"income_period {v!r} must be 'tax-year' or 'calendar'"
            )
        return v


class PostSteps(BaseModel):
    """Toggles for the post-ingest aggregator commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``banking-pipeline prices <data_dir>``. Re-derives the price
    # database from per-trade buy / sell annotations across every ingest
    # output. Optionally enriched by :attr:`price_statements`.
    prices: bool = True

    # Monthly-statement PDF globs consumed by the prices step. Resolved
    # the same way as :class:`Source.glob`. Ignored unless ``prices`` is
    # true. Counterpart to :attr:`balance_statements`: glob entries that
    # match documents *other* than monthly statements (annual / quarterly
    # / non-statement PDFs) are silently dropped after classification —
    # only the per-ISIN Portfolio valuation page on a monthly statement
    # carries the data the price extractor needs.
    price_statements: list[str] = Field(default_factory=list)

    # ``banking-pipeline portfolio <data_dir>``. Regenerates the central
    # account-opens + per-year-includes aggregate file.
    portfolio: bool = True

    # ``banking-pipeline balances <data_dir> --statement ...``. Off by
    # default because it requires explicit statement PDFs to consume;
    # set to ``true`` together with :attr:`balance_statements` when the
    # rebuild should refresh balance assertions.
    balances: bool = False

    # Statement PDF globs consumed by the balances step. Resolved the
    # same way as :class:`Source.glob`. Ignored unless ``balances``
    # is true.
    balance_statements: list[str] = Field(default_factory=list)

    # Operating-currency override for ``banking-pipeline portfolio``.
    # Empty list (the default) lets the CLI fall back to its own
    # default (``["GBP"]``).
    operating_currencies: list[str] = Field(default_factory=list)

    # Booking-method override for ``banking-pipeline portfolio``. Empty
    # string lets the CLI fall back to its own default (``"FIFO"``).
    booking_method: str = ""

    # Analytical Markdown/CSV reports (income / concentration / net-worth /
    # allocation / portfolio-allocation). Runs before reconcile so the
    # reports land even when bean-check later fails. Off by default.
    reports: ReportsStep = Field(default_factory=ReportsStep)

    # Statement-balance reconciliation — runs just before ``check`` so
    # its drift report lands even though bean-check exits nonzero on the
    # same drift. Off by default (needs balance assertions to compare).
    reconcile: ReconcileStep = Field(default_factory=ReconcileStep)

    # bean-check validation step — runs after every other post-step so
    # it sees the freshly-built ledger. Defaults to enabled; set
    # ``[post.check] enabled = false`` to skip.
    check: CheckStep = Field(default_factory=CheckStep)


class ImportStep(BaseModel):
    """Configuration for the optional pre-ingest archive step.

    Files raw bank downloads (a folder, a ``.zip``, or a glob of zips)
    into the dated ``<year>/<account>/`` archive tree *before* the
    ``[[sources]]`` ingest globs read from it — so a single ``rebuild``
    takes fresh downloads all the way to a checked ledger. Mirrors the
    standalone ``import`` command (see :mod:`banking_pipeline.archive`).

    Off by default: most rebuilds re-run against an archive that's
    already populated, and keeping import opt-in keeps ``rebuild``
    idempotent — a re-run with the step off moves no files. When enabled,
    the source / archive resolve from these fields first, then fall back
    to the matching ``import_*`` settings (the same fallback the
    ``import`` command uses), so a config that already sets those settings
    only needs ``enabled = true`` here.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Whether to file fresh downloads into the archive before ingest.
    enabled: bool = False

    # Glob selecting one or more sources (folders / ``.zip`` files / loose
    # PDFs), filed as one batch — e.g. the bank's periodic
    # ``~/Downloads/files-*.zip``. ``~`` is expanded. Empty falls back to
    # the ``import_source_glob`` setting. Takes precedence over
    # :attr:`source_dir` when it resolves to anything.
    source_glob: str = ""

    # A single source folder or ``.zip``. ``~`` is expanded. Empty falls
    # back to the ``import_source_dir`` setting. Used only when no
    # source glob (here or in settings) is set.
    source_dir: str = ""

    # Archive root to file into. ``~`` is expanded. Empty falls back to
    # the ``import_archive_dir`` setting.
    archive_dir: str = ""

    # Glob for files to file within each source (case-insensitive on the
    # extension). Matches the ``import`` command's ``--pattern`` default.
    pattern: str = "*.pdf"


class BatchConfig(BaseModel):
    """Top-level rebuild config loaded from ``banking-pipeline.toml``."""

    model_config = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)

    # Where per-batch beancount outputs are written. Relative paths are
    # resolved against the project root by the caller.
    data_dir: Path = Path("data")

    # Glob (relative to ``data_dir``) of stale outputs to delete before
    # rebuilding. Defaults to ``"20*.beancount"`` to wipe every yearly
    # output. Empty string skips the cleanup step.
    clean_glob: str = "20*.beancount"

    # Optional pre-ingest archive step. ``import`` is a Python keyword, so
    # the attribute is ``import_step`` with the TOML key ``[import]`` as an
    # alias (BatchConfig sets ``populate_by_name=True`` so both resolve).
    import_step: ImportStep = Field(default_factory=ImportStep, alias="import")

    sources: list[Source] = Field(default_factory=list)
    post: PostSteps = Field(default_factory=PostSteps)

    @model_validator(mode="after")
    def _check_unique_labels(self) -> BatchConfig:
        seen: set[str] = set()
        for src in self.sources:
            if src.label in seen:
                raise ValueError(
                    f"duplicate source label {src.label!r}; each label "
                    "becomes an output filename and must be unique"
                )
            seen.add(src.label)
        return self

    @model_validator(mode="after")
    def _require_sources_when_ingesting(self) -> BatchConfig:
        # An empty ``sources`` list with no post-steps would be a no-op
        # rebuild — almost certainly a config typo. We allow it (rebuild
        # will print "nothing to do") but flag obvious typos by failing
        # when the post-steps are enabled but there's no input.
        if not self.sources and (
            self.post.prices
            or self.post.portfolio
            or self.post.balances
            or self.post.reports.enabled
        ):
            raise ValueError(
                "no [[sources]] declared but [post] steps are enabled; "
                "post-steps need at least one ingest output to work against"
            )
        return self

    def resolve_data_dir(self, project_root: Path) -> Path:
        """Return ``data_dir`` resolved against the project root."""

        if self.data_dir.is_absolute():
            return self.data_dir
        return (project_root / self.data_dir).resolve()

    def stale_files(self, project_root: Path) -> Iterable[Path]:
        """Iterate over the existing output files to delete before rebuild.

        Yields the files matching ``clean_glob`` plus every
        ``*.transactions.jsonl`` sidecar (regenerated alongside each
        ``.beancount``), so a clean rebuild doesn't leave stale sidecars
        behind. Returns an empty iterator when ``clean_glob`` is empty
        (cleanup skipped entirely).
        """

        if not self.clean_glob:
            return iter(())
        data_dir = self.resolve_data_dir(project_root)
        return chain(
            data_dir.glob(self.clean_glob),
            data_dir.glob("*.transactions.jsonl"),
        )


def _split_anchor(path: Path) -> tuple[Path, str]:
    """Split an absolute glob pattern into ``(static_parent, glob_tail)``.

    ``Path.glob`` requires a relative pattern; absolute patterns must be
    decomposed into the longest static prefix and the trailing wildcard
    portion. ``~/.../2025/K-*.001/*.pdf`` →
    ``(Path('~/.../2025'), 'K-*.001/*.pdf')``.
    """

    parts = path.parts
    static_end = 0
    for i, part in enumerate(parts):
        if any(c in part for c in "*?["):
            static_end = i
            break
    else:
        # No wildcards — entire path is static.
        return path.parent, path.name
    static = Path(*parts[:static_end])
    pattern = str(Path(*parts[static_end:]))
    return static, pattern


def load_config(
    project_root: Path,
    *,
    config_path: Path | None = None,
) -> BatchConfig:
    """Load and validate the rebuild config.

    ``config_path`` overrides the default location (project_root /
    ``banking-pipeline.toml``). Raises :class:`FileNotFoundError` with
    a helpful "copy the example" message when the config file is
    absent, and :class:`pydantic.ValidationError` on schema problems.
    """

    target = config_path or (project_root / DEFAULT_CONFIG_FILENAME)
    if not target.exists():
        example = project_root / EXAMPLE_CONFIG_FILENAME
        hint = (
            f"\nCopy {example} → {target} and edit for your local "
            "folder layout."
            if example.exists()
            else ""
        )
        raise FileNotFoundError(
            f"Rebuild config not found at {target}.{hint}"
        )

    with target.open("rb") as fh:
        raw = tomllib.load(fh)
    # The ``[settings]`` table belongs to :class:`banking_pipeline.config.
    # Settings` (it reads the same file). Drop it here so BatchConfig's
    # ``extra="forbid"`` doesn't reject the shared namespace.
    raw.pop("settings", None)
    return BatchConfig.model_validate(raw)

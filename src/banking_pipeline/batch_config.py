"""TOML-driven rebuild orchestration config.

Drives the ``banking-pipeline rebuild`` command — replaces the
historical ``run.sh`` shell script. The rebuild config lives in
``banking-pipeline.toml`` (gitignored; copy from
``banking-pipeline.example.toml``) so personal Dropbox / iCloud paths
don't need to land in the repo.

Schema is intentionally narrow:

* ``data_dir`` — where per-batch beancount outputs are written.
* ``clean_glob`` — glob of stale outputs deleted before rebuild.
* ``[[sources]]`` — one entry per ingest call. Each entry has a
  ``label`` (becomes ``<data_dir>/<label>.beancount``) and a
  ``glob`` resolved against the project root.
* ``[post]`` — toggles for the prices / portfolio / balances post-steps.

Validation happens at load time via Pydantic — duplicate labels, missing
sources, and unreadable globs all surface a clear error before any work
starts.
"""

from __future__ import annotations

import sys
import tomllib
from collections.abc import Iterable
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


class PostSteps(BaseModel):
    """Toggles for the post-ingest aggregator commands."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # ``banking-pipeline prices <data_dir>``. Re-derives the price
    # database from per-trade buy / sell annotations across every ingest
    # output.
    prices: bool = True

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

    # bean-check validation step — runs after every other post-step so
    # it sees the freshly-built ledger. Defaults to enabled; set
    # ``[post.check] enabled = false`` to skip.
    check: CheckStep = Field(default_factory=CheckStep)


class BatchConfig(BaseModel):
    """Top-level rebuild config loaded from ``banking-pipeline.toml``."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    # Where per-batch beancount outputs are written. Relative paths are
    # resolved against the project root by the caller.
    data_dir: Path = Path("data")

    # Glob (relative to ``data_dir``) of stale outputs to delete before
    # rebuilding. Defaults to ``"20*.beancount"`` to wipe every yearly
    # output. Empty string skips the cleanup step.
    clean_glob: str = "20*.beancount"

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
            self.post.prices or self.post.portfolio or self.post.balances
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
        """Iterate over the existing output files matching ``clean_glob``.

        Returns an empty iterator when ``clean_glob`` is empty (cleanup
        skipped). The caller deletes the returned files before running
        the new ingests.
        """

        if not self.clean_glob:
            return iter(())
        data_dir = self.resolve_data_dir(project_root)
        return data_dir.glob(self.clean_glob)


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
        # ``tomllib`` is in the stdlib from Python 3.11; project pins
        # 3.14 in pyproject.toml so this is always available.
        if sys.version_info < (3, 11):  # pragma: no cover - defensive
            raise RuntimeError(
                "TOML config loading requires Python 3.11+; "
                "this project pins 3.14."
            )
        raw = tomllib.load(fh)
    return BatchConfig.model_validate(raw)

# Plan: FIG situs-split in the `holdings` report

**Status:** ✅ Shipped. All stages built and committed — `ruff` / `mypy` /
`pytest` (1087) green, `code-reviewer` run on the diff (one prose Warning fixed),
live-verified against the real archive (all holdings classified `foreign`, split
footer renders, unclassified callout correctly empty). See the
[CHANGELOG](../../CHANGELOG.md).

Annotate each `holdings` row **foreign (FIG-relievable)** vs **UK-situs
(taxable)**, and split the cost / unrealised-P&L totals on that axis, so the
report can be read in the light of a Foreign Income & Gains claim. Under a FIG
claim a non-UK-situs gain is relieved to nil (and a foreign loss disallowed),
so the CGT-harvesting rationale for the exact unrealised figure is void for
foreign holdings — the report currently shows one undifferentiated UK
section 104 unrealised P&L and hides that distinction.

This is a **presentation / labelling layer only**. The section 104 pool is
FIG-untouched (only the taxable *output* is residence-filtered — see
[design-decisions.md](../design-decisions.md#uk-residence-and-the-fig-regime)),
so no cost basis changes. The situs signal already exists —
`CommodityMetadata.resolved_uk_situs`, the same field the tax pipeline's
`gain_is_foreign` consumes.

## Why now / motivation

Profile shift: the user holds **no ISA**, has **no UK income**, and **will
claim FIG**. That voids the allowance/AEA-optimisation framing the report
implicitly carried and re-points it at the one distinction that now matters —
which holdings are foreign (relievable) and which are UK-situs (taxable). See
the revised [backlog](../backlog.md).

## Non-goals

- **The pool / cost basis is untouched.** No change to `match_history`,
  `UkSection104Lens`, or the sidecar substrate.
- **Not the ERI-uplift-in-relieved-years correction.** That is a *separate*
  correctness question (it changes filed CGT numbers, needs the user's
  decision and professional sign-off) — tracked as its own backlog item and
  design-decision. This plan only consumes the situs flag; it does not touch
  the ERI uplift. The two compose: the flag this plan adds is the same signal
  that task will key on. See
  [design-decisions.md](../design-decisions.md#fig-relieved-eri-does-not-uplift-the-uk-base-cost).
- **No year / claim-set input.** The report has no tax-year concept (it shows
  *current* holdings). Situs is a static property of a holding, so the column
  is always shown; the FIG framing is stated as a conditional note ("under a
  FIG claim…"), not gated on config. Keeps the report year-free.

## Design

Situs resolves per holding to one of three states, so the report never
silently defaults a mystery holding to "taxable":

- **UK** — `resolved_uk_situs is True` (GB domicile / `uk-domestic`, or an
  explicit `uk_situs = true`).
- **foreign** — `resolved_uk_situs is False` — FIG-relievable.
- **unclassified** — no `CommodityMetadata` for the key (an ISA ticker line, a
  holding with no metadata). `gain_is_foreign` defaults these to UK (no
  relief) as the *safe* default for the return, but in the report they must be
  called out, not hidden: an unclassified holding that is *actually* foreign
  would be **missing relief** (mirrors the tax-summary NOTE in
  `cli/tax.py:_write_tax_summary`).

Represent as `uk_situs: bool | None` on `HoldingRow` (`True`=UK, `False`=
foreign, `None`=unclassified).

### Data flow

`build_report` already receives `commodities`. Resolve a
`situs: dict[str, bool] = {isin: m.resolved_uk_situs for isin, m in
commodities.items()}` there and pass it to `join_holdings`, which stamps
`row.uk_situs = situs.get(key)` (→ `None` when absent). This keeps
`join_holdings` — the testable core — free of metadata *semantics* (it takes a
resolved bool map, not `CommodityMetadata`). Only the market-valued rows exist
by the time we stamp, so it is a cheap dict lookup per row.

### Totals

Split the two totals that carry FIG meaning, keeping the existing combined
totals for continuity:

- `total_unrealised_foreign_gbp` / `total_unrealised_uk_gbp` (FIG-relievable
  vs taxable). Unclassified rows go in a third `…_unclassified_gbp` bucket so
  they are never silently folded into "taxable".
- Same three-way split for `total_cost_gbp` (optional; unrealised is the one
  that matters, cost is nice-to-have — decide in Stage 1, cheap either way).

Only rows with a matched section 104 basis have an unrealised figure, exactly
as today.

## Stages

### Stage 1 — model + core (`holdings.py`), unit-tested

- Add `uk_situs: bool | None` to `HoldingRow`.
- Add the three split totals to `HoldingsReport` (foreign / UK / unclassified
  unrealised; cost split TBD).
- `join_holdings` takes `situs: dict[str, bool]`, stamps each row, accumulates
  the split totals. Default param `None` → all rows `None` (keeps existing
  callers/tests compiling until updated).
- Unit tests: a foreign holding, a UK holding, an unclassified holding; assert
  per-row `uk_situs` and that the three split totals partition the combined
  total exactly.

### Stage 2 — render + CSV, golden updated

- Markdown: add a **Situs** column (`UK` / `foreign` / `—`), and a split
  footer — foreign (relievable) vs UK (taxable) vs unclassified unrealised.
- Add the FIG note: under a FIG claim, foreign unrealised **gains** are
  relieved to nil and foreign **losses** disallowed, so the harvesting
  rationale is void for foreign rows; UK-situs is the taxable slice. Reporting
  aid, not advice.
- Add a `⚠️ unclassified situs` callout listing any `None` rows with a matched
  basis (actionable: set `uk_situs` / `domicile` in `commodities.toml`).
- CSV: add a `uk_situs` column (`uk` / `foreign` / blank).
- Regenerate the holdings golden; review the diff.

### Stage 3 — CLI wiring + live verify

- `cli/reports.py`: `build_report` already has `commodities_map`; thread the
  resolved situs through (no new CLI flag).
- Run `holdings` against the real archive; eyeball the split and the
  unclassified callout (expect every real holding classified — the tax
  pipeline already relies on this metadata, so an unclassified row here is a
  genuine `commodities.toml` gap to fix).
- Definition of Done: `ruff`, `mypy src`, `pytest`, golden diff reviewed,
  `code-reviewer` on the diff, docs (README `holdings` section +
  design-decisions "pluggable lens" note that the report is now
  situs-annotated), PII guard.

## Verification

- `uv run ruff check .` / `uv run mypy src` / `uv run pytest`
- Golden re-render diffs clean or is deliberately regenerated.
- `uv run banking-pipeline holdings …` against the archive shows the split and
  a clean (empty) unclassified callout.
- No pool / tax-pipeline behaviour change (this touches only `holdings.py`,
  its render/CSV, and the CLI wiring — none of `match_history`, the lens, or
  the sidecars).

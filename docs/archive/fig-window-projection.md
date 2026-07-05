# Plan: FIG-window multi-year projection

**Status:** ✅ Shipped and committed. `fig-projection` command
(`tax/uk/fig_projection.py` pure core + `cli/reports.py` command/render);
`ruff` / `mypy` / `pytest` (1105) green, `code-reviewer` clean (both optional
suggestions applied — negative-`--income` guard, window/row-filter helpers
extracted and unit-tested), live-verified against the real archive (correct
remaining window + act-by, material saving). The default-off rebuild toggle was
**dropped** — the command needs a per-run `--income`, so it's an on-demand
planning query, not a routine rebuild artifact. See the
[CHANGELOG](../../CHANGELOG.md).

Quantify the **cost of deferring vs. crystallising foreign unrealised gains**
across the remaining Foreign Income & Gains (FIG) window, tied to the
window-expiry deadline, so a mid-window holder can see whether — and by when —
to realise foreign gains while a claim relieves them to nil.

## The gap it fills (vs. what exists)

- `fig-advice` (`evaluate_fig_window`) already answers **"which years should I
  claim?"** — but only over *realised actuals* (income and gains that already
  happened). It is backward/current-looking on the facts.
- `tax-forecast` estimates **this year's** liability from year-to-date actuals.
- Neither looks at **unrealised** gains, and neither frames the **window
  deadline**. The strategic FIG move for this profile is forward-looking: the
  base cost of a foreign holding can be reset upward *for free* by realising its
  gain in a claimed window year (relieved to nil), shrinking the taxable gain on
  any eventual post-window disposal. That opportunity **expires** when the
  4-year window closes. This projection is that missing forward layer.

It consumes exactly what the shipped **situs-split** now surfaces:
`HoldingsReport.total_unrealised_foreign_gbp` (aggregate) and the per-row
foreign unrealised gains (`uk_situs is False`).

## The tax model

For a foreign holding carrying current unrealised gain **G**:

- **Defer** (do nothing): on an eventual **post-window** disposal, the embedded
  gain G is taxable at the CGT rate then in force → cost ≈ **G × r**. (Any
  further growth after today is taxed identically under both paths, so it is
  scenario-neutral and drops out — the projection prices only the *currently
  embedded* gain.)
- **Crystallise-in-window**: realise the holding in a claimed window year → G
  **relieved to nil** (£0 CGT); reacquire at market → base cost resets to
  today's value. The embedded gain G escapes CGT permanently.
- **Saving from crystallising ≈ G × r**, and the **act-by date** is 5 April of
  the final eligible window year (`fig_eligible_years(arrival)` max).

`r` is priced by stacking the gain through the CGT bands at the holder's
assumed income (reusing `compute_liability`'s CGT logic / `cgt_forecast_rates`),
not a flat rate — for a no-UK-income holder a large crystallisation can span the
basic→higher CGT rate boundary, which a flat rate would miss.

## Modelling decisions — settled (2026-07)

1. **CGT-rate basis: `--income` band stacking.** Price the deferred gain by
   stacking it through the CGT bands at the income level the holder supplies (as
   `tax-forecast` does), not a flat rate — so a large crystallisation that spans
   the basic→higher CGT boundary is priced correctly.
2. **Disposal model: upper-bound + flag.** Price today's embedded foreign gain
   as the **maximum** saving and state prominently that it is only real if the
   holding is sold in the holder's lifetime (CGT is uplifted to market on
   death). No assumed disposal date/horizon in the MVP.
3. **Growth: growth-free MVP.** Price only today's embedded gain. Post-today
   growth is taxed identically whether crystallised or deferred, so it is
   scenario-neutral for the headline saving — modelling it would add assumptions
   without changing the answer.

## Non-goals (kept out of this plan)

- **The per-lot "which lots to crystallise-and-rebase" advisor** with the
  30-day **bed-and-breakfast** check — that is the separate *FIG-reframed
  disposal / rebasing advisor* backlog item, the tactical follow-on. This
  projection is the *macro* "is it worth it, and by when" view that decides
  whether that advisor is worth building. It **flags** the B&B mechanic as a
  caveat but does not plan trades.
- **Execution / rebuy modelling** (market-out-of-30-days risk, transaction
  costs). Flagged as caveats, not modelled.
- **No change to the tax pipeline or the pool.** Read-only planning output,
  like `fig-advice` / `tax-forecast`. Carries the "planning aid, not tax
  advice" framing.

## Design

- New module `tax/uk/fig_projection.py` — a pure `project_fig_window(...)`
  returning a `FigProjection` (per-window-year rows + the aggregate saving +
  the act-by date), mirroring `fig_advice.evaluate_fig_window`'s pure-core
  shape. Inputs: remaining window years, foreign unrealised gain (aggregate and
  per-holding), the CGT band/rate schedule, assumed income.
- New CLI command `fig-projection` in `cli/tax.py` (sits beside `fig-advice`):
  loads the holdings **foreign unrealised** figure (reusing
  `holdings.build_report` over the statements + the situs-split), resolves the
  window from `arrival` / `fig_eligible_years`, prices the saving, and writes
  `fig-projection.md` (+ `.csv`). Reuses the statement-context loader the other
  valuation reports use.
- Rendering: a headline (crystallise-now saving, act-by date), a per-holding
  table (foreign holding, unrealised gain, indicative CGT if deferred), and a
  caveats block (B&B, disposal-conditionality, AEA/PA forfeit in a claim year).

## Stages

1. **Core** (`fig_projection.py`) — `project_fig_window` + `FigProjection`,
   pure, unit-tested (rate stacking across the basic→higher boundary; window
   from arrival; empty foreign-gains → nil saving).
2. **CLI + render** — `fig-projection` command, Markdown + CSV, caveats block.
   Test via `CliRunner` (as `test_fig_advice_cli.py`).
3. **Wire + docs** — optional default-off `[post.reports]` toggle; README +
   architecture note; DoD loop (`ruff`/`mypy`/`pytest`, `code-reviewer`, PII).

## Verification

- `ruff` / `mypy` / `pytest` including the new core + CLI cases.
- Run against the live holdings (all-foreign portfolio today): the projection
  should show the whole foreign unrealised gain as crystallisable headroom and
  the correct act-by date (5 Apr of the final eligible window year).
- No tax-pipeline / pool behaviour change (read-only planning command).

# Plan: FIG-aware ERI base-cost correction

**Status:** ✅ Shipped (core — Stages 1, 2, 4-lens; committed). Two remainder
items are deferred to the [backlog](../backlog.md): the audit-surfacing line and
the Stage 3(a) per-scenario forecast re-match — both moot until relieved-year
ERI exists. The suppression is wired into
the tax pipeline (`tax-report` / `tax-pack` / forecast) and both holdings call
sites; `ruff` / `mypy` / `pytest` (1093) green, no golden moved. **Inert on
today's data** — `fig_claim_years` (2025-26/26-27) and `eri.toml` (2023-24/24-25)
don't yet overlap — so it corrects future filings once relieved-year ERI is
entered. Remaining: Stage 3 settled as the (b) approximation (forecast); the
Stage 4 *audit-surfacing line* is deferred (see below). Signed off (user's tax
adviser, 2026-07): FIG-relieved ERI gives no base-cost uplift; for accumulation
units the equalisation element does not survive, so the whole net tranche drops.

Suppress the section 104 ERI base-cost uplift for ERI **relieved under a FIG
claim**. The uplift is anti-double-tax relief (reg 99 Offshore Funds (Tax)
Regulations 2009 — allowable only for income *charged* to tax; IFM13373). ERI
in a FIG-claimed year is relieved to nil, so it was never charged, so it must
not uplift the base cost. Applying it — as the code does today,
unconditionally — **overstates cost → understates a post-window taxable gain →
under-declares CGT**. Full rationale:
[design-decisions.md](../design-decisions.md#fig-relieved-eri-does-not-uplift-the-uk-base-cost).

## Sign-off (obtained)

The user's tax adviser confirmed (2026-07), so the two former gates are
cleared:
1. **Q1 — the reading:** FIG-relieved ERI is not charged to tax, so under
   reg 99 it gives **no base-cost uplift**. Confirmed.
2. **Q2 — equalisation:** for **accumulation units** the equalisation element
   does *not* independently reduce base cost once the ERI is relieved — so the
   whole **net** tranche is suppressed, not just the gross part. Confirmed.

Stage 1 stays a sensible first step (inert until wired — it de-risks the core
without changing any output), but nothing now blocks Stages 2–4.

## The rule (precise)

An ERI tranche `(isin, deemed_date, net_amount)` is **suppressed** from the
section 104 base-cost uplift iff **both**:
- the holding is **foreign** — `not resolved_uk_situs` (i.e.
  `gain_is_foreign(commodities.get(isin))`); **and**
- `date_to_tax_year(deemed_date)` is in the effective **`fig_claim_years`**.

Everything else (UK-situs ERI, ERI in non-claimed years) uplifts as today.
Pre-residence ERI stays handled by `eri.toml` data discipline (excluded at
source — see the non-resident corollary in design-decisions), so it is out of
scope here; an optional defensive `arrival` filter is noted in Stage 1 but not
required.

**Permanence / cumulative.** Suppression is *permanent* for that tranche, not
just within its year: a post-window taxable disposal must still not see the
uplift. Because the suppression lives in `cumulative_base_cost_adjustments`
(which builds the whole-history adjustment set the pool consumes), permanence
falls out for free — a suppressed tranche is simply never added.

**Income is untouched.** Only the base-cost *uplift* is suppressed. The ERI
*income* declaration stays exactly as today: year-scoped `compute_eri` rows,
relieved onto the FIG designation by `_partition_fig_relief`. The
`gross_income − withholding == amount` invariant is unaffected (we touch no
income figure).

**Equalisation (resolved — suppress the net tranche).** The tranche amount is
`gross − equalisation`. The adviser confirmed that for accumulation units the
equalisation element does not independently reduce base cost once the ERI is
relieved, so the whole **net** `PoolCostAdjustment` is dropped for a relieved
year — no need to split gross from equalisation. This is the single-tranche
path Stage 1 already builds; no extra branch.

## Choke point

`cumulative_base_cost_adjustments` (`tax/uk/eri.py`) is the single place the
whole-history pool uplift is assembled, and it already loops per tax year with
`commodities` in hand. All four consumers route through it:

- `cli/tax.py:_compute_tax_year` → `compute_sa108` + `match_history` (the
  filed SA108 / loss chain, for `tax-report` / `tax-pack` / `tax-forecast`).
- `cli/reports.py:holdings` → `UkSection104Lens.cost_adjustments`.
- `cli/rebuild.py` holdings step → same lens.

So the fix is one filter in the helper plus threading `fig_claim_years` from
each caller. `compute_eri` itself is **not** touched (its income rows must
stay), and `tax/uk/basis.py`'s ERI decomposition needs no change — it rebuilds
the pool with/without the adjustments it is *given*, so a suppressed tranche
simply never appears in `eri_uplift_gbp`.

## Stages

### Stage 1 — core suppression + tests (inert) — ✅ done

- `cumulative_base_cost_adjustments(...)` gained `fig_claim_years:
  frozenset[str] = frozenset()`. In the per-year merge it skips a year's
  adjustments for any foreign ISIN (`gain_is_foreign`) when
  `year in fig_claim_years`. Default empty set → **no behaviour change**.
- **Deviation from the brief:** the return stays the 2-tuple
  `(adjustments, gaps)` — the suppressed-tranches surfacing is moved to
  **Stage 4** (the only place that consumes it, the audit line). This keeps
  Stage 1 zero-caller-touch and genuinely inert, rather than churning three
  call sites for a value nothing reads yet.
- The defensive `arrival` filter was **not** added — pre-residence ERI stays
  handled by `eri.toml` data discipline (out of scope, as noted above);
  adding a second mechanism would only muddy the API.
- Tests (`tests/tax/uk/test_eri.py`, 5 new): foreign + claimed → dropped;
  UK-situs + claimed → kept; foreign + non-claimed → kept; two tranches, only
  the claimed year suppressed (permanence); empty `fig_claim_years` →
  unchanged (regression guard). `ruff` / `mypy` / `pytest` (1092) green.

### Stage 2 — wire the tax pipeline (changes filed CGT) — ✅ done

- `_compute_tax_year` passes `settings.fig_claim_years` to
  `cumulative_base_cost_adjustments`, correcting `tax-report` / `tax-pack`
  (and the forecast's shared `comp` — see Stage 3). The defensive `arrival`
  filter was not added (out of scope, per Stage 1).
- Integration test `test_sa108.py::test_fig_relieved_eri_uplift_dropped_
  raises_later_disposal_gain`: a foreign reporting fund with a 2025-26 ERI
  tranche disposed in 2026-27 → gain rises by exactly the suppressed uplift
  (£200 → £600).
- **No golden moved** (full suite 1093 green). None paired a claimed-year ERI
  with a later disposal — and on the live data there is **no overlap** at all:
  `fig_claim_years` is `{2025-26, 2026-27}` but `eri.toml` currently spans only
  `2023-24 / 2024-25`, so the correction is **inert on today's figures** and
  bites only once relieved-year (2025-26+) ERI is entered. Correct-by-
  construction, verified by tests, zero live-figure movement now.
- **`--strict` interaction:** suppression removes cost, so it can only
  *increase* a taxable gain — never understate. No new understatement blocker.

### Stage 3 — forecast scenario-correctness — ✅ decided: (b) documented

`tax-forecast` evaluates claim-vs-no-claim via `_run_scenario(True/False)`,
which re-runs only the loss chain + `compute_liability` over the **shared**
`comp` (configured-claims cost adjustments). The current-year toggle can, in
one narrow case, change this year's taxable gain: a **same-year disposal that
post-dates a same-year ERI event on a foreign holding**.

**Chosen: (b) documented approximation.** The forecast keeps the single shared
computation using the configured claims. Rationale: the current-year ERI
uplift only affects *future* disposals, so the claim-vs-no-claim *recommendation*
(this year's liability) is unaffected except in the same-year-disposal-after-
same-year-ERI case, which is second-order and usually empty. Prior claimed
years — which do affect this year's disposals — are fixed by config in both
scenarios and already corrected via the shared `comp`, so those are exact.
Option (a) (per-scenario pool re-match) is a heavier refactor for a nil-in-
practice delta; deferred unless a real case shows it matters. Documented in the
`_compute_tax_year` comment.

### Stage 4 — holdings lens ✅ done; audit surfacing — deferred

- **Done:** `cli/reports.py:holdings` and the `cli/rebuild.py` holdings step
  pass `settings.fig_claim_years`, so foreign holdings show the corrected
  (lower) cost / (higher) unrealised and `eri_uplift_gbp` drops the relieved
  tranches — the effect is already visible in the existing **`of which ERI`**
  column (a suppressed tranche simply doesn't appear there). Composes with the
  shipped [situs-split](../archive/fig-situs-split.md): the foreign rows it
  labels are exactly the ones this corrects. (Inert on live data today — no
  claimed-year ERI yet.)
- **Deferred — audit line.** A dedicated "N ERI tranche(s) (£X) suppressed as
  FIG-relieved" line in `summary.txt` / the holdings report needs the
  suppressed-tranches return (the Stage 1 deviation) plumbed through
  `cumulative_base_cost_adjustments` → `_TaxComputation` / the holdings report
  model + render. Deferred as auditability polish: the *effect* is already
  visible via `of which ERI`, and it is moot until relieved-year ERI exists.
  Pick this up when 2025-26 ERI lands. Tracked as a remaining item in the
  [backlog](../backlog.md).

## Verification / Definition of Done

- `uv run ruff check .`, `uv run mypy src`, `uv run pytest` (including the new
  Stage 1–3 cases).
- Tax goldens: any regenerated diff reviewed and deliberate; new fixture for
  the claimed-year-ERI-then-later-disposal case.
- `uv run banking-pipeline check` / `rebuild --strict` clean — suppression
  only raises gains, so no new understatement gate trips.
- Invariants hold: no `import beancount`; tax math still reads the sidecars;
  `gross_income − withholding == amount` untouched (income unchanged);
  generated ledgers not hand-edited.
- `code-reviewer` on the diff, no Critical findings.
- PII guard clean; no real figures in committed docs/tests.
- Docs: the design-decisions entry is already the settled decision (sign-off
  recorded); on landing, drop its "not yet implemented" caveat, move the plan
  to `archive/`, update the backlog item, and note in the `holdings` README
  section that foreign ERI uplift is FIG-suppressed.

## Sequencing

Situs-split ([fig-situs-split.md](fig-situs-split.md)) first (it's the lens
that makes this correction legible), then Stages 1–4 here in order —
sign-off is done, so no pause. Stage 3's (a)-vs-(b) forecast choice is the
only remaining in-flight decision.

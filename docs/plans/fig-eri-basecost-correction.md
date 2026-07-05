# Plan: FIG-aware ERI base-cost correction

**Status:** Ready — the treatment is signed off (user's tax adviser, 2026-07):
FIG-relieved ERI gives no base-cost uplift, and for accumulation units the
equalisation element does not survive independently, so the whole net tranche
is dropped for a relieved year. All stages are unblocked; not yet started.

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

### Stage 1 — core suppression + tests (inert)

- `cumulative_base_cost_adjustments(...)` gains `fig_claim_years:
  frozenset[str] = frozenset()`. In the per-year merge, skip a year's
  adjustments for any foreign ISIN when `year in fig_claim_years`. Default
  empty set → **no behaviour change** (every current caller keeps today's
  result until Stage 2/3 pass the real set).
- Return the suppressed tranches (e.g. extend the return tuple, or a small
  result object) so callers can surface them — needed for the Stage 4 audit
  line. Keep the existing `(adjustments, gaps)` shape working; add the
  suppressed set as a third element or a named-tuple field.
- Optional defensive `arrival: date | None` filter (drop any tranche with
  `deemed_date < arrival`) — belt-and-braces against a mis-transcribed
  pre-residence `eri.toml` entry. Cheap; include if it doesn't muddy the API.
- Tests (`tests/tax/uk/test_eri.py`): foreign ISIN + claimed year → tranche
  dropped; UK-situs ISIN + claimed year → kept; foreign ISIN + non-claimed
  year → kept; empty `fig_claim_years` → identical to today (regression
  guard). Plus a **cumulative** case: ERI in a claimed year, disposal in a
  later non-claimed year → the later disposal's cost excludes the uplift.

### Stage 2 — wire the tax pipeline (changes filed CGT)

- `_compute_tax_year` passes `settings.fig_claim_years` (and, if added,
  `arrival`) to `cumulative_base_cost_adjustments`. This corrects `tax-report`
  and `tax-pack`.
- Add a `test_sa108.py` / `test_section_104.py` integration case: a foreign
  reporting fund with a claimed-year ERI tranche, disposed in a later taxable
  year → higher gain than the pre-fix path. Assert the exact delta equals the
  suppressed tranche.
- Regenerate any tax golden that pairs a claimed-year ERI with a later
  disposal (there may be none today — the cumulative-ERI fix noted no golden
  paired earlier-year ERI with a later disposal; add a fixture rather than
  bend an existing one).
- **`--strict` interaction:** suppression removes cost, so it can only
  *increase* a taxable gain — it never *understates*. No new understatement
  blocker; but surface the suppression in `summary.txt` (see Stage 4).

### Stage 3 — forecast scenario-correctness — *decision point*

`tax-forecast` evaluates claim-vs-no-claim via `_run_scenario(True/False)`,
which today re-runs only the loss chain + `compute_liability` over a **shared**
`comp.history` / `comp.sa108` — both computed once with the configured claim
set's cost adjustments. Once the uplift depends on `fig_claim_years`, the
current-year toggle can, in a narrow case, change this year's taxable gain: a
**same-year disposal that post-dates a same-year ERI event on a foreign
holding** sees the uplift only in the no-claim scenario.

Two ways to resolve, pick at this point:
- **(a) Scenario-correct (preferred):** recompute the FIG-sensitive cost
  adjustments + `match_history`/`compute_sa108` inside `_run_scenario` for
  that scenario's claim set. Extract the "adjustments → pool matching" block
  from `_compute_tax_year` into a helper both call. Heavier (matches the pool
  twice) but exact.
- **(b) Documented approximation:** keep the single shared computation using
  the configured claims, and document that the forecast's *current-year*
  claim recommendation ignores the (second-order, often nil) within-year
  uplift interaction. Cheaper; acceptable only if the delta is argued
  negligible.

Prior claimed years are fixed by config in both scenarios, so only the
current-year toggle × the same-year-disposal-after-same-year-ERI case is at
stake — usually empty. Recommend (a) for correctness unless the double-match
cost is shown to matter. Add a `test_tax_forecast_cli.py` case if (a).

### Stage 4 — holdings lens + audit surfacing

- `cli/reports.py:holdings` and the `cli/rebuild.py` holdings step pass
  `settings.fig_claim_years`. Foreign holdings then show the corrected (lower)
  cost / (higher) unrealised, and `eri_uplift_gbp` drops the relieved
  tranches. **Composes with the [situs-split](fig-situs-split.md):** the
  foreign rows the split labels are exactly the ones whose ERI uplift this
  corrects — land the situs-split first so the report explains *why* those
  rows moved.
- Surface the suppression everywhere the ERI uplift is reported: a line in
  `tax-report` `summary.txt` and the holdings report — "N ERI tranche(s)
  (£X) suppressed as FIG-relieved (not charged → no base-cost uplift; see
  design-decisions)". Makes the effect auditable against Pictet's own figures
  (which *do* uplift — another reason our basis legitimately differs, like the
  existing `of which ERI` column).
- Not-tax-advice framing on any new output line.

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

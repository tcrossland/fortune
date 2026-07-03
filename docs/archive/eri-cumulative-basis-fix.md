# Plan: ERI base-cost adjustments must be cumulative in the tax pipeline

**Status:** Done. Shipped on branch `holdings-cost-basis-report`.

- **Stage 1 (reproduce):** added a failing-first CLI test —
  `test_prior_year_eri_uplifts_current_year_disposal_cost` in
  `tests/tax/uk/test_tax_report_cli.py` (buy 1000 @ £1000 in 2023-24, £100 ERI
  uplift that year, sell in 2025-26). Confirmed it reported cost £1000 / gain
  £500 against the old wiring; now cost £1100 / gain £400.
- **Stage 2 (rewire):** `_compute_tax_year` (`cli/tax.py`) keeps
  `compute_eri(…, year)` for the income rows but now computes
  `cumulative_base_cost_adjustments(…)` and passes those to both
  `compute_sa108` and `match_history`. The cumulative ERI rate-gaps
  (`eri_adj_gaps`) replace the single-year `eri_result.missing_rates` in
  `_TaxComputation.rate_gaps` (a superset — nothing dropped; set-dedup handles
  overlap). `tax_report` now threads `comp.rate_gaps` through the summary (new
  optional `_write_tax_summary(rate_gaps=…)` param) and the `--strict`
  understatement blocker. `tax-pack` / `tax-forecast` / `fig-advice` already
  read `comp.rate_gaps`, so they inherit the fix.
- **Stage 3 (re-run):** full `uv run pytest` green (1028). No existing golden
  or number moved — no prior test paired an earlier-year ERI entry with a
  later-year disposal, so nothing regressed; the correction only shows on the
  new test and on real data.
- **Stage 4 (FIG / arrival cross-check):** no double-count.
  - *FIG:* `_partition_fig_relief` moves foreign disposals to the designation
    using `r.gain_gbp`, which now carries the (larger) uplifted cost → the
    relieved gain is the true gain. The base-cost uplift and FIG relief are
    orthogonal: the uplift makes the gain figure correct, and whatever happens
    to that gain (taxed or relieved) uses the correct figure. Existing FIG
    tests still pass.
  - *arrival:* the residence filter (`is_pre_residence`) drops pre-arrival
    disposals at the *reporting* layer; adjustments are applied to the *pool*
    chronologically, exactly as pre-arrival acquisition costs already are. This
    fix doesn't change that treatment — it only makes the adjustment set
    cumulative. (Whether non-resident-year ERI *should* uplift a UK base cost
    is a separate, pre-existing rebasing question that also applied to the old
    single-year path; out of scope here, noted for the backlog.)
- **Stage 5 (docs):** design-decisions.md gains a "tax pipeline feeds
  cumulative ERI base-cost uplift" entry (and the stale clause in the holdings
  entry is corrected); CHANGELOG entry added.

**Follow-up:** regenerate the 2025-26 return (first affected filing).

## Original plan (below, for reference)

## Problem (confirmed, live)

`_compute_tax_year(year=Y)` (`cli/tax.py`) computes
`eri_result = compute_eri(…, tax_year_label=Y)` and passes
`eri_result.base_cost_adjustments` — **only year Y's** ERI base-cost uplift —
to both `compute_sa108` and `match_history`. The section 104 pool is
cumulative, so a disposal in year Y whose units accrued ERI in an *earlier*
year needs that earlier uplift in its allowable cost. It is not applied.

`compute_eri` scopes to a single tax year by design
(`eri.py`: `if not (start <= entry.fund_distribution_date <= end): continue`),
so one call can never carry prior years' adjustments. The eri.py docstring
states the intent this breaks: *"ERI is also added to the CGT base cost on a
**later** disposal (you've already been taxed on it)."* So this is a wiring
gap, not deliberate behaviour.

**Effect:** the disposal's cost is understated by the omitted prior-year
uplift → the CGT **gain is overstated** → too much tax (or too small a loss).
The loss-carry-forward chain (`loss_carryforward_chain(comp.history.rows, …)`)
consumes the same mis-costed rows, so brought-forward losses are affected too.

**Empirical confirmation** (synthetic: buy 1000 @ £1000, £100 ERI uplift in
year 1, sell in year 2):

| | cost | gain |
|---|---|---|
| pipeline (year-scoped ERI) | £1,000 | £1,000 |
| correct (cumulative ERI) | £1,100 | £900 |

**Live, not just latent.** The real `eri.toml` currently carries ERI for one
tax year (2024-25). The 2024-25 return is therefore correct (ERI year == report
year, nothing prior to omit). But the majority of those ERI-bearing funds were
disposed in **2025-26 / 2026-27** — so once a 2025-26 tax report is run, those
disposals' pool cost omits the 2024-25 uplift and the 2025-26 CGT is overstated.

## Fix

The mechanism already exists: `cumulative_base_cost_adjustments` (added by the
holdings-report work, `eri.py`) runs `compute_eri` for every tax year the
`eri` table spans and merges the per-ISIN adjustment lists.

In `_compute_tax_year`, keep `compute_eri(…, year)` for the ERI **income** rows
(only the current year's ERI income is declared), but feed the **cumulative**
base-cost adjustments to the pool:

```python
eri_result = compute_eri(txns, tax_year_label=year, …)   # income rows (unchanged)
adjustments, eri_adj_gaps = cumulative_base_cost_adjustments(txns, …)  # pool uplift
sa108 = compute_sa108(…, cost_adjustments=adjustments, …)
history = match_history(…, cost_adjustments=adjustments)
```

`match_disposals` applies adjustments chronologically, so a future-dated
adjustment lands after the current year's disposals and cannot affect them —
passing the whole-history set is safe for a year-Y SA108.

**Rate gaps.** `cumulative_base_cost_adjustments` returns the ERI GBP-rate gaps
across all years. Decide how these fold into the report's existing
`rate_gaps` / understatement-blocker channel (currently
`eri_result.missing_rates` is the single-year set). A prior-year ERI entry with
no GBP rate now becomes relevant to the current pool, so surface it.

## Stages

1. **Reproduce as a test first.** A `test_sa108` (or `test_tax_report`) case:
   ERI in year 1, disposal in year 2, assert the year-2 gain includes the
   year-1 uplift. Confirm it fails against the current wiring.
2. **Rewire `_compute_tax_year`** to feed cumulative adjustments to
   `compute_sa108` + `match_history`; keep the income rows year-scoped. Fold
   the cumulative ERI rate-gaps into the report gap channel.
3. **Re-run the full tax suite.** Any golden/number that moves must be
   hand-verified as a *correction* (prior-year ERI now applied), not a
   regression. Expect changes only where a disposal post-dates its ERI year.
4. **Doc + rationale.** Note the cumulative-adjustment requirement in
   `design-decisions.md` (or extend the holdings-report entry, which already
   explains why `compute_eri` is year-scoped and the pool is cumulative), and
   record the correction in the CHANGELOG.

## Invariants / risks

- **Tax-critical, invariant-touching** — changes filed CGT numbers. This is
  the reason it's plan-gated and hand-verified, not a drive-by edit.
- Tax math still reads the sidecars, not the ledger — unchanged.
- The `gross_income − withholding_tax == amount` model invariant is unrelated.
- `cumulative_base_cost_adjustments` re-runs `compute_eri` per year — O(years),
  negligible for this data size.
- Cross-check the interaction with the **residence / FIG** partition and the
  `arrival` filter: prior-year ERI uplift on a holding disposed post-arrival
  should behave; confirm no double-count with FIG relief.

## Definition of Done

- New failing-first test now passes; full `uv run pytest` green.
- `ruff` / `mypy src` clean.
- Every moved tax golden/number hand-verified as a correction, diff reviewed.
- `code-reviewer` run on the diff, no Critical findings.
- Docs + CHANGELOG updated; this file's status advanced.
- Note: the **2025-26** return should be regenerated after this lands (it is
  the first affected filing).

# Backlog

Candidate enhancements not yet built — a menu to pull from, not
committed work. Biased toward changes that fit the existing
data-driven, sidecar-based architecture rather than introducing a new
paradigm. Shipped features are recorded in
[../CHANGELOG.md](../CHANGELOG.md); design rationale in
[design-decisions.md](design-decisions.md).

The backward-looking tax-*reporting* area is essentially complete. The
active direction is forward-looking **tax planning & advice** (below),
built on the same sidecar substrate; what remains in reporting is FIG
presentation polish plus the broader (and weaker-fit) reporting ideas.

## Tax reporting

- **`fig-advice` refinements** — per-year income (one `--income` is
  currently assumed across the window) and run-rate projection of
  incomplete years (it uses year-to-date actuals, so a current-year
  recommendation is provisional).
- **`tax-forecast` refinements** — run-rate extrapolation of the
  in-progress year, and Scottish income-tax bands (England/Wales/NI
  only today).
- **Tax-pack PDF** — Markdown only for now, to stay dependency-light.
- **Pension (SIPP) wrapper** — the tax-exempt-wrapper choke point covers
  ISAs; a SIPP would slot in the same way if one is ever held.

## Tax planning & advice

Forward-looking, but a strong fit: each reuses an engine already built
(the section 104 pool, `prices`, the `liability` / `rates` stacking, the
`tax-forecast` machinery). All would carry the existing "planning aid,
not tax advice" framing.

- **CGT year-end harvesting advisor.** Combine the section 104 pool with
  current `prices` to compute *unrealised* gain/loss per lot, then —
  against the remaining AEA and the year's realised gains — advise how
  much more could be realised tax-free and which loss-making lots to
  crystallise to offset gains. Flag any repurchase that would trip the
  30-day bed-and-breakfast rule and undo a harvested loss.
- **Allowance-utilisation dashboard.** From year-to-date actuals, show
  used-vs-remaining for each statutory headroom: dividend allowance,
  personal savings allowance + starting-rate band, the CGT AEA, and the
  £100k–£125,140 personal-allowance taper ("60% trap") zone — to time
  income / pension contributions before 5 April. Statutory values come
  from config, as `rates.py` already does.
- **`tax-forecast` "what-if" delta calculator.** Given a hypothetical
  action — sell £X of holding Y, contribute £Z to a pension, take £W of
  dividends — recompute the forecast and report the *marginal* tax delta.
  A thin layer over the existing forecast engine that turns the estimate
  into a planning tool.
- **Pension (SIPP) annual-allowance planner.** Model contribution
  headroom against the £60k annual allowance, the high-earner taper, and
  3-year carry-forward of unused allowance, plus the relief obtained.
  (Distinct from the SIPP *wrapper* item, which is only the tax-exempt
  choke point.)
- **ISA subscription tracker.** Track the £20k annual subscription limit
  against the Vanguard ISA contributions already ingested
  (`Equity:Vgd:ISA:Contributions`). Small and self-contained.

## Bookkeeping (ingest quality)

- **Idempotent re-ingest (`ingest --append`).** The dedup *audit*
  (`dedup-check`) shipped; an incremental mode that merges new PDFs into
  an existing output, skipping already-present transactions, is deferred
  — it fights the per-document / close-directive rendering grain.
- **Confidence / audit ledger.** Persist per-document classifier
  confidence and which rule fired into a queryable index, so the
  lowest-confidence documents in a run can be surfaced for manual review
  rather than trusted silently.

## Accounting

- **FX revaluation vs. price P&L separation** on holdings — useful for
  accounting clarity and as an input to CGT.

## Financial reporting

- **Period reports beyond beancount/Fava.** Net-worth-over-time,
  per-portfolio allocation, income-by-source, and realised/unrealised
  P&L summaries rendered from the sidecars (Markdown/CSV). Would
  formalise the ad-hoc `data/*.md` artifacts into a `report` command.
- **Asset-allocation snapshot** driven by the asset-class metadata
  already in `commodities.toml` (equity/bond/cash mix over time).
- **Concentration / exposure report.** Single-holding, per-issuer,
  per-currency, and per-asset-class concentration over time, from
  holdings + `commodities.toml`. Cheap given the metadata and directly
  useful for spotting over-weight positions.

## Financial planning & budgeting

Weakest fit for the current backward-looking architecture — scope
cautiously.

- **Income/expense run-rate** from historical sidecars: trailing-12
  cashflow by category, projected forward. Low effort, reuses existing
  data.
- **Multi-year liability & cashflow projection.** Extend `tax-forecast`
  forward across several years with simple assumptions, tied to the FIG
  window expiry so the cost of deferring vs. claiming is visible. Builds
  on `fig-advice` + `tax-forecast`; forward-looking, so a firm "planning
  aid, not advice" framing.
- **Full budgeting** (envelopes, targets) is deprioritised — a different
  product, and the substrate isn't built for it.

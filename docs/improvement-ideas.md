# Improvement ideas

A backlog of candidate enhancements across bookkeeping, accounting,
reporting, planning, budgeting, and tax. Biased toward changes that
fit the existing data-driven, sidecar-based architecture rather than
introducing a new paradigm. Not committed work — a menu to pull from.

The tax-reporting backlog is essentially complete: statement-balance
reconciliation, idempotent-re-ingest dedup keying, the current-year
tax-liability forecast, residence/FIG relief, the rate-source coverage
check, the tax pack, and the multi-year FIG claim optimiser
(`fig-advice`). The remaining tax items are smaller FIG-presentation
polish (clearer designation split, unclassified-holding warnings; see the
Tax reporting section). The broader **reporting and planning** ideas are
still unstarted: confidence/audit ledger, FX-vs-price P&L split, period
reports, asset-allocation snapshot, income/expense run-rate.

## Bookkeeping (ingest quality)

- **Statement-balance reconciliation.** *(Shipped.)* The `reconcile`
  command diffs computed ledger balances against the statement-asserted
  balances per account/date, reports the full grid (not just the
  first `bean-check` failure), surfaces drift magnitude/direction,
  and flags coverage gaps (months with no statement ingested).
  Pinpoints the earliest period an account diverged so a missed or
  misclassified document is actionable. See `reconcile.py` and
  [reconciliation-plan.md](reconciliation-plan.md).
- **Idempotent re-ingest / dedup keying.** *(Audit shipped.)* Each
  sidecar line now carries a stable content `dedup_key`, and
  `dedup-check` (read-only) flags transactions sharing one as suspected
  double-counts (`EXACT` vs `POSSIBLE`). See `dedup.py`. Still open:
  an `ingest --append` incremental mode that merges new PDFs into an
  existing output, skipping already-present transactions (deferred — it
  fights the per-document/close-directive rendering grain).
- **Confidence / audit ledger.** Persist per-document classifier
  confidence and which rule fired into a queryable index, so the
  lowest-confidence documents in a run can be surfaced for manual
  review rather than trusted silently.

## Accounting

- **Second bank as a real test of the abstraction.** *(Shipped.)*
  The Vanguard UK Stocks & Shares ISA was wired through the
  writer-profile / ruleset / template seams, validating that adding a
  bank is (largely) a data-only change. It also exercised the seams in
  new ways — a two-segment `account_prefix` (`Vgd:ISA`), ticker-keyed
  commodities, and a NoOp template for paper-only doctypes.
- **FX revaluation vs. price P&L separation** on holdings — useful
  for accounting clarity and as an input to CGT.

## Financial reporting

- **Period reports beyond beancount/Fava.** Net-worth-over-time,
  per-portfolio allocation, income-by-source, and realised/unrealised
  P&L summaries rendered from the sidecars (Markdown/CSV). Formalises
  the existing ad-hoc `data/*.md` artifacts into a `report` command.
- **Asset-allocation snapshot** driven by the asset-class metadata
  already in `commodities.toml` (equity/bond/cash mix over time).

## Financial planning & budgeting

Weakest fit for the current backward-looking architecture — scope
cautiously.

- **Income/expense run-rate** from historical sidecars: trailing-12
  cashflow by category, projected forward. Low effort, reuses
  existing data.
- **Tax-liability forecast.** *(Shipped.)* `tax-forecast --income <gbp>`
  estimates the current (incomplete) tax year's UK liability, reusing the
  SA108/SA106 machinery for the year-to-date taxable amounts and stacking
  them in UK order (non-savings income → savings → dividends → CGT on the
  remaining basic-rate band), with the personal-allowance taper and
  foreign tax credit relief. Year-to-date actuals only. See
  `tax/uk/liability.py` + `tax/uk/rates.py`. Still open: run-rate
  extrapolation, and Scottish income-tax bands.
- **Full budgeting** (envelopes, targets) is deprioritised — a
  different product, and the substrate isn't built for it.

## Tax reporting (strongest existing area)

- **CGT annual exempt amount + loss carry-forward.** *(Shipped.)*
  `tax-report` now threads allowable losses across tax years (seeded by
  optional pre-ledger losses in `data/cgt-losses.toml`), applies the
  statutory deduction order (current-year losses → brought-forward
  losses down to the AEA → AEA), and optimises the mid-year rate-change
  allocation by absorbing relief against the higher-rate bucket first. It
  writes `cgt-loss-carryforward.csv` (the year-by-year chain) and a CGT
  allowance block in `summary.txt`. See `tax/uk/cgt_allowance.py`. Still
  open: the 4-year loss-claim time limit isn't enforced (losses are
  claimed automatically).
- **ISA / pension wrapper awareness.** *(Shipped for ISA.)* Each
  `Transaction` carries an optional `account_wrapper`; `tax-report`
  filters `is_tax_exempt` transactions at a single choke point before
  any `compute_*` / `match_history` call, so the Vanguard ISA's
  disposals and income never reach SA108 / SA106 / the loss chain. See
  `TAX_EXEMPT_WRAPPERS` in `models.py`. Still open: a pension (SIPP)
  wrapper if one is ever added.
- **Residence + 4-year FIG relief.** *(Shipped.)* `uk_residence_start_date`
  drops pre-residence years (and the non-resident part of a split arrival
  year); `fig_claim_years` relieves foreign income + non-UK gains for a
  FIG-eligible year while forfeiting the PA + AEA. `tax-report` partitions
  relieved foreign items onto `fig-designation.csv`; `tax-forecast`
  computes the year with and without the claim and recommends the cheaper.
  See `tax/uk/residence.py`. Out of scope: the 10-prior-non-resident
  eligibility test, temporary-non-residence clawback, and former
  remittance-basis transitional rebasing/TRF.
- **Tax pack.** *(Shipped.)* `tax-pack` renders `tax-pack.md`, a per-year
  filing aid tying the computed SA108/SA106 figures to HMRC form boxes
  (CGT listed-shares boxes + allowance computation, foreign
  dividends/interest with FTCR, offshore income gains, deeply-discounted,
  ERI, and the FIG designation). Pure renderer in `tax/uk/tax_pack.py`;
  box numbers are caveated (HMRC re-numbers the forms). Still open: a PDF
  rendering (Markdown only for now, to stay dependency-light).
- **Rate-source coverage check.** *(Shipped.)* An unconvertible amount
  (no per-tx `gbp_rate`, no source rate) is excluded from the figures, so
  it silently understates. `to_gbp` returning `None` is now captured as a
  `RateGap(isin, currency, month)` on each report's `missing_rates`;
  `tax-report` / `tax-forecast` warn with the exact HMRC CSV row to add,
  and `--strict` turns any gap into a non-zero exit. See
  `tax/uk/currency.py`.

### FIG follow-ups (surfaced running the 2025-26 pack)

- **FIG claim is a multi-year, loss-aware decision.** *(Shipped.)*
  `fig-advice --income <gbp>` brute-forces every claim subset across the
  eligible window, threading the loss chain per subset so a year's
  disallowed foreign losses (and the knock-on to later years' carried
  losses) are reflected, and recommends the cheapest pattern. See
  `tax/uk/fig_advice.py`. Still open: per-year income (one `--income` is
  assumed across the window) and run-rate projection of incomplete years
  (year-to-date actuals only, so a current-year recommendation is
  provisional).
- **Separate disallowed losses in the FIG designation.** The designation
  table mixes relieved income, relieved gains, and disallowed foreign
  losses, and nets them in the total — which can understate the true cost
  of the claim. Split them out (relieved income / relieved gains /
  disallowed losses) and surface the foregone loss relief explicitly.
- **Flag unclassified holdings that escape FIG relief.** A disposal with
  no `commodities.toml` entry defaults to UK-situs, so under a claim it
  stays taxable (and a genuinely foreign one wrongly lands on SA108).
  `tax-report` already emits `WARN_UNCLASSIFIED`, but the pack / FIG path
  should call out that an unclassified holding may be missing relief, to
  prompt a metadata fix before filing.

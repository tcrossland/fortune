# Improvement ideas

A backlog of candidate enhancements across bookkeeping, accounting,
reporting, planning, budgeting, and tax. Biased toward changes that
fit the existing data-driven, sidecar-based architecture rather than
introducing a new paradigm. Not committed work — a menu to pull from.

Statement-balance reconciliation and idempotent-re-ingest dedup keying
have since shipped; the standout remaining high-leverage item is the
**current-year tax-liability forecast**, which builds directly on the
section-104 engine and the AEA/loss-carry-forward chain. Pair it with
the **rate-source coverage check** so the forecast isn't built on
silent `None` rates.

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
- **Tax-liability forecast** for the current (incomplete) tax year so
  there are no April surprises. Most synergistic planning feature
  with what is already computed.
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
- **Tax pack** — a single per-year Markdown/PDF summary tying the
  SA108/SA106 CSVs to actual HMRC box numbers.
- **Rate-source coverage check** — warn when a disposal/dividend has
  no `gbp_rate` and no HMRC monthly rate available, rather than
  silently producing `None`.

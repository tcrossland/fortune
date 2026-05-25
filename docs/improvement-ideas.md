# Improvement ideas

A backlog of candidate enhancements across bookkeeping, accounting,
reporting, planning, budgeting, and tax. Biased toward changes that
fit the existing data-driven, sidecar-based architecture rather than
introducing a new paradigm. Not committed work — a menu to pull from.

Top three by leverage-to-effort: **statement-balance reconciliation**,
**idempotent re-ingest**, and **current-year tax-liability forecast** —
all three build on substrate that already exists (balance assertions,
JSONL sidecars, the section-104 engine).

## Bookkeeping (ingest quality)

- **Statement-balance reconciliation.** A `reconcile` command that
  diffs computed ledger balances against the statement-asserted
  balances per account/date, reports the full grid (not just the
  first `bean-check` failure), surfaces drift magnitude/direction,
  and flags coverage gaps (months with no statement ingested).
  Pinpoints the earliest period an account diverged so a missed or
  misclassified document is actionable. See
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

- **Second bank as a real test of the abstraction.** The README
  claims adding a bank is data-only; actually wiring one (even a
  stub) would validate the writer-profile / ruleset seams before
  they ossify around Pictet.
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
- **ISA / pension wrapper awareness** so wrapped accounts are
  excluded from CGT/SA106.
- **Tax pack** — a single per-year Markdown/PDF summary tying the
  SA108/SA106 CSVs to actual HMRC box numbers.
- **Rate-source coverage check** — warn when a disposal/dividend has
  no `gbp_rate` and no HMRC monthly rate available, rather than
  silently producing `None`.

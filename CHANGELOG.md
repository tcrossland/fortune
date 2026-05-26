# Changelog

Notable shipped features, newest first. This is a single-user project
with no tagged releases, so entries are chronological rather than
versioned. The forward-looking backlog lives in
[docs/backlog.md](docs/backlog.md); the *why* behind the bigger
decisions is in [docs/design-decisions.md](docs/design-decisions.md).

## UK tax

- **FIG designation loss split** — the `fig-designation.csv` / tax-pack
  table now buckets each relieved item as income / gain / *disallowed
  loss* (a `kind` column) instead of netting a forfeited foreign loss
  silently into one total. The summary and pack report relieved income,
  relieved gains, and disallowed losses as separate subtotals so the
  foregone loss relief is visible. (`tax/uk/residence.py`)
- **`fig-advice`** — multi-year Foreign Income & Gains claim optimiser.
  Brute-forces every claim subset across the eligible window, threading
  the loss-carry-forward chain per subset so disallowed foreign losses
  propagate correctly, and recommends the cheapest pattern.
  (`tax/uk/fig_advice.py`)
- **`tax-pack`** — per-year Markdown filing aid tying the computed
  SA108/SA106 figures to the HMRC form boxes. (`tax/uk/tax_pack.py`)
- **Rate-source coverage check** — an unconvertible amount is captured as
  a `RateGap(isin, currency, month)` and surfaced with the exact HMRC CSV
  row to add; `--strict` turns any gap into a non-zero exit on
  `tax-report` / `tax-forecast`. (`tax/uk/currency.py`)
- **UK residence + 4-year FIG relief** — `uk_residence_start_date`
  applies split-year treatment (pre-residence income/gains drop out);
  `fig_claim_years` relieves foreign income + non-UK gains for a claimed
  year at the cost of the personal allowance + CGT annual exempt amount.
  (`tax/uk/residence.py`)
- **`tax-forecast`** — current-year UK liability estimate, stacking
  income in UK order with the PA taper and foreign tax credit relief.
  (`tax/uk/liability.py`, `tax/uk/rates.py`)
- **CGT annual exempt amount + loss carry-forward** — threads allowable
  losses across tax years in HMRC's statutory deduction order, optimising
  the mid-year rate-change allocation. (`tax/uk/cgt_allowance.py`)
- **ISA wrapper awareness** — ISA-wrapped transactions are tax-exempt and
  filtered at a single choke point before any tax computation.
- **Excess reportable income, deeply discounted securities, opening
  positions, CGT rate-change split, foreign WHT** — the Pictet-report
  parity follow-ups. (`tax/uk/eri.py`, `tax/uk/sa108.py`, `tax/uk/sa106.py`)
- **`tax-report`** — SA106 / SA108 CSV inputs from the JSONL sidecars,
  with section 104 / same-day / 30-day matching in GBP.

## Bookkeeping & accounting

- **Vanguard UK Stocks & Shares ISA** — a second bank wired through the
  writer-profile / ruleset / template seams (two-segment account prefix,
  ticker-keyed commodities, NoOp templates).
- **`reconcile`** — statement-balance reconciliation: diffs computed
  ledger balances against statement assertions across the whole grid,
  localising the earliest drift and flagging coverage gaps.
  (`reconcile.py`)
- **`dedup-check`** — read-only audit flagging double-counted
  transactions via a stable content `dedup_key`. (`dedup.py`)

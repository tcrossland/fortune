# Backlog

Candidate enhancements not yet built — a menu to pull from, not
committed work. Biased toward changes that fit the existing
data-driven, sidecar-based architecture rather than introducing a new
paradigm. Shipped features are recorded in
[../CHANGELOG.md](../CHANGELOG.md); design rationale in
[design-decisions.md](design-decisions.md).

The tax-reporting area is essentially complete; what remains is FIG
presentation polish plus the broader (and weaker-fit) reporting and
planning ideas.

## Tax reporting

- **Flag unclassified holdings that escape FIG relief.** A disposal with
  no `commodities.toml` entry defaults to UK-situs, so under a claim it
  stays taxable (and a genuinely foreign one wrongly lands on SA108).
  `tax-report` already emits `WARN_UNCLASSIFIED`; the pack / FIG path
  should additionally call out that an unclassified holding may be
  *missing* relief, to prompt a metadata fix before filing.
- **`fig-advice` refinements** — per-year income (one `--income` is
  currently assumed across the window) and run-rate projection of
  incomplete years (it uses year-to-date actuals, so a current-year
  recommendation is provisional).
- **`tax-forecast` refinements** — run-rate extrapolation of the
  in-progress year, and Scottish income-tax bands (England/Wales/NI
  only today).
- **Tax-pack PDF** — Markdown only for now, to stay dependency-light.
- **CGT 4-year loss-claim time limit** — losses are currently claimed
  automatically; the time limit isn't enforced.
- **Pension (SIPP) wrapper** — the tax-exempt-wrapper choke point covers
  ISAs; a SIPP would slot in the same way if one is ever held.

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

## Financial planning & budgeting

Weakest fit for the current backward-looking architecture — scope
cautiously.

- **Income/expense run-rate** from historical sidecars: trailing-12
  cashflow by category, projected forward. Low effort, reuses existing
  data.
- **Full budgeting** (envelopes, targets) is deprioritised — a different
  product, and the substrate isn't built for it.

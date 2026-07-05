# Plan: parse Pictet's "UK Tax Report" as a CGT/income cross-check

**Status:** ✅ Shipped (parser + cross-check + `reconcile-uk-tax` CLI).
Validated against the real FY24-25 report: 21 securities parsed, presence clean
(0 missing disposals), income + chargeable gains match after folding ERI into
the pipeline income totals; allowable-loss / offshore mismatches surfaced as
indicative (FX). `ruff` / `mypy` / `pytest` (1127) green, `code-reviewer` clean
(one docstring Warning fixed). Refinement made in build: aggregate mismatches
are *indicative*, not build-material — only a missing Pictet disposal
(`pictet_only`) gates the build, since Pictet's average FX makes the aggregates
diverge every year. See the [CHANGELOG](../../CHANGELOG.md).

Parse Pictet's annual **"Income and capital gains UK"** report (its own
`INCOME_CAPITAL_GAINS_UK` doctype, filed to `<year>/tax/`) and use it as a
**tolerance cross-check** against the pipeline's computed SA108 / SA106 for the
year — the way `completeness` / `reconcile-transactions` cross-check the cash
and trade legs. It is Pictet's own **GBP, Section-104, UK-basis** computation —
the same substrate an adviser files from — so it validates the pipeline's
capital-gains and foreign-income figures against an independent UK computation,
catching a missing disposal, a mis-classified reporting status, or a cost-basis
divergence that today only surfaces by hand (as in the 2024-25 comparison).

## Why this, and why now

The manual 2024-25 reconciliation showed the pipeline's CGT diverging from the
filed return, and traced it to FX convention (see
[design-decisions](../design-decisions.md#the-gbp-cost-basis-is-a-consistent-spot-source-not-the-custodians-booked-rate)).
That investigation was done by eye against the adviser's schedule. This report
is the machine-readable version of that schedule, issued every year — so the
cross-check becomes a rebuild step instead of a manual exercise. It supersedes
the *"Pictet tax reports as a reconciliation target"* backlog idea for **UK**
purposes (that item eyed the Spanish EUR/FIFO reports; this GBP UK report is the
right target — no Spanish-locale tokenisation, and it *is* the UK basis).

## What the report contains (from the FY24-25 copy)

- **Currency GBP**, per **account** (mandate) — "capital gains and losses are
  calculated separately for each account… not consolidated." Section-104
  pooled. FX: **average exchange rate** (Pictet's own, *not* spot).
- Table of contents: **2. Income** (UK income overview / overseas income
  overview / overseas income **detail**), **3. Capital Gain** (overview /
  **detail**), **4. Transactions** (per-transaction detail), 5. Private Equity,
  6. Disclaimer. ~29 content pages.
- Gains computed on **zero or estimated cost** where Pictet lacks the
  acquisition history (transferred-in securities) are **highlighted green** — a
  built-in "this basis is unreliable" flag.

## What to parse (MVP)

1. **Capital gain tax detail** — per-security (or per-disposal) proceeds /
   allowable cost / gain / loss in GBP, per account.
2. **Overseas income detail** — foreign dividends / interest by source with
   withholding tax, GBP.
3. **The overviews** (aggregate totals) — a cheap sanity anchor to check the
   detail parse sums correctly.

A multi-column numeric-table parser in the `balances_extract` / `prices_extract`
mould. English + GBP, so no Spanish tokenisation — the parsing risk is column
alignment / multi-page table continuation, not number locale.

## The cross-check

Compare, for the tax year, Pictet's parsed figures against the pipeline's
computed `Sa108Report` / `Sa106Report`:

- **Capital gains:** Pictet's aggregate gain / loss (summed across accounts, to
  match the pipeline's NIF-level pool) vs the pipeline's SA108 total, within a
  **tolerance** (see below). Per-security drill-down flags a security present in
  one but not the other (a missing disposal, or a reporting-status routing
  difference — SA108 vs offshore-income-gains).
- **Income:** Pictet's overseas dividend / interest + WHT totals vs the
  pipeline's SA106 dividends + interest.

Output a Markdown + CSV findings report (`pictet-uk-reconcile/…`), MATCH /
MISMATCH / MISSING per line, exiting non-zero on a **material** discrepancy;
wired as an optional `[post.*]` rebuild step. A NO_OUTPUT archive doctype is
promoted to a *parsed* input only for this reader — it is **never** fed to the
tax pipeline (the pipeline computes from the sidecars; this only checks it).

## Non-goals

- **Not a source of truth / not a tax feed.** The pipeline's SA108/SA106 stay
  computed from the JSONL sidecars; this report only *cross-checks* them. Never
  import its figures into the return.
- **Not exact matching.** Pictet uses an average FX and per-account pooling; the
  pipeline uses a chosen spot/monthly source and a NIF-level pool. The numbers
  **will not tie to the penny** — the check is for *material* divergence, not
  reconciliation to zero.
- **Not the Spanish IRPF reports** (EUR/FIFO) — those remain a separate,
  management-view idea.
- **Not the Transactions / Private Equity sections** in the MVP (the CG + income
  detail carry the reconciliation value; the Transactions section is a later
  enhancement if per-lot drill-down is wanted).

## Decisions (settled)

1. **Tolerance model — aggregate band + exact per-security presence.** A
   generous tolerance on the aggregate gain/loss and income totals (FX
   convention alone moved 2024-25 CGT ~18%, so a tight band would cry wolf
   yearly), **plus** an exact *presence* check per security — a security in
   Pictet's report but not the pipeline's SA108 (or vice versa) is the real bug
   signal (a missing/extra disposal, a reporting-status routing difference), and
   presence is FX-independent. Only the aggregate is tolerance-checked; presence
   is exact.
2. **Per-account vs NIF — aggregate first.** Compare Pictet's totals summed
   across mandates to the pipeline's NIF-level pool. A per-mandate split is a
   later refinement if the aggregate check surfaces something it can't localise.
3. **Green/zero-cost lines — detect zero/absent cost, surface as informational.**
   The green highlight isn't in the extracted text, so detect a zero or missing
   allowable cost on a Pictet line instead; report it as an *expected*
   divergence (the pipeline likely has the real cost via
   `opening-positions.toml`), never a failure.

## Stages

1. **Classify + text fixture.** Confirm the doctype classifies (it already
   does), add a scrubbed text fixture, and pin the section anchors.
2. **Parser** (`pictet_uk_tax_extract.py`) — CG detail + overseas income detail
   + overview totals → typed rows. Unit-tested against the fixture; assert the
   detail sums to the overview.
3. **Cross-check + CLI** — a `reconcile-uk-tax` command (+ optional `[post.*]`
   toggle) diffing the parsed report against the computed SA108/SA106, Markdown
   + CSV findings, tolerance + presence checks per the decisions above.
4. **Docs + DoD** — README (a new validation command), architecture, PII scrub
   of the fixture, `code-reviewer`.

## Verification

- `ruff` / `mypy` / `pytest`, new parser + cross-check tests.
- Run against the real FY24-25 report: the aggregate lands within tolerance and
  the per-security presence check is clean (or flags a genuine, explicable
  divergence — e.g. the crypto/green-cost lines).
- No change to the tax pipeline's computed figures (read-only cross-check).

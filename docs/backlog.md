# Backlog

Candidate enhancements not yet built — a menu to pull from, not
committed work. Biased toward changes that fit the existing
data-driven, sidecar-based architecture rather than introducing a new
paradigm. Shipped features are recorded in
[../CHANGELOG.md](../CHANGELOG.md); design rationale in
[design-decisions.md](design-decisions.md).

A handful of these ideas have graduated from a one-line menu entry to a
full implementation brief in [plans/](plans/) — designed but not yet
built. None is in flight right now; the one open brief is the **Revolut
contra-leg reclass** (a bookkeeping tidy-up). When a plan ships, its line
moves to the CHANGELOG and the plan moves to `archive/`. (Most recently
shipped: the **statement-completeness** transaction cross-check — see the
CHANGELOG and `archive/statement-completeness.md`.)

For the *correctness and robustness* gaps in the reporting subsystems
(both tax reporting-status and the analytical reports) — as distinct from
the new-feature ideas below — see the [reporting audit](reporting-audit.md),
now **fully resolved** (all P0 / P1 / P2 items shipped; see the CHANGELOG).
It stays as a dated snapshot of that work, not a pointer to open tasks; any
future correctness items graduate into this backlog.

The backward-looking tax-*reporting* area is feature-complete but carries
the correctness caveats the audit catalogues. The active *new-feature*
direction is forward-looking **tax planning & advice** (below), built on
the same sidecar substrate; what remains in reporting is FIG presentation
polish plus the broader (and weaker-fit) reporting ideas.

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
  30-day bed-and-breakfast rule and undo a harvested loss. *Foundation:* the
  per-holding unrealised table this needs is the
  [holdings cost-basis report](archive/holdings-cost-basis-report.md)
  (shipped) — build on it: this is the headroom query +
  advice + prospective bed-and-breakfast check layered on top.
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
- **`reconcile-export` — cross-check the sidecars against the portal
  Transactions export.** Pictet's e-banking "Transactions" report exports as
  a machine-readable workbook (one row per leg; `Order nr.` is the join key,
  ISIN and per-leg amounts present) covering every trade type across both LU
  mandates over multi-year windows. Same idea as `completeness` (which diffs
  only the current-account cash statement) but ID-keyed and covering
  securities, FX, fees, and income. A one-off reconciliation of a full
  five-year export matched every ID-keyed sidecar transaction with the
  cash-leg amount agreeing to the cent, and the only unmatched export rows
  were benign (FX-forward *open* legs, which we book at settlement, and the
  order-number-less doctypes above) — so this is a low-risk cross-check to
  formalise. Design notes: the export's GBP columns are **broker-side FX,
  not HMRC rates** (cross-check only, never feed tax); it splits one logical
  trade into multiple legs (aggregate by `Order nr.` before comparing); and
  it's a bulk feed with no per-document provenance, so it's a reconciliation
  input, not an ingest source. The order-number capture this depends on is
  **shipped** — every sidecar row now carries `transaction_number` (see the
  CHANGELOG), so the join is fully ID-based. (`limit_extension` is the one
  doctype that carries an order number but never appears in this export — a
  credit-facility event — so it won't cross-check here regardless.)
- **`reconcile-holdings` — cross-check the pool against the portal Holdings
  export.** The position-level sibling of `reconcile-export` (which is
  transaction-level). Pictet's e-banking `Holdings` xlsx is a per-position
  snapshot (`Account nr.`, ISIN, quantity, market value, **Net cost (GBP)**,
  and the price-vs-FX-split unrealised columns) across both mandates. Two
  checks fall out, both validated in a one-off run against a 2026-07-03 export:
  (a) **quantity** — join by ISIN (the pool is NIF-level; consolidate the
  export across mandates) and assert export qty == section-104 `pool_qty`; on
  the spot date every security matched the pool (siding with it over the stale
  month-end statement), a direct corroboration of the `holdings` drift-
  classifier's *timing* verdicts; (b) **cost basis** — Pictet `Net cost (GBP)`
  vs the section-104 lens reconciled within ~1% aggregate (per-holding mostly
  <2%, FX-convention-driven; see the source-options item). Low-risk to
  formalise as a periodic tolerance check. Same caveats: broker-side FX,
  Pictet book cost (a third basis), spot (not month-end) valuation date — a
  management-view cross-check, never a tax feed. Overlaps the deferred
  balance-sheet phase-4 cost-basis overlay.
  *Prefer the `.csv` form of the Holdings export* (same 90 columns as the
  `.xlsx`, one row per position) — stdlib-parseable, no `openpyxl`, same
  format family as the cash-statement CSV: `;`-delimited, CRLF, `YYYY/MM/DD`
  dates, dot-decimal (high precision, no thousands sep), **bare `Account nr.`**
  (`173837.001`, no K-/P- letter — resolve via `lettered_portfolio_map` like
  the completeness CSV path does). Decode **cp1252** to be safe (the export
  family is Windows-1252; a given Holdings file may be ASCII-clean, but an
  accented holding name would need it). When this is built, the CSV cash-
  statement parser (`parse_cash_statement_csv`) is the pattern to mirror.
- **Portal `Cash statements` export as a `completeness` input.** The
  cash-ledger sibling of `reconcile-export`/`reconcile-holdings`, and a cleaner
  source for the **shipped** `completeness` command, which today parses the
  current-account cash ledger out of a `Financial-statement` PDF and *recovers
  each movement's sign from the running-balance delta*. The e-banking `Cash
  statements` export gives the same ledger structured across both mandates and
  all currency sub-accounts, with a **signed `Net amount`**, a **running
  `Balance` per sub-account** (`prev + net == balance` holds exactly — a free
  integrity self-check, no sign-recovery needed), and an `Order nr.` on every row
  (ID-joinable to the sidecar `transaction_number`). Validated by an `Order nr.`
  join over 2021→2026 (both the `.xlsx` and `.csv` forms — identical result):
  **0 missing** (every cash movement ingested) and every ingested transaction
  absent from the ledger legitimately cash-neutral (fund→fund switches, limit
  extensions, in-specie receipts) or settlement-timing (see rule 3).

  **Drop-in evidence:** the CSV also reconciles *exactly* to the shipped
  command's own PDF parser. Re-running `completeness` over the 4 archived
  `Financial-statement-*.pdf` (K mandate, periods ending 2021-12-31 →
  2023-06-30 — the only ones ever downloaded) and comparing each statement's
  parsed `CashLine`s against the CSV rows for the same period matched on
  `(value_date, currency, signed amount)` gave **284/284 lines, 0 PDF-only,
  0 CSV-only** across the four periods. The parser's sign-recovery-from-
  balance-delta (its trickiest, most failure-prone step) reproduces the CSV's
  *native* signed amounts to the cent — so the CSV can replace the PDF
  cash-ledger parser with no change in results, and drop the sign-recovery
  logic entirely. The CSV also extends coverage the PDFs don't: only K
  through 2023-06-30 was ever pulled as a `Financial-statement`, whereas the
  CSV spans both mandates to 2026.

  **Prefer the `.csv` export over the `.xlsx`.** Same 22 columns and the same
  `Order nr.` key, but `.csv` is stdlib-parseable (`csv` module — no `openpyxl`
  dependency, so it stays clear of the licence-hygiene surface too) and
  plain-text. Its format needs three things right, one of them a real gotcha:
  - **Encoding is Windows-1252 (cp1252), *not* UTF-8** — the `°` in `N° de
    transacción` / `Gastos … 3° trimestre` is byte `0xb0`, so a naive UTF-8
    read *crashes*. Decode `cp1252` (or `latin-1`).
  - **Delimiter is `;`** (semicolon), line endings **CRLF** (read with
    `newline=''`).
  - Dates are `YYYY/MM/DD` strings; numbers are dot-decimal with no thousands
    separator (clean `Decimal`).

  **Export it *by value date*, not booking date.** `completeness` keys each cash
  line on its `settlement_date`, which *is* the statement's value date
  (`statement_completeness.py`), so the value-date variant matches the command's
  match key and its running `Balance` is on the same settlement basis as the
  month-end statement balance assertions `reconcile` checks. Both variants carry
  the same `Order nr.` set, so a *presence* diff is basis-independent; the basis
  matters for the running balance and for near-edge timing. Three hygiene rules,
  each learned from a bad download this session:
  1. **Export all currency sub-accounts.** The portal's currency selector
     defaults to a subset — one download silently dropped HKD, another dropped
     EUR (the largest, 458 rows). The ID-keyed diff caught both, but a naive
     reader wouldn't.
  2. **Use value date** (above) so the reader matches the shipped command.
  3. **Cut the value-date window past the latest settlement.** On a value-date
     export, a trade executed near the edge but settling later sits *beyond* the
     horizon (e.g. trades on 30.06–02.07 value-dated 06.07 were absent from an
     export cut at value date 03.07) — correctly ingested (trade-dated), just
     not yet on the value-date ledger. `completeness` already excludes
     out-of-window events, so this is expected, not a gap; the booking-date
     variant *includes* them (booked earlier), which is the visible sign of the
     two bases diverging by the settlement lag.

  A bulk feed with no per-document provenance, so a reconciliation input, not an
  ingest source.
- **`prune-tax-reports` differing-twin convergence.** A non-retained daily
  P&L report whose same-named `_superseded/` twin *differs* in content can't
  be superseded (move-aside no-ops on the name collision) and warns on every
  run; the `moved N` summary counts that non-move too. The byte-identical
  stray-convergence step (shipped) settles identical re-files but not
  differing ones. Fix is upstream — a `pruned-dates` manifest so import
  doesn't re-file an already-pruned daily — or rename-on-collision in
  `_superseded/`. Surfaced in the stray-convergence review (commit `e1566ea`).

## Accounting

- **FX revaluation vs. price P&L separation** on holdings — useful for
  accounting clarity and as an input to CGT. Note: Pictet's tax reports
  already compute this split, so it may come for free with the
  P&L-report-by-parsing item under "Financial reporting" rather than
  needing independent derivation.

## Financial reporting

- **Interactive balance sheet (`balance-sheet`) — MVP shipped.** A single
  self-contained, offline HTML artifact with a client-side as-of date
  scrubber: recompute every account's value, the Assets / Liabilities /
  net-worth totals, the account tree and allocation donut for any date.
  Reads the ledger via `bean-query` (a ledger construct, like the trial
  balance, not statement marks). Phases 1–3 (dataset → artifact → CLI +
  rebuild wiring) shipped — see
  [archive/interactive-balance-sheet.md](archive/interactive-balance-sheet.md).
  Still open (phase 4, deferred as non-goals): a **cost-basis / unrealised
  P&L column** (needs per-date FIFO booking) and the **statement-assertion
  drift overlay** (the dataset already carries the assertions; rendering
  overlaps `reconcile`). *The cost-basis half is available from the shipped*
  [holdings cost-basis report](archive/holdings-cost-basis-report.md) *— its
  residual section-104 pool per holding is the same substrate this column
  needs.*
- **Holdings cost basis + unrealised P&L (`holdings`) — shipped (UK).** Joins
  the latest statement market value with a UK section-104 GBP cost basis (from
  the sidecars, ERI-adjusted) and reports per-holding unrealised P&L, with a
  statement-qty ↔ pool-qty cross-check. Pluggable per-jurisdiction basis lens
  (`basis_lens.py`); the reserved **EUR/Spanish lens** (`--basis es`) is the
  open slot — for a possible UK→Spain residence change, where the ISA is no
  longer tax-exempt and Spanish FIFO/EUR rules apply. **Before building that
  lens, settle the cost-basis source** (see the next item) — a bare stub was
  deliberately not built.
  - *FIG-awareness (enhancement).* The report computes one undifferentiated
    UK section-104 unrealised P&L; it ignores situs. Under a **FIG claim**
    (`fig_claim_years`), foreign (non-UK-situs) gains are relieved to nil, the
    CGT AEA is forfeited, and foreign losses are disallowed — so the
    CGT-harvesting rationale for the exact basis is void for foreign holdings
    in a claim year, and the unrealised P&L needs reading in that light.
    Annotate / split each row **foreign (FIG-relievable) vs UK-situs
    (taxable)** using `CommodityMetadata.resolved_uk_situs` — already in the
    lens's `commodities` and used by the tax pipeline (`gain_is_foreign`), so
    the data is in hand. The pool itself is FIG-untouched (only the taxable
    *output* is residence-filtered), so nothing in the basis changes — this is
    a presentation/labelling layer. Related open question the ERI cumulative
    fix already flagged: whether **relieved-year ERI** should uplift a UK base
    cost at all (a rebasing question); the lens applies ERI uplift
    unconditionally, so it inherits whatever the pipeline settles.
- **Unrealised-P&L source options (decide before the EUR lens / `pnl` report).**
  Three ways to get a non-UK / management-view cost basis + unrealised P&L,
  weighed:
  1. *Read the columns already in the Pictet monthly statement.* The
     "Portfolio valuation" table we **already parse** (for balances + prices)
     carries per-holding **Net cost (GBP)**, **Net unrealised (Orig.)** and
     **Net unrealised (GBP)**. Pros: the data is in a file we already ingest;
     **per-portfolio** (K/P statements are separate — the dimension the
     NIF-level tax reports lack); monthly cadence; English/GBP-reference, so no
     Spanish tokenisation; a multi-column numeric-table parser like
     `balances_extract`. Cons: it's Pictet's **book cost** (a third basis
     definition — not UK section-104, not Spanish-FIFO), so a management view
     only; column wrapping/alignment is the parsing risk; the P statement is
     by-name (reuse `build_statement_name_index`).
  2. *Parse Pictet's separate Spanish IRPF unrealised report* (the original
     plan). Pros: explicit price/FX split; genuine Spanish-FIFO/EUR figures.
     Cons: Spanish-locale tokenisation is the bulk of the work; NIF-level (no
     portfolio dim); event-triggered cadence; a whole new doctype to maintain.
  3. *Compute native/EUR FIFO ourselves from the sidecars.* Pros: full control.
     Cons: a **second** cost-basis engine (section-104 is GBP-pooled only) — the
     re-implementation risk the repo avoids.
  Recommendation: for a management-view / rough Spanish picture, **option 1**
  (statement columns) dominates on cost and gives the portfolio dimension;
  reserve option 2 only if a Spanish-CGT-grade figure is actually needed. UK
  tax planning already has its exact basis (the shipped section-104 lens).
  *Even-cleaner source discovered — the portal `Holdings` xlsx export.* Pictet's
  e-banking Holdings report exports the same per-holding **Net cost (GBP)** and
  **Net unrealised (GBP)** as a structured workbook (one row per position, with
  `Account nr.`, so it keeps the portfolio dimension), and — unlike the
  statement PDF — it already carries the **price-vs-FX split** (`Net unrealised
  on market` / `on currency`), which also satisfies the Accounting item below
  for free. It's a bulk snapshot with broker-side FX and no per-document
  provenance, so it's a reconciliation/management-view input, not a tax feed —
  but as a source it dominates the wrapped-PDF columns of option 1.
  *Open question — resolved (cross-checked the export against the section-104
  lens per holding, spot date 2026-07-03):* Pictet's **Net cost (GBP)**
  reconciles to our section-104 basis **within ~1% in aggregate**, per-holding
  gaps mostly **under 2%**. The divergence is **FX-convention, not lot-
  averaging** — the GBP-native funds match to the penny (no FX), every non-GBP
  fund diverges, and the sign is systematically one-way; the worst single
  holding is a non-GBP bond fund in the high-single-digit-percent range.
  Verdict: **usable as a tolerance cross-check, not as a substitute** for the
  exact UK basis (exactly as predicted). Bonus from the same join: on the spot
  date the export's per-ISIN **quantities matched the section-104 pool on every
  security** (siding with the pool, not the stale month-end statement) — an
  independent confirmation of the pool's live accuracy and the drift-classifier's
  *timing* verdicts.
- **Period reports beyond beancount/Fava.** Net-worth-over-time
  (`net-worth`), income-by-source (`income`, by tax or calendar year),
  asset-allocation-over-time (`allocation`), and per-portfolio allocation
  (`portfolio-allocation`) shipped; still open: realised/unrealised P&L
  summaries (Markdown/CSV).
- **Realised / unrealised P&L by parsing Pictet's tax reports.** Pictet
  issues Spanish-locale IRPF reports ("Informe fiscal personas físicas")
  in two flavours — realised ("Ganancias y pérdidas patrimoniales") and
  unrealised ("…no realizadas"). Both give per-lot cost, proceeds /
  market value, dates, and — crucially — already split the gain into a
  price-driven component ("Total por alteración patrimonial") and a pure
  FX component ("Total por tipo de cambio"). *Parsing* these (a new
  Spanish doctype + multi-page table parser, like `balances_extract` /
  `prices_extract`, feeding a new `pnl` report module/command) is far
  lower-risk than computing P&L ourselves — it sidesteps re-implementing
  cost-basis matching, and the FX/price split also satisfies the
  Accounting item below. Caveats: (a) the figures are **Spanish rules,
  EUR, FIFO** — a management/performance view that will *not* equal UK
  CGT, so it must be positioned separately from the tax pipeline and must
  not feed it; (b) parsing risk — the tables use spaces as both column
  *and* thousands separators (e.g. `12 345,67` = 12,345.67) with EU
  decimal commas and
  multi-line ISIN/header wrapping, so robust number tokenisation is the
  bulk of the work. An example of each was inspected 2026-05-26;
  structure confirmed viable. A second, full-detail pair (as-of
  2026-06-30) was inspected 2026-07-02 — same layout, so nothing new to
  design.
- **Pictet tax reports as a reconciliation target (not a parsed report).**
  Distinct from the parsing item above: rather than surface Pictet's
  numbers as our own `pnl` report, use them to *cross-check* what the
  pipeline already derives — the same idea as `completeness`, aimed at
  cost basis and disposals. Two checks fall out of the two reports:
  (a) the **realised** report enumerates every disposal Pictet booked in
  the period, so assert each appears as a sale in the sidecars (catches a
  missing trade confirmation), and sanity-check the acquisition lot/date
  Pictet matched against ours; (b) the **unrealised** report is a per-lot
  cost-basis + market-value snapshot at a date — the ready-made target the
  deferred **balance-sheet phase 4** assertion-drift / cost-basis overlay
  needs (above). Same caveats as the parsing item — Spanish FIFO/EUR
  figures, informative-only, indicative prices — but for a *tolerance
  cross-check* those matter far less than for a headline number: we compare
  raw lot facts (dates, quantities, proceeds, cost), not Pictet's gain
  method, and never feed any of it to the UK tax pipeline. Wants plan-mode
  first (touches the balance-sheet plan and the reconcile grain).
  *Filing prerequisite (shared first stage, also needed by the parsing
  item above) — ✅ shipped* in
  [docs/archive/pictet-pnl-tax-archive.md](archive/pictet-pnl-tax-archive.md):
  `TAX_REALISED_PL` / `TAX_UNREALISED_PL` doctypes + an `archive.py` filing
  branch now self-file these into `<year>/tax/<Realised|Unrealised> PL
  <YYYYMMDD>.pdf` (keyed on the report's numeric as-of date), and
  `prune-tax-reports` trims the daily volume to month-end + year-end / 5-Apr
  anchors. So a parse or reconcile reader can now glob
  `<year>/tax/{Realised,Unrealised}*.pdf` for a clean, canonically-named,
  deduplicated set.
  *Caveat — no portfolio dimension, and both mandates hold securities:*
  the reports are consolidated at the **taxpayer (NIF) level**, not per
  mandate — every lot is labelled with the bare client number `999999`,
  never `K-999999.001` / `P-999999.002` (verified on the 2026-01-07 and
  2026-06-30 reports). This genuinely matters: **both** mandates hold
  securities and the report commingles them. K-999999.001 holds the core
  funds; P-999999.002 is a *leveraged (Lombard)* mandate whose net
  valuation is negative (the loan dominates) but which holds a real equity
  sleeve — thematic ETFs plus single stocks (e.g. Fujifilm, Kratos) that
  show up as disposals in the realised report. So a cross-check must match
  on **security + lot (date/qty/cost)**, not portfolio, and cannot attribute
  a P&L row to a mandate. Parsing wrinkle for whoever picks this up: the two
  source statements identify holdings differently — the K "Financial
  Statement" prints ISINs, the P one lists holdings **by name only (no
  ISIN)** — so mapping a consolidated P&L row back to the per-portfolio
  ledger means matching K by ISIN and P by name/quantity. Don't assume a
  security lives in K. A name→ISIN resolver already exists for exactly this
  — `build_statement_name_index` over `commodities.toml` `statement_names`
  (added by the P-mandate reconcile fix) — reuse it rather than rebuild.
  *Which snapshots to archive:* the portal re-cuts both reports on
  booking-event days only (not a daily schedule — no weekends, variable
  publication times, irregular missing weekdays all point to
  activity-triggered generation, and the unrealised MTM would regenerate
  every trading day if it were price-driven). Don't hoard the daily
  stream: realised is cumulative within the tax year (the latest report
  supersedes all earlier ones for YTD disposals), and unrealised daily
  deltas are mostly price noise the reconciliation doesn't consume. Keep a
  principled subset — the final realised report per tax year (plus one
  month-end each to catch any Pictet restatement), month-end + calendar-
  year-end + UK-tax-year-end (5 Apr) unrealised snapshots as cost-basis
  anchors, and optionally the first report after each disposal for a
  tight audit trail.

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

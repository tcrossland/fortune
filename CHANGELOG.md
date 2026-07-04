# Changelog

Notable shipped features, newest first. This is a single-user project
with no tagged releases, so entries are chronological rather than
versioned. The forward-looking backlog lives in
[docs/backlog.md](docs/backlog.md); the *why* behind the bigger
decisions is in [docs/design-decisions.md](docs/design-decisions.md).

## UK tax

- **ERI base-cost uplift is cumulative in the tax pipeline (correction)** —
  `tax-report` / `tax-forecast` / `tax-pack` now feed the **cumulative**
  section 104 base-cost adjustments (every ERI year the `eri.toml` spans) to
  the pool, not just the report year's. A disposal whose units accrued Excess
  Reportable Income in an *earlier* year was under-counting its allowable cost,
  so the CGT gain was **overstated** (too much tax; the loss-carry-forward
  chain consumed the same mis-costed rows). ERI *income* stays year-scoped
  (SA106 declares only the current year); only the pool uplift is cumulative.
  `match_disposals` applies adjustments chronologically, so a future-dated
  uplift can't affect the current year's disposals. Cumulative ERI GBP-rate
  gaps now fold into the `--strict` understatement gate. Live for 2025-26 (the
  first affected filing — regenerate it). Plan:
  [docs/archive/eri-cumulative-basis-fix.md](docs/archive/eri-cumulative-basis-fix.md).
  (`cli/tax.py`)
- **ERI / SA106 consistency warnings** — two silent inconsistencies now
  surface in `summary.txt`. ERI for a bond fund (`distributions_as_interest`)
  follows the commodity flag (foreign *interest*), overriding a mistyped
  `income_type` in `eri.toml` so its ERI and distributions can't split
  differently — and flags the override. And foreign income dropped from
  SA106 because its ISIN *prefix* is `GB` while the commodity situs is
  explicitly foreign (a depositary receipt) is flagged for review rather
  than silently lost. Reporting-audit P1 (A6/A7). (`tax/uk/eri.py`,
  `tax/uk/sa106.py`, `cli/tax.py`)
- **`fetch_reporting_funds.py` hardened** — the HMRC reporting-funds
  updater now guards network/format failures (clear abort, never a
  traceback), sanity-checks the parsed ISIN count against an implausibly
  small download before writing, backs up `commodities.toml` and writes
  atomically (temp + replace), and rewrites status **order-independently**
  per `[[commodity]]` block (no longer assuming `isin` precedes
  `reporting_status`). The pure rewrite is unit-tested. The reporting
  audit's P1 (A4/A8 fixed; A5 over-match mitigated by the count guard).
- **`--strict` gates on every understatement mode** — `tax-report`,
  `tax-forecast` and `fig-advice` now exit non-zero under `--strict` not
  only on a missing GBP rate but also on an **unclassified** disposal
  (`reporting_status = "unknown"` → excluded from SA108 / offshore income
  gains / the loss chain entirely) and an **unmatched** disposal (no
  acquisition / opening position → matched at zero cost). Previously these
  two were text-only warnings that passed `--strict`, so a CI gate could
  green-light a silently-understated return. The P0 finding from the
  [reporting audit](docs/reporting-audit.md). (`cli/tax.py`)
- **CGT 4-year loss-claim window warning** — the loss-carry-forward chain
  now tracks the allowable-loss pool by arising year (FIFO) and flags a
  brought-forward loss relieved more than four years after it arose, since
  it is allowable only if it was notified to HMRC within the statutory
  window (which the tool can't verify). Surfaced as `WARN_LOSS_CLAIM_WINDOW`
  in the summary and a note in the tax-pack; figures are left unchanged
  (a loss notified in its arising year carries forward indefinitely).
  Pre-ledger losses have no arising year and aren't flagged.
  (`tax/uk/cgt_allowance.py`)
- **Unclassified-holding FIG-relief warning** — under a FIG claim a
  disposal with no `commodities.toml` entry defaults to UK situs, so it is
  neither taxed nor relieved; a genuinely foreign one silently misses
  relief. `tax-report`'s summary now flags this on the `WARN_UNCLASSIFIED`
  block, and the tax-pack gains an "unclassified holdings — may be missing
  FIG relief" section, prompting a metadata fix before filing.
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

## Reporting

- **Faster statement discovery for the valuation reports** — the ad-hoc
  `holdings` / `net-worth` / `concentration` / `allocation` /
  `portfolio-allocation` CLIs no longer text-extract the whole Pictet archive
  (~2,900 PDFs) to find the ~monthly statements. Three composable changes:
  (a) `--statements-glob` prunes the `--statements-dir` walk by filename
  before opening any PDF (e.g. `'*monthly*.pdf'`, the convention the rebuild
  globs already use); (b) `holdings` additionally opts into `latest_only`,
  keeping only the newest statement per directory (by the `YYYYMMDD` in the
  name) before opening, since it reports only the latest snapshot per
  portfolio — the content-based latest-per-portfolio selection still runs on
  the survivors, so it degrades to slower, never wrong; (c) with **no**
  `--statement` / `--statements-dir`, the reports fall back to the rebuild's
  configured `balance_statements` globs (from `banking-pipeline.toml`), so an
  ad-hoc report reuses the canonical set (Pictet monthly + the whole Vanguard
  ISA dir — no silent-drop) and matches rebuild output. A whole-archive
  `holdings` run drops from ~90s to ~1-2s with byte-identical output.
  (`cli/_main.py`, `cli/reports.py`, `cli_options.py`)
- **`mandate-returns` counts distributing-fund income as return, not a flow**
  — the holdings-based gain is a *price* return, so a distributing fund's cash
  payout used to fall into the inferred-flow residual and be stripped from
  performance, understating TWR/MWR by the distributed yield. `build_report` /
  `aggregate_period_returns` now read the sidecars (`--source`) and add each
  period's distributions (`distribution_income` — `DIVIDEND_TYPES` rows with an
  ISIN, incl. bond-fund payouts) back into the gain and out of the flow. Income
  is folded in only for a portfolio with **tracked positions**, so the P
  mandate (whose by-name holdings aren't valued here, leaving a tiny
  residual-cash base) isn't divided into a nonsense return. Live effect: a
  sub-percentage-point uplift to the whole-mandate TWR. Rationale:
  [design-decisions.md](docs/design-decisions.md#mandate-returns-counts-distribution-income-as-return).
  (`mandate_returns.py`, `cli/reports.py`, `cli/rebuild.py`)
- **Net-worth / allocation retire a wound-down portfolio on a nil statement**
  — the timeline reports' as-of forward-fill carries each portfolio's last
  snapshot forward, which overstated net worth once the Vanguard ISA wound
  down (an empty statement created no snapshot, so the last non-empty value
  lingered indefinitely). A recognised nil statement — a Vanguard ISA whose
  *current-column* `Account total` is £0.00 — now emits a zero-value snapshot
  (`drained_portfolio_snapshot` / `parse_isa_nil_statement`) that supersedes
  it, retiring the account at its drain date. Keyed on the statement's explicit
  nil total, **not** the absence of parsed holdings, so a parse failure on a
  still-funded account can't be mistaken for a wind-down and phantom-collapse
  the total. Scoped to the two timeline reports (the latest-snapshot reports
  don't linger); the residual caveat (a portfolio that stops statementing with
  no closing nil statement) is narrowed in both outputs. Audit item B6, now
  behaviourally fixed. Rationale:
  [design-decisions.md](docs/design-decisions.md#a-recognised-nil-statement-retires-its-portfolio-from-the-timeline).
  (`valuation.py`, `vanguard_statement.py`, `net_worth.py`, `allocation.py`)
- **`holdings` drift cross-check classifies timing vs gap** — the
  statement-vs-section-104-pool quantity check no longer blanket-flags every
  disagreement as an ingest gap. A month-end mark is struck on settled
  positions, so an ingested trade settling *after* the statement date isn't on
  it while the trade-dated pool has already moved — a **timing** lead that
  clears with the next statement, distinct from a **gap** (a missing trade
  confirmation or stale statement). Classification nets the signed
  post-statement trade movement per ISIN (cutoff = `settlement_date`, fallback
  `trade_date`); *timing* iff `pool − statement == movement`. The
  held-not-on-statement list is classified the same way, and a timing row's
  unrealised P&L is flagged provisional (market at the pre-trade statement
  quantity, cost at the post-trade pool). Rationale:
  [design-decisions.md](docs/design-decisions.md#the-holdings-drift-cross-check-classifies-timing-vs-gap-by-settlement-date).
  (`holdings.py`, `cli/reports.py`)
- **`holdings` — cost basis + unrealised P&L** — joins the latest statement
  market value per portfolio with a pluggable per-jurisdiction cost basis and
  reports per-holding unrealised gain/loss, cross-checking the statement
  quantity against the section 104 pool. The UK lens (`--basis uk`) reads cost
  basis from the sidecars (ERI-adjusted across all years), never the ledger;
  the EUR/Spanish lens (`--basis es`) is reserved. ISA holdings show from the
  statement side but carry no section 104 basis (tax-exempt). Securities are
  consolidated by ISIN before the join (the pool is NIF-level). A `holdings`
  rebuild toggle (`[post.reports]`, default off). (`holdings.py`,
  `basis_lens.py`, `tax/uk/basis.py`, `cli/reports.py`)
- **`net-worth --monthly`** — resamples the timeline onto a first-of-month
  grid instead of one row per raw statement date. Portfolios statement on
  mixed cadences (Pictet month-end → dated the 1st, the Vanguard ISA and
  property valuations mid-month), so the default event-driven grid shows
  spurious mid-month rows where only one portfolio refreshed; monthly mode
  keeps the fresh-Pictet first-of-month points, folds each mid-month update
  into the next, and forward-fills gap months. Also a `net_worth_monthly`
  rebuild toggle (`[post.reports]`, default off). (`net_worth.py`,
  `cli/reports.py`, `batch_config.py`)
- **Valuation reports forward-fill the GBP rate** — a month-end statement
  is dated to the following day (a 30 June snapshot carries `on_date`
  1 July), so it asked for a month HMRC hadn't published yet, dropping every
  non-GBP holding as a `RateGap` and collapsing the newest net-worth row.
  `value_holdings` now wraps its source in `ForwardFillRateSource` (walks
  back month-by-month, bounded to 12, to the latest published rate), so
  concentration / net-worth / allocation / portfolio-allocation /
  mandate-returns mark to the latest known rate — matching the balance
  sheet. The tax pipeline keeps the exact-month source (CGT needs it);
  wrapping a rateless `NullSource` stays `None`, preserving the `--strict`
  understated-snapshot gate. (`fx/gbp_rates.py`, `valuation.py`)
- **Interactive balance sheet (`balance-sheet`)** — a single
  self-contained, **offline** `balance-sheet.html` (+ a JSON sidecar) you
  open in any browser and scrub to *any* as-of date. A ledger construct
  (like `trial-balance`): one `bean-query` returns the Asset/Liability
  postings, and the browser sums each holding up to the chosen date and
  values it to GBP entirely client-side — a collapsible account tree, a
  hand-rolled SVG allocation donut, and the Assets / Liabilities (the
  Lombard loan = negative cash) / net-worth totals. FX comes from the
  `GbpRateSource` (the security marks carry no currency→GBP rate); an
  unpriced holding is flagged, never zeroed. Standalone command (`--open`
  to view) or via the `[post.reports] balance_sheet` rebuild toggle; the
  artifact carries real balances so `reports/balance-sheet/` is git-ignored.
  Phases 1–3 (dataset → artifact → wiring); cost-basis and assertion-drift
  overlay deferred as non-goals. (`balance_sheet.py`,
  `balance_sheet_template.html`, `cli/reports.py`, `cli/rebuild.py`;
  README § Reports + design-decisions.md)
- **`net-worth` table is newest-first** — the net-worth-over-time Markdown
  table now lists rows in descending date order (most recent at the top);
  each row's Δ keeps its chronological meaning (change since the previous,
  older date). The CSV stays ascending for spreadsheet use. (`net_worth.py`)
- **Spanish monthly statement (`ESTADO_MENSUAL`) valuation extraction** —
  the Madrid account's Spanish-locale statement now feeds the valuation
  reports (concentration / net-worth / allocation) like the English
  monthly statement, not just classification/archival. `balances_extract`
  handles the bare `K-NNNNNN.NNN` account line and the currency-led cash
  row; `prices_extract` reads the *cotización* off the holding row above
  the `ISIN:` line (the ES layout packs the holding into two lines, and
  the ISIN line carries the gross unit cost, not the mark). Cash on those statements is rounded to whole units, so a
  whole-number fiat balance assertion now carries a `~ 0.5` rounding
  tolerance (securities and cent-precise cash assert exactly).
  Validated
  against a real statement (33 holdings + cash, `qty × cotización`
  reconciling with `VALORACIÓN`). (`balances_extract.py`,
  `prices_extract.py`)
- **Vanguard ISA ticker metadata** — `commodities.toml` can now carry
  entries keyed on a Vanguard fund **ticker** (e.g. `VGVA` / `VMIG`), which
  the ISA holdings use as their commodity since the contract notes print no
  ISIN. The commodity-code validator gained a small allow-list for those
  tickers (a mistyped ISIN is still rejected). This classifies the ISA
  holdings in the concentration / allocation reports instead of bucketing
  them `unknown`. (`commodities_metadata.py`)
- **Reporting-audit test coverage (P2 close-out)** — adversarial
  `infer_issuer` precedence/collision test; an assertion that an
  `unknown`-status disposal is excluded from SA108 *and* the
  loss-carry-forward chain (not just flagged); CLI `--strict` exit-path
  tests for concentration / allocation / portfolio-allocation / income;
  and `report_format` helper tests. Completes the reporting audit — all
  P0/P1/P2 items resolved.
- **trial-balance polish** — trial-balance can now be regenerated by
  `rebuild` (an opt-in `[post.reports] trial_balance` toggle; ledger-based
  via bean-query, so a missing ledger/binary warns and skips rather than
  failing the run). A multi-leg account with one unvaluable leg now keeps
  its valued legs in the GBP total instead of dropping the whole account,
  and the rate-gap warning names the account (`USD … (Assets:…:USD)`) rather
  than the useless `(USD)`. Reporting-audit P2 (B3/B7/B11). (`cli/rebuild.py`,
  `batch_config.py`, `trial_balance.py`)
- **Uniform report warnings + shared formatting** — the "unclassified
  holdings (no metadata)" and "unvaluable holdings (no mark)" warnings now
  render consistently across concentration / net-worth / allocation /
  portfolio-allocation (previously net-worth and allocation silently
  dropped one or both), and the net-worth/allocation timelines surface the
  stale-forward-fill caveat in the report body. The shared
  `report_format.unclassified_lines` / `missing_price_lines` / `weight`
  helpers replace four per-report copies. Reporting-audit P2 (B4/B5/B6/B10).
  (`report_format.py` + the four report modules)
- **`net-worth --strict` + valuation-source note** — `net-worth` gains the
  `--strict` flag the other valued reports already had (exit non-zero when
  a snapshot can't be fully valued, so a CI gate catches an understated
  timeline), and the `trial-balance` module now documents why it does *not*
  reconcile with the statement-valuation reports (ledger current positions
  / today vs latest statement snapshot / statement date). Reporting-audit
  P1 (B1/B2). (`cli/reports.py`, `trial_balance.py`)
- **Concentration "by issuer"** — the `concentration` report gains a
  fund-house / single-provider exposure breakdown alongside the existing
  ones. Issuer comes from an optional `issuer` field on the commodity
  metadata, or is inferred from the fund name (`infer_issuer` — iShares /
  Amundi / Pictet / …), so it works without tagging every ISIN. Additive:
  "by domicile" stays, since UK-tax situs and reporting status key off
  domicile, not issuer. (`commodities_metadata.py`, `valuation.py`,
  `concentration.py`)
- **`trial-balance`** — per-account trial balance from the beancount ledger
  via `bean-query` (the one report that needs the loader, since the
  cost-basis `Realized`/`Unrealized` legs are computed at load time). Lists
  every account's closing balance — securities in units, cash native — and
  adds a **GBP market-value column on Assets / Liabilities** (each account's
  `value()` converted at the configured `GbpRateSource`, the same rate
  machinery `concentration` uses); Equity / Income / Expenses stay native
  (cumulative flows whose spot conversion would need an `Equity:Conversions`
  plug). Unvaluable balances (no mark / no GBP rate) are blank in the GBP
  column and flagged; `--strict` exits non-zero on any. Writes
  `trial-balance.md` + `trial-balance.csv`. (`trial_balance.py`,
  `bean_query.py`)
- **`portfolio-split`** — writes one independently-loadable beancount
  ledger per bank account (each Pictet account, the Vanguard ISA) under
  `<data_dir>/accounts/`, for opening a single account in isolation in
  Fava. Each per-year ingest file is one bank+account stream, so the
  command groups the sources by owning account and runs the same
  open/close scan per group, emitting its own `option`s (operating
  currency, booking method, and the root ledger's
  `inferred_tolerance_default` directives so it balances standalone) and
  `include`s of that account's per-year files plus `prices.beancount` —
  not `balances.beancount`, whose assertions span every account.
  (`portfolio_aggregate.generate_per_account`)
- **`rebuild` missing-source guard** — `rebuild` now aborts before the
  clean step when a `[[sources]]` glob matches zero files *and* the clean
  step would delete that source's existing output (the moved / unsynced
  source case — a wiped ledger the ingest step can't regenerate). A
  genuinely-new empty year (no output yet) still just warns;
  `--allow-missing-sources` overrides. (`cli/rebuild.py`)
- **`[import]` rebuild step** — `rebuild` can optionally file fresh
  downloads into the dated archive tree (the `import` command) before the
  `[[sources]]` globs read from it, so one run goes from a download zip to
  a checked ledger. Off by default to keep a plain rebuild idempotent.
  (`batch_config.py`, `cli/rebuild.py`)
- **`[post.reports]` rebuild step** — `rebuild` can now regenerate the
  analytical reports (income / concentration / net-worth / allocation /
  portfolio-allocation) into the configured `*_reports_dir`s as part of
  the run, before reconcile/check so they land even when bean-check exits
  nonzero. Off by default; per-report toggles, and a `statements` glob
  that falls back to the balances step's. (`batch_config.py`, `cli.py`)
- **`portfolio-allocation`** — per-portfolio allocation report. Breaks the
  latest valuation down per portfolio (each Pictet account, the Vanguard
  ISA, each property): a cross-portfolio net-worth/share summary plus a
  per-portfolio asset-class + holdings breakdown. Reuses `concentration`'s
  per-portfolio valuation (cash netted within a portfolio, not across the
  book). (`portfolio_allocation.py`)
- **`allocation`** — asset-allocation-over-time report. Tracks the
  asset-class mix (equity / bond / property / … plus net cash) across the
  statement timeline, so allocation drift is visible. Composes
  `concentration`'s per-snapshot valuation (securities aggregated by
  `commodities.toml` asset class) with `net-worth`'s as-of forward-fill;
  weights are a share of gross long holdings, with cash / leverage shown
  separately. Writes a % matrix over time + a latest-date breakdown.
  (`allocation.py`)
- **`income`** — income-by-source report. Aggregates dividend + interest
  income *received* from the JSONL sidecars by period (UK tax year or
  calendar year) and paying source, valued in GBP. Reuses SA106's
  bond-fund distribution→interest reclassification; unlike the tax
  pipeline it includes ISA income (flagged tax-free, not dropped) and
  counts UK + foreign alike. Credit-balance interest only — overdraft
  interest the user pays is an expense and excluded. (`income.py`)
- **Residential property on the ledger (`property`)** — off-ledger
  residential property (a user-maintained `data/property.toml`) is brought
  onto the beancount ledger: each property is a commodity held at cost
  (1 unit), revalued by `price` directives, funded against
  `Equity:Property:<label>` (the financing already sits on the investment
  ledger, so net worth rises by the property value without double-counting).
  A non-GBP property also gets a GBP price mark (via the rate source) so a
  GBP load values it. Folded into the `concentration` / `net-worth` reports
  (asset class `property`, domicile = country) so they show total wealth.
  (`property.py`)
- **`net-worth`** — net-worth-over-time report. Values each statement's
  valuation at its own date (reusing the concentration valuation) and
  builds a combined timeline across portfolios — each contributes its
  latest valuation on or before each date (as-of forward-fill), and
  same-date duplicate statements are deduped so a holding isn't
  double-counted. Writes `net-worth.md` (gross long / net cash / net worth
  per date + period delta) + `net-worth.csv`. (`net_worth.py`)
- **`concentration`** — portfolio concentration / exposure report. Reads
  the latest statement valuation per portfolio (Pictet + Vanguard ISA),
  values every holding in GBP, and breaks the total down by holding,
  asset class, quotation currency, and domicile. Leverage-aware: weights
  are a share of gross long holdings and a negative cash balance (a
  margin / Lombard loan) is netted by currency and reported separately,
  so weights don't blow past 100%. Writes `concentration.md` +
  `holdings.csv`. (`concentration.py`)

## Archiving

- **`prune-tax-reports` converges re-filed strays** — when a rebuild/import
  re-files an already-pruned daily P/L report into `<year>/tax/`, prune now
  deletes it if a **byte-identical** (md5) copy already sits in
  `_superseded/` (the superseded copy is the record), instead of warning and
  leaving it. Only byte-identical, **non-retained** reports are removed — an
  unrealised report re-valued under the same effective date shares a name but
  differs in bytes and is left untouched. Dry-run by default.
  (`cli/prune.py`, `tax_report_prune.py`)
- **Tax reports dated by their effective date, not the content label** —
  a Spanish tax report now files under the effective date in its source
  filename (Pictet's Publication/Effective date, `-<YYYYMMDD>`), with the
  content fiscal date as a fallback and a warning on disagreement. Fixes a
  silent data loss: Pictet froze the `Al 10.09.2023` content label on a run
  of re-valued unrealised reports, which the old content-date scraper
  collapsed onto one canonical name. (`archive.py`)
- **Annual tax-authority filings classified + auto-filed** — Declaración ETE,
  Modelo 720 and the UK income & capital-gains report file into `<year>/tax/`
  under canonical names (`ETE <date>` / `Modelo 720 <date>` / `Income and
  capital gains UK <date>`). Archive-only. (`classifiers/rules.py`,
  `archive.py`)
- **Annual fiscal statement is its own doctype** — the comprehensive "Informe
  fiscal personas físicas" files as `Fiscal statement <date>.pdf`, distinct
  from the daily Realised P/L it was misclassified as (discriminated by its
  `VALORACIÓN DE CARTERA` + `Gastos de administración` sections).
- **Pictet IRPF P/L reports filed + pruned** — Realised/Unrealised P/L reports
  auto-file to `<year>/tax/<Type> PL <date>.pdf`, and a new `prune-tax-reports`
  command keeps month-end + year-end / 5-Apr anchors (moving daily noise to
  `_superseded/`; dry-run by default). `[import] source_globs` composes extra
  source globs. Archive-only — never ingested or fed to the UK-tax pipeline.
  (`cli/prune.py`, `tax_report_prune.py`, `archive.py`)

## Bookkeeping & accounting

- **`completeness` reads the portal cash-statement CSV; the CSV is archived**
  — the statement-completeness cross-check now accepts the e-banking `Cash
  statements by value date` CSV export as well as the `Financial-statement`
  PDF (format detected by suffix). This matters because only 4 of those PDFs
  were ever pulled (K mandate, to 2023-06-30); the CSV is the current source —
  both mandates, all currency sub-accounts, to date. `parse_cash_statement_csv`
  reads it (Windows-1252, `;`-delimited, signed amounts) and still self-checks
  the running balance per sub-account; the worker groups the multi-mandate file
  into one report per portfolio (period synthesised from its value dates) and
  resolves the CSV's letterless `Account nr.` to the lettered sidecar portfolio
  (`lettered_portfolio_map`). Validated on the real export: **0 missing / 0
  unmatched** for both mandates, reconciling exactly to the PDF parser and the
  sidecars. The CSV is filed keep-latest into `<archive>/cash-statements/` by
  the import step (`[import] cash_statement_globs`, bypassing the PDF
  classifier — a CSV isn't a PDF), and `[post.completeness]` reads it from
  there each rebuild. Along the way, `limit_extension` (a `Net amount = 0.00`
  credit-facility advice) joins the cash-neutral exclusion set — latent until
  the CSV widened coverage past 2023. No new dependency (stdlib `csv`).
  (`statement_completeness.py`, `cli/_main.py`, `cli/reports.py`, `archive.py`,
  `batch_config.py`, `cli/rebuild.py`)
- **Order number captured on three previously-unkeyed doctypes** — the
  `pago_interna`, `final_redemption`, and `limit_extension` templates now
  populate `Transaction.transaction_number` from the document header
  (`N° de transacción:` / `Transaction no.:`) via the shared
  `find_transaction_number` helper their siblings already used, so **every**
  Pictet sidecar row is now keyed to Pictet's per-document order number
  (previously 8 rows carried `transaction_number: null`). That number is the
  join key for reconciling the sidecars against Pictet's portal Transactions
  export and strengthens exact-match dedup for re-ingests of these doctypes.
  Rendered ledger is unchanged except a `no:` trailer on the `pago_interna`
  entry; sidecars serialize the field regardless of the builder. Enables the
  `reconcile-export` backlog item. (`templates/pictet/{pago_interna,
  final_redemption,limit_extension}.py`)
- **Revolut self-to-self contra-leg → `Equity:Transfers`** — the
  Pictet↔Revolut transfer counter-leg now books to
  `Equity:Transfers:Revolut:<ccy>` instead of `Assets:Revolut:<ccy>`.
  Revolut's day-to-day activity is never imported, so the
  `Assets:Revolut:*` balance was only ever "net moved between Pictet and
  Revolut" — a phantom the balance sheet (which sums Assets/Liabilities
  postings) counted as real net worth. Booking it to Equity (a perimeter
  crossing, not a holding) excludes it by construction, no report-side
  filter. Only the rendered account string moves —
  `Transaction.counter_account` is unchanged, so the JSONL sidecars, tax
  pipeline, and reconcile/completeness are untouched. (`writer/format.py`,
  `writer/builders/payment.py`; see `docs/design-decisions.md`)
- **`completeness` — statement-vs-sidecar transaction cross-check** — the
  transaction-level counterpart to `reconcile`'s balance-level check. Parses
  the Pictet current-account cash ledger out of a `Financial-statement` PDF
  (the authoritative list of every cash movement for its period) and diffs it
  against the ingested `*.transactions.jsonl` sidecars, flagging statement
  lines with no ingested advice (MISSING — a likely un-ingested document) and
  ingested cash events with no statement line (UNMATCHED — a possible misdated
  booking). Each movement's sign is recovered from the printed running-balance
  delta, which doubles as a self-check (a row that won't reconcile to ±its
  magnitude raises rather than emitting a wrong line); the balance is tracked
  per currency so pypdfium2's repeated page headers and number-less page-break
  carried-forward lines can't break the chain. Securities settlements
  (`switch_*`, in-specie receipts — they post off the current account) and
  out-of-window events are excluded, not flagged; the FX/transfer counter-leg
  (one sidecar row, two statement lines) is expanded so both legs match.
  Writes one `summary-<portfolio>-<period-end>.txt` + `findings-<…>.csv` per
  statement under `reports/completeness/`. Available as the `completeness`
  command and an optional `[post.completeness]` rebuild step (MISSING fails
  the rebuild; UNMATCHED under `strict`). Validated against 2021–2023: zero
  unexplained findings. (`statement_completeness.py`, `cli/_main.py`,
  `cli/reports.py`, `cli/rebuild.py`, `batch_config.py`, `config.py`; see
  `docs/design-decisions.md`)
- **`--strict --check` no longer aborts with a bean-check usage error** —
  the strict path appended `-w` to `bean-check` to "treat warnings as
  errors", but beancount v3's bean-check removed that flag (and has no
  warning/error severity split), so the invocation died with
  `Error: No such option: -w` (`rc=2`) on every strict check since the v3
  pin. Dropped `-w`; bean-check fails on any error regardless of `--strict`
  (a clean ledger passes either way). The `strict` parameter is retained
  for call-site symmetry but documented as inert at the bean-check level.
  Guarded by `tests/test_bean_check.py`. (`bean_check.py`, `cli/rebuild.py`)
- **Nil-activity Vanguard statements no longer break `--strict`** — a
  `vanguard_regular_statement` for a drained / £0 account carries no
  `Activity` section and legitimately extracts nothing, but strict mode
  read that empty result as a template regression and aborted. Templates
  can now implement an optional `is_expected_empty(doc)` hook (duck-typed;
  consulted only when `extract` returned `[]`); `regular_statement`
  returns `True` when there's no `Activity` section, so a genuinely-nil
  statement is treated as expected — while a statement that *does* carry
  activity but extracts nothing still raises (a real regression). Guarded
  by `tests/test_vanguard_nil_statement.py`. (`fields/hybrid.py`,
  `templates/__init__.py`, `templates/vanguard_uk/regular_statement.py`)
- **`FACTURA` / `INTEREST_SCALE` / `ORDER_INFORMATION_REPORT` registered as
  no-output** — these three Pictet templates return `[]` by design (the
  cash leg lives on a sibling `DEBITO_DE_GASTOS` / `INTEREST_PAYMENT`
  advice, or the document is a pre-trade cost *simulation*), but their
  doctypes were missing from `NO_OUTPUT_DOCTYPES`. Under `--strict` the
  empty result was therefore treated as a template regression and
  `rebuild --strict` aborted on the first such document. Added all three
  (a "companion / disclosure" family) to the set; regression-guarded by
  `tests/test_no_output_strict.py`. (`models.py`)
- **Switch salida/entrada leg pairing** — a Pictet fund switch's two
  advices (`SWITCH_SALIDA` + `SWITCH_ENTRADA`) now render with one shared
  beancount `^<link>` (the salida's number), so the pair resolves as a
  single logical operation in `bean-query` / Fava; each leg keeps its own
  number as the `no:` reference. `ingest` collects the whole batch, runs
  the pure `switch_pairing.pair_switches` matcher, then renders. Legs pair
  on a shared **order date** (`Fecha de la orden`, captured into
  `Transaction.order_date`) rather than clearing-account netting — FX
  switches don't net to the cent, but always share an order date. Amount-
  netting is a conservative fallback that refuses to guess on a tie.
  Unpaired legs warn; `--strict` fails on a non-netting in-batch pair.
  Sidecar schema → `…/v4` (additive). (`switch_pairing.py`, `cli/ingest.py`,
  `models.py`, `templates/pictet/_common.py`; see
  `docs/design-decisions.md`)
- **`import` files periodic statements** — the `import` command now files
  Pictet monthly / quarterly / annual valuation statements (both locales),
  not just transaction advices. Statements carry no transaction reference,
  so they file by their as-of (period-end) date into the account's
  `reports/` subfolder — `<year>/<account>/reports/Valuation <period>
  <YYYYMMDD>.pdf`, the convention the ingest / valuation stages already
  glob. Previously they were reported as unmatched and left for manual
  filing. (`archive.py`)
- **Aggregate-aware `close` directives** — close emission moved off the
  per-batch `ingest` path (which couldn't see a later source re-acquiring
  a wound-down holding, breaking bean-check on the re-buy) and onto the
  portfolio aggregate, which sums each ISIN asset account across the full
  history and closes only those that net to zero. `ingest` / `rebuild`
  now agree, and the aggregate owns both the central opens and closes.
  (`portfolio_aggregate.py`)
- **Vanguard UK Stocks & Shares ISA** — a second bank wired through the
  writer-profile / ruleset / template seams (two-segment account prefix,
  ticker-keyed commodities, NoOp templates).
- **`reconcile`** — statement-balance reconciliation: diffs computed
  ledger balances against statement assertions across the whole grid,
  localising the earliest drift and flagging coverage gaps.
  (`reconcile.py`)
- **`dedup-check`** — read-only audit flagging double-counted
  transactions via a stable content `dedup_key`. (`dedup.py`)

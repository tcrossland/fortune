# Glossary

Definitions of the domain terms that recur across this codebase and its
docs. Tight definitions with pointers to the authoritative detail — the
*why* is in [design-decisions.md](design-decisions.md), the *how* in
[architecture.md](architecture.md), the binding constraints in
[../CLAUDE.md](../CLAUDE.md). Terms in the issuer's own vocabulary (the
`DocumentType` values) are documented in full in `models.py`; this file
gives the shape, not a copy.

## Dates

A single transaction carries several timestamps, marking different points
in its lifecycle. Each subsystem keys on the one its rules require — the
sidecars carry all of them so none is forced onto the wrong basis.

- **Order date** — when the order was *placed* (one step before execution).
- **Trade date** — when the deal was *struck* (order executed, price and
  quantity fixed). UK CGT dates a disposal here (contract date), so section
  104 matching keys on trade date — a trade on 5 vs 6 April lands in
  different tax years regardless of settlement.
- **Value date** (= **settlement date**) — when cash and securities actually
  *change hands* (T+n after the trade; T+1–T+3 for the funds held). The
  `completeness` cash-ledger diff and the month-end statement balances are on
  this basis, because cash and settled positions move on the value date.
- **Booking date** — when the bank *recorded the entry in its own ledger*.
  An operational timestamp (at or just after trade date), driven by Pictet's
  back-office, not by the market or the settlement cycle.
- **Effective date** — the Pictet *Publication date* encoded as `-<YYYYMMDD>`
  in a report's source filename; the authoritative date a tax report is
  filed under (the content's fiscal label can be stale). See
  [pictet-effective-date-filing](archive/pictet-effective-date-filing.md).
- **As-of date** — the date a *valuation* / snapshot is struck (a statement's
  mark, a balance-sheet scrub date). Distinct from the transaction dates
  above.

The booking-vs-value split is why the two cash-statement exports diverge at
a period edge: a trade booked before month-end but *settling* after is on
the booking-date ledger and not yet on the value-date one — a settlement
lag, not a data gap.

## Entities and accounts

- **Mandate** — a Pictet discretionary portfolio. Two are held: **K**
  (account `K-999999.001`), the core fund portfolio; and **P** (account
  `P-999999.002`), a leveraged **Lombard** mandate whose net valuation is
  dominated by the loan but which holds a real equity sleeve. (Account
  bodies shown here are the scrubbed placeholders — see
  [Anonymisation](../CLAUDE.md#anonymisation-and-the-pii-guard).)
- **Lombard loan** — a credit facility secured against the portfolio (the P
  mandate). Drawn down to fund off-ledger property, so it reads as a
  negative cash balance / liability, not an investment loss.
- **Sub-account** — a per-currency slice of a mandate's current account
  (e.g. `…001.00.EUR`, `…001.00.USD`). The cash statement's running
  `Balance` is tracked per sub-account.
- **NIF** — *Número de Identificación Fiscal*, the Spanish taxpayer ID.
  Pictet's tax-authority reports are **NIF-level** (consolidated across both
  mandates, no portfolio dimension) — which is why a cost-basis cross-check
  must join on security/lot, not mandate.
- **ISA** — the Vanguard UK Stocks & Shares ISA. UK-tax-exempt, so its
  trades have no section 104 basis and are filtered at the tax choke point.
- **Property** — off-ledger residential property (Bristol UK; Madrid ES),
  modelled from `data/property.toml` as a commodity held at cost.

## Pipeline

- **Sidecar** — a `*.transactions.jsonl` file emitted alongside the
  beancount output. The **load-bearing substrate** the UK-tax pipeline reads;
  all section-104 / GBP math happens here, never re-parsed from ledger text.
- **`Transaction`** — the canonical row (one per document). `currency` /
  `amount` is the cash-leg currency; `security_currency` is the
  trade-execution currency (they differ on FX trades). `is_fx` is the single
  branch point. Full field set in
  [architecture.md](architecture.md#the-domain-model).
- **Classify (lang → bank → doctype)** — the three-stage classification each
  document passes through, via the `LayeredClassifier` facade.
- **Hybrid extraction** — each classify/extract stage runs deterministic
  rules first; the Claude LLM fallback fires only below
  `rule_confidence_threshold` (default `0.75`) and is skipped when no API
  key is set. Not exercised in tests.
- **Strict-mode dispatch** — the rule for what `HybridExtractor` does when a
  registered template returns `[]` (expected-empty vs skip-fallback vs raise
  under `--strict`). The footgun section in
  [../CLAUDE.md](../CLAUDE.md#strict-mode-dispatch-the-footgun) is the
  reference.
- **`NO_OUTPUT_DOCTYPES`** — the single source of truth for doctypes that
  legitimately emit zero transactions (periodic statements, paired-advice
  openings). Consulted by both the writer and the extractor.
- **`is_expected_empty(doc)`** — a per-document escape hatch: a doctype that
  *normally* emits but yields nothing on *this* structurally-empty input
  (e.g. a nil-activity statement).
- **`transaction_number`** — Pictet's per-document order number (the
  `N° de transacción:` / `Transaction no.:` header, = the `Order nr.` column
  in the portal exports, = the numeric suffix of the source filename). The
  stable ID; the join key for reconciling the sidecars against the exports.
- **`dedup_key`** — a content hash used to detect the same event ingested
  twice; deliberately excludes `transaction_number`.
- **`link_id`** — links the two legs of a paired advice (a switch's
  `SALIDA` + `ENTRADA`) that render separately but are one economic event.
- **Switch pairing** — the logic that matches a fund switch's out-leg
  (`SWITCH_SALIDA`) to its in-leg (`SWITCH_ENTRADA`). A switch is
  cash-neutral (fund → fund), so it never appears on the cash ledger.

## Documents and doctypes

- **`DocumentType`** — the per-document classification, kept in the
  **issuer's own vocabulary** (`COMPRA`, `SWITCH_SALIDA`, `REEMBOLSO`,
  `PAGO_INTERNA`, `SUBSCR`, `REDEM`, …), never anglicised. Each value's
  docstring in `models.py` documents its distinguishing PDF markers — read
  it before authoring rules or templates.
- **Monthly statement** — the Pictet statement carrying the Portfolio
  valuation page (per-holding + per-cash-sub-account balances). The
  valuation-bearing doctype the reports discover; named
  `Valuation-monthly-<YYYYMMDD>.pdf` in the archive.
- **Financial statement** — the P-mandate's by-name valuation layout
  (holdings named, no ISIN — resolved name → ISIN via `commodities.toml`
  `statement_names`).
- **Tax-authority filings** — Pictet's annual filings, classified as their
  own `NO_OUTPUT` doctypes and auto-filed under `<year>/tax/`:
  `DECLARACION_ETE`, `MODELO_720`, `INCOME_CAPITAL_GAINS_UK`, and the
  comprehensive `TAX_FISCAL_STATEMENT`. Archived, never ingested.
- **Realised / Unrealised P&L reports** (`TAX_REALISED_PL` /
  `TAX_UNREALISED_PL`) — Pictet's daily-issued IRPF P&L reports; filed and
  pruned to month-/year-end anchors, never fed to the tax pipeline.

## UK tax

- **Section 104 pool** — the UK CGT share-pooling model: all units of a
  security form one pooled holding at a weighted-average GBP cost. Disposals
  match against the residual pool. This project's pool is GBP-pooled and
  NIF-level (consolidated across mandates by ISIN).
- **ERI** (**Excess Reportable Income**) — income a reporting fund earns but
  doesn't distribute; taxable to the holder and it **uplifts the section 104
  base cost**. Applied cumulatively across the whole history (a current cost
  basis), income declared year-scoped. From `eri.toml`.
- **Reporting fund** — an offshore fund with HMRC reporting status; its
  gains are CGT (not income) and its ERI applies. Status tracked per ISIN in
  `commodities.toml` (`reporting_status`).
- **`distributions_as_interest`** — a per-ISIN flag: a bond fund whose
  distributions are taxed as foreign *interest*, not dividends.
- **`deeply_discounted`** — a per-ISIN flag for deeply-discounted securities
  (income, not CGT, on the gain).
- **FIG** (**Foreign Income and Gains**) — the post-2025 regime replacing
  the remittance basis. Under a **FIG claim** (`fig_claim_years`), foreign
  (non-UK-**situs**) gains are relieved to nil, the CGT **AEA** is forfeited,
  and foreign losses are disallowed.
- **Situs** — where an asset is treated as located for tax; determines
  whether a gain is foreign (FIG-relievable) or UK-taxable
  (`CommodityMetadata.resolved_uk_situs`).
- **AEA** (**Annual Exempt Amount**) — the CGT tax-free allowance per year.
- **Loss carry-forward** — unused capital losses carried to offset future
  gains; the chain the tax pipeline maintains.
- **SA108 / SA106** — the HMRC self-assessment pages the tax pipeline emits
  CSVs for: SA108 (capital gains), SA106 (foreign income).
- **Tax choke point** — the single place `tax-report` filters
  `[tx … if not tx.is_tax_exempt]`, right after loading the sidecars, before
  any compute. ISA (tax-exempt) trades drop out here.

## FX and rates

- **HMRC monthly rate** — the exchange rate source used for **all tax** GBP
  conversion (HMRC's published monthly average). The deliberate
  source-of-truth choice — never substitute a broker's FX rate for tax.
- **`ForwardFillRateSource`** — wraps the rate source for *valuation*
  reports: uses the latest published rate ≤ the statement month (bounded
  12-month walk-back), so a month-end statement dated into an
  unpublished-rate month still marks. Tax keeps the exact-month source.
- **`RateGap`** — a flagged holding/period with no GBP rate available; folds
  into the `--strict` understatement gate rather than silently dropping.

## Reconciliation and reports

- **`completeness`** — the transaction-level cross-check: diffs the Pictet
  current-account cash ledger against the sidecars, flagging **MISSING**
  (statement line with no ingested advice) and **UNMATCHED** (ingested cash
  event with no statement line). Keys on `settlement_date` (= value date).
- **`reconcile`** — the balance-level check: statement balance assertions vs
  the ledger.
- **Holdings drift (timing vs gap)** — the `holdings` cross-check of
  statement quantity vs section 104 `pool_qty`. A **timing** disagreement is
  fully explained by ingested trades settling after the statement date (the
  trade-dated pool leads a settlement-dated mark; clears next statement); a
  **gap** is unexplained (a missing trade confirmation or stale statement).
- **`mandate-returns`** — per-mandate TWR / MWR (time- / money-weighted
  return), holdings-based, with distributing-fund income folded back in as
  return rather than an inferred flow.
- **Portal exports** — the machine-readable e-banking downloads
  (`Transactions`, `Holdings`, `Cash statements`) used as reconciliation
  sources, all joinable to the sidecars by `Order nr.` ↔ `transaction_number`.
  See the reconciliation items in [backlog.md](backlog.md).

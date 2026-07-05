# Architecture

Contributor-facing internals for `banking-pipeline`: how the pipeline is
wired, what every module does, the full CLI reference, the configuration
surface, and the recipe for adding a bank. For *how to use* the tool see
[../README.md](../README.md); for the durable *why* behind the load-bearing
choices see [design-decisions.md](design-decisions.md); for the hard
constraints that bind every change see [../CLAUDE.md](../CLAUDE.md).

## What it is

`banking-pipeline` ingests banking correspondence (PDFs, plus one
Revolut-CSV side path), classifies each document by language → bank →
doctype, extracts the fields that matter for accounting, and emits
[beancount](https://beancount.github.io/) entries. It is single-user, built
around Pictet's Luxembourg and Madrid templates plus a Vanguard UK Stocks &
Shares ISA, and structured so that adding a new bank is a **data-only**
change (new fixtures + new ruleset + new template package).

The Vanguard ISA is a **tax-free wrapper**: its holdings carry
`account_wrapper="isa"` on each `Transaction`, the `tax-report` stage drops
every wrapped transaction before any CGT / dividend / interest computation,
and its beancount accounts live under a dedicated `…:Vgd:ISA:…` subtree.

The pipeline never imports `beancount` itself (GPL-2.0) — output is plain
text, and validation shells out to the `bean-check` binary.

## Pipeline data flow

Upstream of the per-document flow, the `import` command files raw PDFs (a
folder or the bank's `.zip`) into the dated `<year>/<account>/` archive that
the later stages read. It reuses the language→bank→doctype classifier but
stops before field extraction.

```
import  ──►  dated archive tree  <year>/<account>/…
                    │
                    ▼   (per document)
PDF ──► extractors/pdf_text.py ──► RawDocument
                                      │
                                      ▼
                    classifiers/language.py   (en | es | unknown)
                                      │
                                      ▼
                    classifiers/bank.py       (pictet | vanguard_uk | unknown)
                                      │
                                      ▼
                    classifiers/rules.py      (doctype, per-bank ruleset)
                                      │
                                      ▼
                    fields/hybrid.py ──► [Transaction, …]
                                      │
                                      ▼
                    writer/ ──► beancount text  (+ *.transactions.jsonl sidecar)
```

Three facade classifiers live in `classifiers/hybrid.py`:
`HybridClassifier` (single-stage rules+LLM), `TwoStageClassifier`
(bank → doctype), and `LayeredClassifier` (language → bank → doctype — the
default the `Pipeline` instantiates).

Each stage is **hybrid**: deterministic rules first; the Claude LLM fallback
only fires when rule confidence is below `rule_confidence_threshold`
(default `0.75`, from `BANKPIPE_RULE_CONFIDENCE_THRESHOLD`). LLM branches are
skipped silently when `BANKPIPE_ANTHROPIC_API_KEY` is unset.

The classifier rule format is `Rule(doc_type, template_id, patterns,
weight, bank)` — `patterns` is a tuple of compiled regexes; a document
scores `weight * hits / len(patterns)` against each rule and the **first**
rule to reach the highest score wins (so rule order matters on ties).
Confidence is a saturating function tuned so that a full pattern match hits
~0.95. `template_id` strings follow `<bank>.<doctype>.v<n>` (e.g.
`pictet.subscription_notice.v1`); the rule emits it and the hybrid extractor
routes on it.

Rules live in `classifiers/rules.py`: per-bank rulesets (`PICTET_EN_RULES` +
`PICTET_ES_RULES`, combined as `PICTET_RULES`, and `VANGUARD_UK_RULES`)
registered in `RULESETS_BY_BANK`, plus a bank-agnostic `GENERIC_RULES`. For a
given document the classifier runs `RULESETS_BY_BANK[bank] + GENERIC_RULES`,
so a rule that should fire regardless of bank goes in `GENERIC_RULES`.

The UK-tax pipeline is deliberately **separate** from the writer and reads
the JSONL sidecars, not the ledger — see [The UK-tax pipeline](#the-uk-tax-pipeline)
and [design-decisions.md](design-decisions.md).

## Module map

```
src/banking_pipeline/
├── cli/                Typer CLI package (commands grouped by domain).
│   ├── __init__.py       Assembles the ``app`` (entry point
│   │                       ``banking_pipeline.cli:app``); imports the
│   │                       group modules to register their commands
│   ├── _main.py          Shared hub: ``app`` + cross-cutting helpers
│   │                       (logging, statement discovery, commodity /
│   │                       property / sidecar loading, bean-check runner)
│   ├── ingest.py         import | ingest | dump-transactions |
│   │                       dedup-check | revolut
│   ├── inspect.py        classify | scan | extract-text
│   ├── statements.py     prices | balances | portfolio | property
│   ├── reports.py        concentration | net-worth | allocation |
│   │                       portfolio-allocation | income | holdings |
│   │                       fig-projection
│   ├── rebuild.py        rebuild | check | reconcile
│   └── tax.py            tax-report | tax-forecast | tax-pack | fig-advice
├── cli_options.py      Reusable Annotated Typer option aliases shared
│                         across commands (VerboseOpt + the statement-
│                         valuation report option set) — single definition
│                         so help text doesn't drift per command
├── pipeline.py         Top-level Pipeline orchestration
├── models.py           Domain models — DocumentType, BankId, Language,
│                         RawDocument, Classification, Transaction,
│                         FeeItem, ExtractionResult, NO_OUTPUT_DOCTYPES,
│                         TAX_EXEMPT_WRAPPERS (+ Transaction.account_wrapper
│                         / .is_tax_exempt — the ISA tax-shelter flag)
├── config.py           Pydantic settings (env_prefix=BANKPIPE_)
├── batch_config.py     `banking-pipeline.toml` schema for `rebuild`
├── archive.py          Files raw bank PDFs into a dated archive tree (the
│                         `import` command — the first pipeline stage). Source
│                         is a folder or a `.zip` (`source_pdfs` extracts the
│                         latter to a temp dir). Bank + doctype come from the
│                         shared `LayeredClassifier`; only the account /
│                         per-document reference / publication date / as-of
│                         date are scraped, by a bank parser keyed on
│                         `BankId` in `FIELD_PARSERS` (Pictet EN+ES today).
│                         Three filing shapes: advice, periodic statement,
│                         and Spanish IRPF / annual tax report (P&L reports,
│                         fiscal statement, ETE / Modelo 720 / UK income &
│                         CG → `<year>/tax/`). Uses the pypdfium2 extractor
├── tax_report_prune.py Retention policy for the archived Pictet P&L tax
│                         reports (pure `select_retained` + the
│                         `prune-tax-reports` command's helpers)
├── bean_check.py       Shells out to the bean-check binary
├── reconcile.py        Statement-balance reconciliation: parses bean-check
│                         assertion failures into a drift report (drift rows +
│                         earliest-drift + coverage gaps)
├── statement_completeness.py  Statement-completeness cross-check: parses the
│                         current-account cash ledger and diffs it against the
│                         sidecars by transaction (missing / unmatched)
├── valuation.py        Statement-valuation core (shared engine): the
│                         `RawHolding` model, `raw_from_statement` parser,
│                         and `value_holdings` (securities at qty×mark, cash
│                         netted by currency → GBP) returning a
│                         `ValuationResult`, plus the generic `as_of`
│                         forward-fill. Consumed by concentration / net_worth
│                         / allocation / portfolio_allocation as peers
├── report_format.py    Shared report rendering helpers: `money` / `gbp` /
│                         `pct` formatters + `rate_gap_lines` (the ⚠️
│                         missing-GBP-rate Markdown section)
├── concentration.py    Portfolio concentration / exposure report. Leverage-
│                         aware — weights are a share of gross long holdings
│                         and negative cash (a margin/Lombard loan) is netted
│                         by currency and reported separately
├── allocation.py       Asset-allocation-over-time: asset-class mix
│                         (equity / bond / property / … + net cash) across
│                         the statement timeline
├── portfolio_allocation.py  Per-portfolio allocation: breaks the latest
│                         valuation down per portfolio (each Pictet account,
│                         the ISA, each property), plus a cross-portfolio
│                         net-worth/share summary
├── income.py           Income-by-source report: dividends + interest
│                         *received* from the JSONL sidecars by period (UK
│                         tax year or calendar) and paying source, in GBP.
│                         Includes ISA income (flagged, not dropped); counts
│                         UK + foreign alike; credit-balance cash interest only
├── net_worth.py        Net-worth-over-time: values each statement snapshot
│                         at its date and builds a combined timeline via
│                         as-of forward-fill, deduping same-date statements
├── trial_balance.py    Per-account trial balance from the *ledger* (via
│                         `bean-query`, since cost-basis Realized/Unrealized
│                         legs are computed at load time): securities in
│                         units, cash native, plus a GBP market-value column
│                         on Assets/Liabilities (Equity/Income/Expenses stay
│                         native). The ledger-faithful counterpart to the
│                         statement-based valuation reports — and by design
│                         does **not** reconcile with them (different source /
│                         as-of / scope; the docstring spells out why)
├── balance_sheet.py    Dataset + artifact for the `balance-sheet` command:
│                         one `bean-query` over Asset/Liability postings →
│                         `BalanceSheetData` → compact JSON, inlined into the
│                         committed `balance_sheet_template.html` to produce a
│                         single offline HTML you scrub to any as-of date.
│                         `value_as_of` is the Python reference the template's
│                         JS ports
├── property.py         Off-ledger residential property: loads
│                         data/property.toml, renders data/property.beancount
│                         (per-property commodity held at cost + price marks,
│                         funded against Equity:Property:<label>); also feeds
│                         concentration / net-worth. EUR property gets a GBP
│                         price mark via the rate source
├── beancount_writer.py Back-compat re-export of `writer.*`
├── balances_extract.py Statement → balance assertions. Dispatches by bank:
│                         Pictet monthly statement + Vanguard ISA regular
│                         statement (each no-ops on the other's text).
│                         Two Pictet layouts: K's ISIN-led rows and the P
│                         mandate's by-name "Financial Statement" (no ISIN;
│                         holdings resolved name→ISIN via commodities.toml
│                         `statement_names`). Coverage guard flags dropped
│                         rows, unresolved by-name holdings, and a
│                         recognised statement that parses to nothing
├── prices_extract.py   Per-trade + statement → price directives. Trade
│                         prices read the ledger's cost-basis / `@` marks;
│                         statement marks come from Pictet valuation pages +
│                         the Vanguard ISA valuation snapshot
├── vanguard_statement.py  Shared Vanguard ISA "Your ISA investments at
│                            <date>" valuation parser (date, account,
│                            net-per-ticker holdings, cash)
├── portfolio_aggregate.py Central account opens + closes + per-year
│                         includes (closes ISIN accounts that net to zero
│                         across the full history — see below)
├── commodities_metadata.py  TOML loader for `data/commodities.toml`
│                              (ISIN → domicile, reporting status, asset
│                              class, `deeply_discounted`,
│                              `distributions_as_interest`)
├── opening_positions.py     TOML loader for `data/opening-positions.toml`
│                              — pre-ledger section-104 lots (cost basis)
├── cgt_losses.py            TOML loader for `data/cgt-losses.toml`
│                              — pre-ledger brought-forward CGT losses
├── transaction_sidecar.py   JSONL `*.transactions.jsonl` writer/reader —
│                              the structured substrate `tax-report`
│                              consumes; each line carries a derived
│                              `dedup_key`
├── dedup.py            Duplicate-transaction audit: content-keys each
│                         transaction and groups double-counted events
│                         (read-only; feeds `dedup-check`)
├── switch_pairing.py   Pure matcher that pairs a Pictet switch's
│                         `SWITCH_SALIDA` + `SWITCH_ENTRADA` legs onto one
│                         shared `^<link>`. Buckets by (account, clearing
│                         currency, booking date) then pairs by shared
│                         `order_date` (the FX-robust key), with amount-
│                         netting only as a fallback for legs lacking it.
│                         `ingest` runs it across the batch before render
│                         (see the `ingest` note below)
├── extractors/
│   └── pdf_text.py     pypdfium2-based PDF → text
├── classifiers/
│   ├── language.py     Stopword-frequency language detection
│   ├── bank.py         Hit-count-saturating bank identification
│   ├── rules.py        Per-bank, per-language doctype Rule registry
│   ├── llm.py          Claude-based fallback classifier
│   └── hybrid.py       Hybrid / TwoStage / Layered facades
├── fields/
│   ├── regex_extract.py  Generic regex field extraction
│   ├── llm_extract.py    Claude tool-use structured extraction
│   ├── validators.py     ISIN / IBAN via python-stdnum
│   └── hybrid.py         Template → regex → LLM dispatch + post-extraction
│                           enrichment (GBP rate stamp, withholding-country
│                           override)
├── fx/
│   └── gbp_rates.py    `GbpRateSource` protocol + `HmrcMonthlyAverageSource`
│                         (data/fx CSV) + `NullSource`;
│                         `build_rate_source(settings)` picks one
├── tax/
│   └── uk/
│       ├── tax_year.py    UK tax-year boundary helpers (6 Apr–5 Apr,
│       │                    label `"YYYY-YY"`)
│       ├── currency.py    `to_gbp(...)` — preferred per-tx rate, else
│       │                    `GbpRateSource` fallback, else None;
│       │                    `to_gbp_all(...)` converts several amounts in
│       │                    one rate context (None if any fails)
│       ├── section_104.py Section 104 pool + same-day + 30-day "bed and
│       │                    breakfast" share-matching + dated pool-cost
│       │                    adjustments (ERI uplift)
│       ├── sa108.py       SA108 CGT row builder (reads sidecars);
│       │                    `match_history` runs the matcher over the full
│       │                    history; routes deeply-discounted → income,
│       │                    non-reporting → offshore-income-gains
│       ├── cgt_allowance.py  Annual exempt amount + loss carry-forward:
│       │                    statutory deduction order + optimal mid-year
│       │                    rate-change allocation + year-to-year chain
│       ├── sa106.py       SA106 foreign dividend + interest aggregation
│       ├── eri.py         Excess reportable income + equalisation
│       │                    (data/eri.toml → income + base-cost uplift)
│       ├── residence.py   Split-year arrival filtering + 4-year FIG
│       │                    window/eligibility + UK-vs-foreign situs
│       ├── rates.py       Statutory income-tax bands/rates + CGT rate
│       │                    percentages by tax year (Settings-overridable)
│       ├── liability.py   UK stacking engine: SA108/SA106 amounts → an
│       │                    estimated £ liability (non-savings → savings →
│       │                    dividends → CGT, with PA taper + FTCR)
│       ├── tax_pack.py    Pure Markdown renderer tying the computed figures
│       │                    to HMRC form boxes (the `tax-pack` filing aid)
│       ├── fig_advice.py  Multi-year FIG claim optimiser: brute-forces the
│       │                    2^k claim subsets over the eligible window,
│       │                    loss-chain-aware, ranks by total window liability
│       └── fig_projection.py  FIG-window projection: prices deferring vs.
│                            crystallising foreign unrealised gains before the
│                            window closes (crystallise-now saving + act-by)
├── templates/
│   ├── __init__.py       TEMPLATE_REGISTRY (populated at import)
│   ├── pictet/           ~40 per-doctype templates (EN + ES locales)
│   └── vanguard_uk/      ISA templates — contract_note_buy/sell,
│                           regular_statement (deposit + interest only),
│                           direct_debit_details (account fee); NoOpTemplate
│                           for the paper-only doctypes. Ticker is the
│                           commodity (resolve_ticker maps fund name → ticker
│                           since sell notes omit it inconsistently)
├── writer/
│   ├── dispatch.py       Doctype → builder routing, render()/render_all()
│   ├── format.py         Amount/posting/account-name primitives
│   ├── profile.py        Per-bank writer config (account_prefix, …;
│   │                       Pictet → `Pic`, Vanguard → `Vgd:ISA`)
│   └── builders/         One module per render shape (incl. vanguard.py:
│                           ISA deposit/interest + account fee)
└── revolut/              CSV import side path (separate from PDF flow)
```

Fixtures live at `tests/fixtures/<language>/<bank>/<doctype>[.<tag>].txt`;
folder/file names **must match** the enum values exactly — that is how
`conftest.discover_fixtures` derives the expected classification without a
manifest. Beancount goldens sit next to their text fixtures with a
`.beancount` suffix and `tests/test_render_goldens.py` re-renders + diffs.

## The domain model

- `DocumentType` values stay in the **issuer's own vocabulary** for
  locale-specific variants (`SWITCH_SALIDA`, `COMPRA`, `REEMBOLSO`,
  `PAGO_INTERNA`, …); each enum value's docstring documents its
  distinguishing PDF-text markers — read it before authoring rules or
  templates. `Language` values are ISO 639-1 codes (`en`, `es`) + `unknown`.
- `NO_OUTPUT_DOCTYPES` (in `models.py`) is the single source of truth for
  "this doctype legitimately emits zero transactions": the writer
  short-circuits to `""` and the extractor treats an empty template result
  as expected. Periodic statements and paired-advice openings belong here.
- `Transaction` is the canonical row. `currency`/`amount` is the **cash-leg
  currency**; `security_currency` is the **trade-execution currency** — on
  FX trades they differ and `subtotal_security` + `fees` carry the
  pre-conversion amounts so the writer can emit `@@ <subtotal> <ccy>`
  without re-deriving. `Transaction.is_fx` is the single branch point.
- Internal cross-currency transfers between the user's own accounts are
  **one** `Transaction` with both legs (`counter_currency`/`counter_amount`),
  not two balanced against `Equity:Uncategorized`. Same for self-to-self
  payments (`gross_amount`/`counter_account`).
- `Transaction` also carries the UK-tax substrate `tax-report` needs without
  re-parsing beancount: `gbp_rate`; `gross_income` / `withholding_tax` /
  `withholding_country`; `accrued_interest`; and `document_type` provenance
  stamped by `Pipeline` after classification.
- `link_id` is the shared beancount `^<link>` threading a switch's two legs;
  `order_date` (Pictet's `Fecha de la orden`) is the corroborating key
  `switch_pairing` uses to set it. Both are `None` until `ingest`'s pairing
  phase runs. `order_date` isn't rendered — it exists only as the pairing
  signal (and rides along in the sidecar).

## The UK-tax pipeline

The tax pipeline reads the JSONL sidecars, never the ledger.

- Every `ingest` / `rebuild` writes a `<stem>.transactions.jsonl` next to
  each `.beancount` (see `transaction_sidecar.py`). This is the load-bearing
  substrate; `dump-transactions <pdf>` prints the same JSONL to stdout.
- **Tax-free wrappers (ISA):** each `Transaction` carries an optional
  `account_wrapper` (`"isa"` for the Vanguard ISA, set by the template);
  `Transaction.is_tax_exempt` is true when it is in `TAX_EXEMPT_WRAPPERS`.
  `tax-report` filters `[tx for tx … if not tx.is_tax_exempt]` immediately
  after loading the sidecars — a **single choke point**, before any
  `compute_*` / `match_history`. (Sidecar schema `…/v4`; older sidecars
  still load — additive fields like `account_wrapper` and `order_date`
  default to `None`.)
- GBP cost basis is carried as posting metadata (`gbp-rate`, `trade-date`)
  and as `Transaction.gbp_rate`. All section 104 / same-day / 30-day
  matching happens in `tax/uk/section_104.py` from the sidecar — beancount's
  booking methods do not implement UK matching rules.
- Rate sources are pluggable via the `GbpRateSource` protocol:
  `HmrcMonthlyAverageSource` (`data/fx/hmrc-monthly-average.csv`, monthly
  average), `EcbDailyRateSource` (`data/fx/ecb-daily.csv`, a daily spot proxy
  triangulated to GBP by `scripts/fetch_ecb_rates.py`), and `NullSource`
  (default). The per-transaction stamped `gbp_rate` wins over any source, so
  switching source for a whole ledger means re-ingesting, not just
  `--rate-source` at report time. When an amount can't be converted to GBP it is
  *excluded* and recorded as a `RateGap` on each report's `missing_rates`;
  `tax-report` / `tax-forecast` surface these and `--strict` makes any gap a
  non-zero exit.
- Reporting status (`reporting` / `non-reporting` / `uk-domestic` /
  `unknown`) lives in `data/commodities.toml` keyed by ISIN. `tax-report`
  routes disposals to SA108 (CGT) vs SA106 offshore income gains
  (non-reporting), and flags unknown status. Two further per-ISIN flags
  reroute income: `deeply_discounted` (gain taxed as income) and
  `distributions_as_interest` (a >60% bond fund — distributions + ERI are
  foreign interest, not dividends).
- CGT annual exempt amount + loss carry-forward live in
  `tax/uk/cgt_allowance.py`, layered on the section-104 gains: HMRC's
  deduction order (current-year losses first, then brought-forward only down
  to the AEA, then the AEA), mid-year rate-change relief against the higher
  bucket first, and the year-to-year chain. AEA values come from
  `cgt_annual_exempt_amount`.
- The model invariant `gross_income − withholding_tax == amount` (within a
  cent) is enforced in `Transaction`'s `@model_validator`.

### UK residence and the FIG regime

The pipeline assumes UK arising-basis residence across the whole history
unless `uk_residence_start_date` is set. `tax/uk/residence.py` applies two
corrections, both config-driven and both leaving the section-104 pool
untouched (acquisitions feed it whenever they happened — only the taxable
*output* is residence-filtered):

- **Pre-residence (split-year):** income/gains before the arrival date drop
  out; whole tax years before arrival are skipped. SA106/SA108 take an
  `arrival` parameter; the loss chain starts at the residence-start year.
- **4-year FIG claim (`fig_claim_years`, from 2025-26):** for an eligible
  year, foreign income and non-UK gains are relieved to nil but the personal
  allowance and CGT AEA are forfeited. Foreign-vs-UK situs is
  `CommodityMetadata.resolved_uk_situs`. The CLI partitions foreign items
  onto `fig-designation.csv`.

Out of scope (documented simplifications): the 10-prior-non-resident
eligibility test, whole-arrival-year ERI attribution, temporary-non-
residence clawback, former-remittance-basis rebasing/TRF. Not tax advice —
verify against HMRC guidance.

## Beancount output conventions

- Account paths are `Assets:<prefix>:<portfolio>:<currency>` for cash legs
  and `Assets:<prefix>:<portfolio>:<ISIN>` for security holdings.
  `<prefix>` comes from `writer/profile.py` (Pictet → `Pic`). Vanguard's
  prefix is the **two-segment** `Vgd:ISA`, which puts every ISA account
  under the dedicated `…:Vgd:ISA:…` subtree. Vanguard ISA holdings key on
  the fund **ticker** (`VMIG`, `VGVA`), not an ISIN; contributions post
  against `Equity:Vgd:ISA:Contributions`, the account fee against
  `Expenses:Vgd:ISA:Fees`.
- Portfolio identifiers are sanitised through `portfolio_segment` — Pictet
  prints `K-123456.001`; beancount segments don't allow `-` or `.`, so it
  becomes `K123456001`.
- Per-currency rounding tolerances live in the hand-curated `main.beancount`
  (`inferred_tolerance_default "<ccy>:0.005"` for cent fiat, `JPY:0.5`).
  ISIN commodities deliberately don't set a default.
- `data/portfolio.beancount` is **generated** (by `portfolio_aggregate`):
  it owns `option "operating_currency"`, the booking method, the central
  account opens, and the `close` directives. **Don't hand-edit it.**
  Hand-curated overrides go in `main.beancount`, which `include`s the
  aggregate.
  - **Close directives are aggregate-only.** A per-batch close can't see a
    *later* source re-acquiring a wound-down holding, and beancount can't
    reopen a closed account. The aggregate sums each ISIN asset account
    across the full history and closes only those that net to exactly zero.
    Don't re-add a close call to `ingest`.
  - `main.beancount` (the `bean-check` root) must itself declare
    `option "booking_method" "FIFO"` and `operating_currency`: beancount
    reads those options **only from the root file**. Omitting
    `booking_method` drops booking to `STRICT`, and every `{}` switch-out
    matching more than one lot then fails with "Ambiguous matches".
- `portfolio_aggregate` only treats flat per-year ingest files as sources:
  it skips any `*.beancount` containing an `include`, the aux files
  (`prices`/`balances`), and `property.beancount` (`_IGNORED_FILENAMES`).
  It only constrains a `…:<CCY>` leaf to a currency when that token actually
  appears as a posting currency. All guard against `bean-check` errors.
- `examples/accounts.beancount` is a starter chart for external readers —
  not loaded by the rebuild.

### Strict-mode dispatch (the failure-mode worth knowing)

`HybridExtractor` has a non-obvious dispatch when a registered template
returns `[]`:

- Doctype in `NO_OUTPUT_DOCTYPES` → log INFO, return `[]`. Expected.
- No template registered → fall through to regex / LLM.
- Template's optional `is_expected_empty(doc)` hook returns `True` → log
  INFO, return `[]`. Expected. Per-document escape hatch for a doctype
  that normally emits but is legitimately empty on some inputs (a
  nil-activity `vanguard_regular_statement`).
- Template registered but empty (no escape above) → log WARN, return `[]`,
  **skip** the regex/LLM fallback. With `strict=True` raise
  `TemplateExtractionError`.

The skip is deliberate: falling through historically papered over template
regressions with `Equity:Uncategorized`-balanced placeholder entries. The
fix surfaces the empty result as a missing entry (so the next `bean-check`
notices the imbalance) or as an exception under `--strict`.

`ingest --strict` and `rebuild --strict` both turn this on; `rebuild
--strict` also escalates reconcile coverage gaps to a failed rebuild.
bean-check fails on any error irrespective of `--strict` — beancount v3's
bean-check has no warnings-as-errors flag (the v2-era `-w` was removed), so
strict adds nothing to that step.

## CLI reference

Run via `uv run banking-pipeline …`. See the [README](../README.md) for
usage examples; this is the behavioural reference.

### Ingest / import

- `import` — the first pipeline stage: file a folder of raw downloads (or a
  `.zip`) into a dated archive tree. Bank + doctype come from the shared
  `LayeredClassifier`; the account number, per-document reference,
  publication date and as-of date are scraped to file each document.
  **Transaction advices** build `<dest>/<year>/<account>/<YYYYMMDD>-<reference>.pdf`;
  when two docs in the batch share a reference (e.g. an invoice + its
  debit-of-fees advice), each is filed with a title-cased doctype suffix so
  neither clobbers the other. Interest advices are always suffixed with
  their currency. **Periodic valuation statements** (monthly / quarterly /
  annual, both locales) carry no reference, so they file by their as-of
  (period-end) date into the account's `reports/` subfolder —
  `<dest>/<as-of-year>/<account>/reports/Valuation <period> <YYYYMMDD>.pdf`.
  **Spanish IRPF tax reports** — the daily Realised / Unrealised P&L reports
  (`TAX_REALISED_PL` / `TAX_UNREALISED_PL`) and the comprehensive annual
  fiscal statement (`TAX_FISCAL_STATEMENT`, "Informe fiscal personas
  físicas") — carry neither account nor reference, so they file by their
  numeric as-of date into a per-year `tax/` folder —
  `<dest>/<as-of-year>/tax/<stem> <YYYYMMDD>.pdf`, where `<stem>` is
  `Realised PL` / `Unrealised PL` / `Fiscal statement`. The statement is a
  superset of the P&L report distinguished by content (its `VALORACIÓN DE
  CARTERA` section + `Gastos de administración…` concept); its classifier
  rule sits before `TAX_REALISED_PL` to win the tie. The three **annual
  tax-authority filings** file the same way: `DECLARACION_ETE` → `ETE
  <YYYYMMDD>.pdf`, `MODELO_720` → `Modelo 720 <YYYYMMDD>.pdf`, and
  `INCOME_CAPITAL_GAINS_UK` → `Income and capital gains UK <YYYYMMDD>.pdf`
  (their as-of is pinned per kind — 31 Dec for ETE/720, 5 Apr for the UK
  report — from a prose period end). The `<YYYYMMDD>` for a tax report is its
  **effective date**, taken from the source filename's trailing `-<YYYYMMDD>`
  (Pictet's Publication/Effective date, `_effective_date_from_filename`) —
  authoritative, since the content's fiscal-reference date can be stale (a
  frozen `Al …` label); the content scraper (`_pictet_tax_as_of`) is only the
  fallback for dateless names, and a filename/content disagreement is logged
  (`archive.tax_report_date_mismatch`). These are all an archive-only
  reference source (never ingested, never fed to the UK-tax pipeline); the
  `prune-tax-reports` command trims the daily P&L volume (the annual statement
  and the tax-authority filings are kept, never pruned). An existing destination is left untouched; an unplaceable /
  unreadable PDF is reported and skipped. `--dry-run` prints planned moves.
  `source` / `dest` are positional, falling back to `import_source_glob` →
  `import_source_dir` and `import_archive_dir`; `[import] source_globs` adds
  further globs (e.g. the loose tax-report PDFs) that compose with the
  primary source. Pictet (both locales) is recognised today via
  `archive.FIELD_PARSERS`; a second bank is data-only.
  A fourth filing path handles the portal **CSV exports** — the
  cash-statement (`archive.file_cash_statements`) and Transactions
  (`archive.file_transactions_csv`) reports — wired into `rebuild`'s import
  step via `[import] cash_statement_globs` / `transactions_globs` (not the
  classifier — a CSV isn't a PDF). Each export is a full-history, both-mandate
  superset, so both file **keep-latest** (a shared `archive._file_keep_latest`):
  named by their max date (parsed from content — value date for the cash
  statement, trade date for the Transactions export) into
  `<dest>/cash-statements/Cash statement by value date <YYYYMMDD>.csv` /
  `<dest>/transactions/Transactions <YYYYMMDD>.csv`, with any older canonical
  copy moved to the folder's `_superseded/` (never deleted, the tax-report
  supersede convention). A byte-identical re-download is skipped; an export
  older than one already archived is skipped.
- `prune-tax-reports` — retention command for the archived Pictet P&L
  reports. Keeps, per calendar year + kind, the latest report per month plus
  the realised year-final and the unrealised on-or-before-5-April snapshot
  (the UK tax-year-end anchor); moves the rest to `<year>/tax/_superseded/`
  (a move, never a delete). Also sweeps aside legacy-named copies that
  duplicate an already-filed canonical report — found by **content** (the
  legacy filenames encode the download date, not the as-of, so re-downloads
  of one report collapse), reusing `archive.file_documents` in dry-run. Only
  canonically-named P&L files are pruned; the annual `Fiscal statement` /
  `ETE` / `Modelo 720` / `Income and capital gains UK` filings (canonical
  names but kept, not pruned) and the `_superseded/` folder are untouched, so
  a re-run is a no-op. It also **converges strays**
  (`tax_report_prune.find_superseded_strays`): when a `rebuild` / `import`
  re-files an already-pruned daily into `tax/`, prune can't move it aside (a
  same-named twin is already in `_superseded/`, so the move-aside warns and
  leaves it), and `tax/` accumulates strays a re-run keeps warning about. If
  the `tax/` copy is **byte-identical** (md5) to its superseded twin, the
  superseded copy is the record and the `tax/` copy is **deleted** (never
  moved) so the tree converges; a twin that *differs* — an unrealised snapshot
  re-valued under the same effective date — is left untouched (the critical
  safety guard). **Dry-run by
  default**; `--apply` performs the moves and stray deletions. Deliberately
  *not* wired into `rebuild` (import over-collects and prune trims — convergent
  but churny, so it stays manual). Selection policy:
  `tax_report_prune.select_retained` (pure, unit-tested).
- `ingest` — classify + extract + render one or more PDFs; supports
  `--check <ledger>` and `--strict`. Always writes a
  `<stem>.transactions.jsonl` sidecar next to the output `.beancount`.
  Runs in two phases: it **collects** every document's transactions for
  the batch, runs `switch_pairing.pair_switches` to stamp the shared
  `link_id` on paired switch legs, then **renders** — a switch's
  salida↔entrada link can't be known until both legs are in hand. Unpaired
  switch legs warn; under `--strict` an in-batch pair that should have
  netted but didn't is escalated to a hard error.
- `dump-transactions` — extract one or more PDFs and print the JSONL sidecar
  to stdout (no ledger touch).
- `dedup-check` — read-only audit. Walks `*.transactions.jsonl` sidecars
  under a directory (default `data`), content-keys each transaction (date +
  signed amount + currency + ISIN + doctype + account, *not* the ref), and
  reports collisions — `EXACT` (same ref) vs `POSSIBLE`. `--output` writes a
  CSV; exits nonzero when any duplicate is found.
- `revolut` — separate side path: Revolut Personal CSV → beancount.

### Inspect

- `classify` — print the language/bank/doctype verdict.
- `scan` — walk a directory; one row per PDF; `--json` for JSONL.
- `extract-text` — dump PDF text; `--show-rules` shows which rules fired at
  each stage (the rule-authoring workhorse).

### Statements

- `prices`, `balances`, `portfolio` — aggregators that read the ingest
  output under `data/`. `prices` / `balances` also parse statement PDFs
  passed via `--statement` / discovered by glob; both handle Pictet monthly
  statements and the Vanguard ISA regular statement (parsers self-select).
- `portfolio-split` — write one independently-loadable ledger per bank
  account (each `Assets:<prefix>:<portfolio>` root), so a single mandate can
  be loaded / checked in isolation. A generation utility beside `portfolio`;
  reads the same ingest output.
- `property` — generate the residential-property ledger from
  `data/property.toml`. Each property becomes a commodity held at cost
  revalued by `price` directives, funded against `Equity:Property:<label>`.
  A non-GBP property also gets a GBP price mark. Writes
  `<property_ledger_path>` (`data/property.beancount`); `include` it from
  `main.beancount`.

### Reports

- `concentration` — concentration / exposure. Reads the latest statement
  valuation per portfolio, values holdings in GBP, writes `concentration.md`
  + `holdings.csv`: breakdowns by holding / asset class / currency /
  domicile / issuer. Weights are a share of **gross long holdings**; a
  negative cash balance (margin / Lombard loan) is netted by currency and
  reported separately. Non-GBP holdings with no rate are excluded + flagged;
  `--strict` exits non-zero on any gap.
- `holdings` — cost basis + unrealised P&L. Joins the latest statement
  valuation per portfolio (market value, GBP) with a per-jurisdiction cost
  basis from a `BasisLens` (`--basis uk`, the UK section 104 pool from the
  sidecars; `es`/EUR-Spanish reserved, not yet built), writes `holdings.md` +
  `holdings.csv`. Reports per-holding unrealised gain/loss and cross-checks
  the statement quantity against the pool quantity, **classifying** each
  disagreement *timing* vs *gap*: a month-end mark is struck on settled
  positions, so an ingested trade that settles after the statement date is not
  yet on it while the trade-dated pool has already moved — such a drift is a
  settlement lead that clears with the next statement, not an ingest gap. The
  cutoff is `settlement_date` (fallback `trade_date`), and the net signed
  post-statement movement must equal `pool − statement` for a *timing* verdict;
  anything else is a *gap* to investigate (a missing trade confirmation or a
  stale statement). Cost basis reads the JSONL sidecars via `match_history` —
  never the ledger — and is a UK-tax lens: **not** Pictet's EUR/Spanish
  figures and never fed to the tax pipeline. ISA trades are excluded from the
  lens (tax-exempt, no section 104 basis); ISA holdings still show from the
  statement side with a blank cost. ERI base-cost uplift is folded in across
  the whole history (`cumulative_base_cost_adjustments`), so a reporting fund's
  cost matches the section 104 pool — and the report **decomposes it**: an
  `of which ERI` column (`eri_uplift_gbp`) shows the ERI portion inside each
  cost, the main reason the section 104 cost differs from a broker's book cost.
  The lens computes it by building the pool a second time *without* the cost
  adjustments and diffing (`HoldingBasis.cost_adjustment`) — the ERI *remaining*
  in the residual pool after disposals remove it proportionally, not the raw
  ERI figure. `--source` (default
  `data`) points at the sidecars; `--strict` exits non-zero on any valuation
  gap. Securities-only (no cash / property). Seam: `basis_lens.py` (neutral
  `BasisLens` / `HoldingBasis`) + `tax/uk/basis.py` (`UkSection104Lens`).
- `net-worth` — net-worth-over-time. Values *every* statement at its own
  date and builds a combined timeline (as-of forward-fill, same-date
  duplicates deduped). Writes `net-worth.md` + `net-worth.csv`. `--monthly`
  resamples onto a first-of-month grid instead of one row per raw statement
  date, dropping the mid-month rows where only the Vanguard ISA or a
  property valuation refreshed (the rebuild step's `net_worth_monthly`
  toggle does the same). A recognised nil statement (a Vanguard ISA whose
  current-column account total is £0.00) emits a zero-value snapshot via
  `drained_portfolio_snapshot`, so a wound-down account is retired from the
  forward-fill at its drain date rather than lingering at its last value.
- `allocation` — asset-allocation-over-time. At each timeline date reports
  the asset-class mix as a share of gross long, with net cash shown
  separately. Writes `allocation.md` + `allocation.csv`. Retires a
  wound-down portfolio on a recognised nil statement, as `net-worth` does.
- `portfolio-allocation` — per-portfolio allocation of the *latest*
  valuation (cross-portfolio summary + per-portfolio asset-class + holdings;
  cash netted within a portfolio). Writes `portfolio-allocation.md` + `.csv`.
- *Statement discovery (the four valuation reports above + the mandate
  reports):* pass statements via `--statement` (repeatable) and/or
  `--statements-dir` (with `-R`/`--statements-recursive` to descend). With
  **neither**, the report falls back to the rebuild's configured
  `balance_statements` globs (from `banking-pipeline.toml` in the cwd) — the
  same canonical set the rebuild's report step uses (Pictet monthly + the
  whole Vanguard ISA dir), expanded by filename (fast) and so matching
  rebuild output without hand-listing files. No config file → the usual "no
  statements given" error.
  Directory discovery (`--statements-dir`) **opens and classifies every PDF**
  to keep the valuation-bearing ones — robust on an arbitrary tree, but slow
  on the full Pictet archive (thousands of PDFs, mostly daily tax-report
  noise).
  `--statements-glob` is the opt-in **fast path**: it prunes the walk by
  filename *before* any PDF is opened, so only matches are classified. Pass
  the Pictet monthly convention `--statements-glob '*monthly*.pdf'` (the same
  the rebuild's `price_statements` / `balance_statements` globs use) to cut a
  whole-archive run from ~90s to ~10s with identical output.
  `holdings` additionally opts into `latest_only`: because it reports only the
  *latest* snapshot per portfolio, it prunes each discovered directory to its
  newest statement (by the `YYYYMMDD` in the filename) *before* opening any
  PDF — Pictet files each portfolio's monthly series in its own dir, so the
  superseded monthlies needn't be parsed. Content-based latest-per-portfolio
  selection still runs on the survivors, so the prune only ever makes it
  slower on an unexpected layout, never wrong. Combined with the glob, a
  whole-archive `holdings` run drops to ~2s (8 statements opened, not 78).
- `income` — income-by-source. Aggregates dividend + interest income from
  the sidecars by `--period` (`tax-year` default, or `calendar`) and paying
  source, in GBP. Unlike `tax-report` it **includes** ISA income (flagged in
  a wrapper column) and counts UK + foreign alike. Writes `income.md` +
  `income.csv`.
- `trial-balance` — per-account trial balance from the *ledger* (not the
  statements). Runs `bean-query` over the ledger (default `main.beancount`),
  listing each account's closing balance — securities in units, cash native
  — with a GBP market-value column on Assets/Liabilities (latest mark
  converted via `--rate-source`; Equity/Income/Expenses stay native). Writes
  `trial-balance.md` + `trial-balance.csv` to `trial_balance_reports_dir`.
  Needs the `bean-query` binary (`uv tool install beancount`); a missing
  binary is a warning, not an error. `--strict` exits non-zero if any
  Asset/Liability balance can't be valued in GBP. This is the
  ledger-faithful view and **does not reconcile** with the statement-based
  valuation reports (concentration / net-worth / allocation /
  portfolio-allocation) — different source (ledger positions vs latest
  statement snapshot), as-of (today vs last statement date), and scope.
- `balance-sheet` — a single self-contained `balance-sheet.html` (+ a
  `balance-sheet-data.json` sidecar) you can **scrub to any as-of date**
  entirely client-side. Like `trial-balance` it's a ledger construct:
  `bean-query` returns the Asset/Liability postings once, and the browser
  sums units ≤ the chosen date and values each holding to GBP (chaining
  commodity → quote currency → GBP), rendering the Assets / Liabilities
  (the Lombard loan = negative cash) / net-worth totals, a collapsible
  account tree, and a hand-rolled SVG allocation donut. Offline by
  construction (no CDN, nothing vendored). FX comes from the
  `GbpRateSource` (security marks in `data/prices.beancount` carry no
  currency→GBP rate); an unpriced holding is flagged, never zeroed. Needs
  `bean-query` (a missing binary warns + skips). Writes to
  `balance_sheet_reports_dir` — git-ignored, the artifact carries real
  balances. The module map entry is `balance_sheet.py` (+ the committed,
  data-free `balance_sheet_template.html`).

### Rebuild / validation

- `check` — standalone `bean-check` wrapper.
- `reconcile` — statement-balance reconciliation. Runs `bean-check`, parses
  balance-assertion failures, and writes `drift.csv` + `summary.txt` (drift
  rows, earliest-divergence date, coverage gaps, **MISSING PORTFOLIO**).
  Additive to `check`; reports the whole grid instead of aborting on the
  first failure. Exits nonzero on any drift; `--strict` also fails on
  coverage gaps and on a missing portfolio. The missing-portfolio check
  reads the account opens in `data/portfolio.beancount`
  (`parse_ledger_portfolios`, restricted to the reconcilable `Assets:Pic:` /
  `Assets:Vgd:` banks) and flags any portfolio the ledger holds but no
  statement asserts — the hole through which the P mandate hid before it had
  a parser path. Defaults: `ledger=main.beancount`,
  `--balances=data/balances.beancount`.

  **Pictet's two valuation layouts.** The K-*.001 statement lists each
  holding as a quantity-led row followed by an `ISIN:` marker; the P-*.002
  (leveraged Lombard) mandate uses the by-name "Financial Statement" layout
  — holdings printed by an abbreviated name with **no ISIN**, and cash rows
  carrying the GBP-reference conversion column + a weight. `balances_extract`
  runs both pattern sets per line (mutually exclusive by shape: K cash ends
  after one balance, by-name cash has two currency tokens + `%`; K security
  data is multi-line, the by-name security row has three currency tokens).
  By-name cash asserts directly; by-name holdings resolve name→ISIN through
  `commodities.toml` `statement_names` aliases
  (`build_statement_name_index`). The `--strict` coverage guard reports
  three kinds beyond a dropped cash/ISIN row: `unresolved-holding` (a by-name
  name with no alias — the fix is a `statement_names` entry),
  `empty-statement` (a recognised valuation that extracted zero rows — a
  whole-statement drop), and `unreadable`.
- `completeness` — statement-*completeness* cross-check, the
  transaction-level counterpart to `reconcile`'s balance-level one. Parses
  the Pictet current-account cash ledger (the authoritative list of every
  cash movement for its period — see
  [`statement_completeness.py`](../src/banking_pipeline/statement_completeness.py))
  from either a `Financial-statement-*.pdf` (one mandate + period; sign
  recovered from the running-balance delta) **or** a portal `Cash
  statement*.csv` export (`parse_cash_statement_csv` — all mandates + all
  currency sub-accounts over a long range, signed amounts direct, one report
  per mandate with the period synthesised from its value dates; the format
  is detected by suffix). The CSV export is the current source — only 4
  `Financial-statement` PDFs were ever pulled (K, to 2023-06-30) — and its
  bare `Account nr.` (no `K-`/`P-` letter) is resolved to the lettered
  sidecar portfolio via `lettered_portfolio_map`. Diffs against the
  `*.transactions.jsonl` sidecars under `--source` (default `data`). Writes
  one
  `summary-<portfolio>-<period-end>.txt` +
  `findings-<portfolio>-<period-end>.csv` per statement (keyed so
  successive runs or multiple portfolios don't clobber) under `completeness_dir`
  (`reports/completeness`): **MISSING-in-ledger** (a statement line with no
  ingested advice — a likely un-ingested document) and
  **UNMATCHED-in-ledger** (an ingested cash event with no statement line —
  a possible misdated booking). Securities settlements (`switch_*`,
  `liquidacion_recepcion_de_valores`, which post off the current account)
  and events outside the statement's window are excluded, not flagged.
  Match key is `(currency, amount, date≈)`; the FX/transfer counter-leg is
  expanded so both legs match. Pass statements via `--statement`
  (repeatable) and/or `--statements-dir` (scans `Financial-statement-*.pdf`
  and `Cash statement*.csv` recursively). Exits non-zero on any MISSING;
  `--strict` also fails on UNMATCHED.
- `reconcile-transactions` — the **transaction-level** counterpart to
  `completeness` (which covers only the cash ledger). Diffs every trade leg in
  the portal `Transactions` CSV export — both mandates, all trade types —
  against the sidecars by `Order nr.` (⇄ the sidecar `transaction_number`),
  so a securities trade the pipeline failed to ingest surfaces (it would
  corrupt the section 104 pool + CGT). One `summary-<portfolio>-<period-end>`
  + `findings-…` per mandate under `reconcile_transactions_dir`
  (`reports/reconcile-transactions`): **MISSING** (an export trade with no
  ingested transaction), **UNMATCHED** (a sidecar with no export row), and
  **AMOUNT_MISMATCH** (a matched single-leg securities order whose export cash
  amount ≠ the sidecar). Forex-forward opens (booked at settlement) and limit
  extensions (not transactions) are excluded; out-of-window sidecars are
  tallied, not flagged. Pass exports via `--transactions` / `--transactions-dir`
  (scans `Transactions*.csv`, skipping `_superseded/`). Exits non-zero on any
  MISSING or AMOUNT_MISMATCH; `--strict` also on UNMATCHED. Parser + diff live
  in [`transactions_export.py`](../src/banking_pipeline/transactions_export.py);
  the letterless CSV `Account nr.` resolves to the lettered sidecar portfolio
  via `lettered_portfolio_map` (shared with completeness).
- `rebuild` — end-to-end run driven by `banking-pipeline.toml`. Owns the
  `clean → ingest per source → prices/portfolio/balances → reports →
  reconcile → completeness → reconcile-transactions → check` sequence.
  `[post.reports]` (off by
  default) regenerates the analytical reports (income / concentration /
  net-worth / allocation / portfolio-allocation, plus opt-in `trial_balance`
  and `holdings` toggles, with per-report toggles) *before* reconcile/check
  so they land even when `bean-check` later exits nonzero; its `statements`
  glob falls back to `balance_statements` when unset. `[post.reconcile]`
  (off by default) runs *before* `check` for the same reason.
  `[post.completeness]` (off by default; needs a `statements` glob — the
  archived `cash-statements/*.csv`, or Financial-statement PDFs) runs
  alongside reconcile — MISSING fails the rebuild, UNMATCHED fails it under
  `strict`. Its CSV source is filed by the import step: `[import]
  cash_statement_globs` (e.g. `~/Downloads/Cash_statements_by_value_date_*.csv`)
  files the portal export into `<archive>/cash-statements/` by content
  (keep-latest — see the archive filing shapes above), bypassing the PDF
  classifier since a CSV isn't a PDF. Three things to get right when
  **downloading** the export (each a real footgun found in testing): (1)
  select **all currency sub-accounts** — the portal's currency filter defaults
  to a subset and silently drops one (an omitted sub-account reads as no cash
  movements for that currency); (2) export **by value date**, not booking date
  — `completeness` keys on `settlement_date` = the value date; (3) cut the
  window **past the latest settlement**, else a near-edge trade settling later
  sits beyond the horizon (correctly ingested, just not yet on the value-date
  ledger — an out-of-window row, not a gap).
  `[post.reconcile_transactions]` (off by default) runs the transaction-level
  cross-check after completeness — MISSING and AMOUNT_MISMATCH fail the
  rebuild, UNMATCHED under `strict`. Its source is filed by `[import]
  transactions_globs` (e.g. `~/Downloads/Transactions_*.csv`) keep-latest into
  `<archive>/transactions/`, the same content-filed path as the cash statement
  (both via `archive._file_keep_latest`).

### UK tax

- `tax-report --year 2025-26` — read sidecars under `--source` (default
  `data`), apply UK tax-year bounds + section 104 matching, and write under
  `reports/uk-tax/<year>/`: `sa108-disposals.csv` (CGT; `period` splits
  pre / on-or-after the year's CGT rate-change date),
  `sa106-dividends.csv`, `sa106-interest.csv`,
  `sa106-offshore-income-gains.csv`, `sa106-deep-discounted.csv`,
  `sa106-eri.csv`, `cgt-loss-carryforward.csv`, and `summary.txt`.
  `--rate-source` overrides `gbp_rate_source`; `--strict` exits non-zero on
  any missing GBP rate. Residence-aware (a pre-residence year is skipped;
  under a FIG claim foreign items move onto `fig-designation.csv`).
- `tax-forecast --income <gbp> [--year 2026-27]` — current-year liability
  estimate (defaults to the in-progress tax year). Reuses `tax-report` to
  compute year-to-date taxable amounts, stacks them in UK order, applies the
  statutory rates/bands, nets FTCR, and writes `forecast-summary.txt` +
  `forecast.csv`. Year-to-date *actuals* only; ISA excluded at the same
  choke point. When the year is FIG-eligible it computes with and without
  the claim and recommends the cheaper. `--strict` exits non-zero on any gap.
- `tax-pack [--year 2025-26]` — renders `tax-pack.md`, tying the computed
  SA108/SA106 figures to HMRC form boxes (a filing aid; box numbers
  caveated). Pure renderer in `tax/uk/tax_pack.py`.
- `fig-advice --income <gbp>` — recommends *which* FIG years to claim,
  optimised jointly across the eligible window (brute-forces the 2^k claim
  subsets, k ≤ 4, threading the loss chain per subset). Writes
  `fig-advice.txt`. A planning aid, not tax advice.
- `fig-projection --income <gbp>` — the forward companion to `fig-advice`:
  prices deferring vs. **crystallising** the **foreign** unrealised gains
  (from the holdings report's situs-split) during the remaining FIG window.
  The CGT that deferring would cost (the crystallisable gain stacked above
  `--income` at the `--year` rates, reusing `compute_liability`) is the
  crystallise-now saving; the act-by date is the window's close. Also shows
  each winner's **post-reset base cost** (its current market value — what
  future post-window CGT is measured from). An upper-bound estimate (ignores
  the AEA and post-death CGT uplift; flags the 30-day bed-and-breakfast
  mechanic but doesn't pick lots). Pure core in `tax/uk/fig_projection.py`;
  writes `fig-projection.md` + `.csv`. Not a rebuild step (needs a per-run
  `--income`) — an on-demand planning query.

## Configuration reference

Runtime config is `Settings` in `config.py`. Three sources, highest
precedence first: `BANKPIPE_`-prefixed env vars → `.env` → the `[settings]`
table of `banking-pipeline.toml` → field defaults
(`settings_customise_sources`). The TOML table is the home for structured /
personal config (keyed maps, residence/FIG knobs); env still overrides it,
and secrets (`anthropic_api_key`) stay env-only. `load_config` drops
`[settings]` and `Settings` reads only it, so the BatchConfig and Settings
schemas don't collide.

**Classifier / routing**

- `anthropic_api_key`, `anthropic_model`, `rule_confidence_threshold`,
  `default_currency`.
- `beneficiary_bank_map` (self-to-self destinations like Revolut → the
  counter-leg account `Equity:Transfers:<segment>:<ccy>`; see
  design-decisions) and `counterparty_account_map` (third-party named
  counterparties → account segments), both under `[settings.<map>]`.

**UK tax** (all optional, default to no-op)

- `gbp_rate_source` (`"null"` | `"hmrc-monthly"` | `"ecb-daily"`),
  `hmrc_rate_path` (default `data/fx/hmrc-monthly-average.csv`),
  `ecb_rate_path` (default `data/fx/ecb-daily.csv`).
- `commodities_metadata_path` (default `data/commodities.toml`),
  `opening_positions_path` (`data/opening-positions.toml`), `eri_path`
  (`data/eri.toml`), `cgt_losses_path` (`data/cgt-losses.toml`).
- `tax_reports_dir` (default `reports/uk-tax`),
  `cgt_rate_change_dates` (`{label: date}`, default
  `{"2024-25": 2024-10-30}`), `cgt_annual_exempt_amount` (`{label: Decimal}`).
- `tax-forecast`: `income_tax_bands` (`{label: IncomeTaxBands}`) and
  `cgt_forecast_rates` (`{label: CgtRateSchedule}`) — statutory
  England/Wales/NI defaults from `tax/uk/rates.py`, frozen 2024-25..2026-27.
  A forecast year missing from `income_tax_bands` aborts rather than
  guessing; add years to `rates.py` as HMRC sets them.

**Residence / FIG**

- `uk_residence_start_date` (`date | None`, default `None` = resident
  throughout) and `fig_claim_years` (`frozenset[str]`, default empty). A
  per-ISIN `uk_situs` override lives in `data/commodities.toml`.

**Report output directories**

- `reconciliation_dir` (`reports/reconciliation`),
  `completeness_dir` (`reports/completeness`),
  `concentration_reports_dir` (`reports/concentration`),
  `holdings_reports_dir` (`reports/holdings`),
  `net_worth_reports_dir` (`reports/net-worth`),
  `income_reports_dir` (`reports/income`),
  `allocation_reports_dir` (`reports/allocation`),
  `portfolio_allocation_reports_dir` (`reports/portfolio-allocation`),
  `trial_balance_reports_dir` (`reports/trial-balance`).

**Property**

- `property_path` (`data/property.toml`), `property_ledger_path`
  (`data/property.beancount`).

**Import**

- `import_source_glob` / `import_source_dir` / `import_archive_dir` (all
  default unset). Source resolves: positional `source` → `import_source_glob`
  (a glob, `~` allowed, several sources filed as one batch) →
  `import_source_dir`. The positional `dest` overrides `import_archive_dir`.

**Batch config** — `banking-pipeline.toml` (gitignored, schema in
`batch_config.py`) carries personal Dropbox/iCloud paths plus
`[post.reports]`, `[post.reconcile]`, `[post.check]` toggles. Its
`[settings]` table feeds `Settings`. `.env.example` /
`banking-pipeline.example.toml` are the committed templates.

The four user-maintained TOMLs that feed `tax-report` are all gitignored
with a committed `.example.toml`: `data/commodities.toml`,
`data/opening-positions.toml`, `data/eri.toml`, `data/cgt-losses.toml`.

## Adding a new bank

Adding a new bank is a **data-only** change:

1. Add a `BankId` enum value in `models.py`.
2. Add a bank-detection `BankRule` in `classifiers/bank.py`.
3. Add a per-language ruleset in `classifiers/rules.py` and register it
   under `RULESETS_BY_BANK`.
4. Create a `templates/<bank>/` package with per-doctype templates, each
   exposing a `template_id: str` class attribute and an
   `extract(doc) -> list[Transaction]` method. Register them in that
   package's exported tuple (mirror `PICTET_TEMPLATES`).
5. Add a `BankWriterProfile` in `writer/profile.py` keyed on the new
   `BankId` (sets `account_prefix` and any future knobs).
6. Drop fixtures under `tests/fixtures/<lang>/<bank>/<doctype>.txt` — the
   parametric tests pick them up automatically.

When adding a new **doctype**, the loop is: dump text with
`uv run banking-pipeline extract-text --show-rules <file>`, add a `Rule` and
a fixture, then a template, then a golden. The parametric suites in
`test_pictet_fixtures.py` and `test_language.py` pick up the new fixture
automatically and assert each stage clears `rule_confidence_threshold`.

Read the relevant `DocumentType` enum docstring before writing a new
template — it usually documents the exact PDF-text markers that distinguish
the doctype from its near-neighbours.

## Anonymisation and the PII guard

Fixtures are scrubbed of **personal identifiers** before they're committed —
name, NI number, DOB, home address, and IBANs are replaced; security names
and trade amounts are kept (needed for meaningful goldens, not identity
PII). Account numbers use a **dummy placeholder**: the Pictet portfolio body
is `999999` (most EN fixtures), `123456` (ES fixtures + buy/sell-shares), or
`000000` (numbers-only test inputs); the Vanguard ISA account is `VG0000000`;
the NI placeholder is `AB123456C`. When you add a fixture from a real PDF,
scrub it to one of these forms (mirror an existing sibling).

`scripts/check_pii.py` enforces this as a **pre-commit guard** (install
once: `git config core.hooksPath scripts/git-hooks`). It blocks a commit
whose staged content carries a non-allow-listed Pictet/Vanguard account, a
UK-NI-shaped string, or any regex in a local git-ignored `.pii-deny`
(template: `.pii-deny.example`). `python3 scripts/check_pii.py --all` audits
the whole tracked tree.

The guard catches identifiers, not *derived* figures: never cite real
amounts, balances, gains/losses, or holdings taken from the gitignored
personal data (`data/`, `reports/`) in committed docs, commit messages, or
backlog/changelog entries — describe the issue generically. The numbers are
personal financial data even when no account number appears.

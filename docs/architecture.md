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
│   │                       portfolio-allocation | income
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
│                         Uses the pypdfium2 extractor
├── bean_check.py       Shells out to the bean-check binary
├── reconcile.py        Statement-balance reconciliation: parses bean-check
│                         assertion failures into a drift report (drift rows +
│                         earliest-drift + coverage gaps)
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
├── property.py         Off-ledger residential property: loads
│                         data/property.toml, renders data/property.beancount
│                         (per-property commodity held at cost + price marks,
│                         funded against Equity:Property:<label>); also feeds
│                         concentration / net-worth. EUR property gets a GBP
│                         price mark via the rate source
├── beancount_writer.py Back-compat re-export of `writer.*`
├── balances_extract.py Statement → balance assertions. Dispatches by bank:
│                         Pictet monthly statement + Vanguard ISA regular
│                         statement (each no-ops on the other's text)
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
│       └── fig_advice.py  Multi-year FIG claim optimiser: brute-forces the
│                            2^k claim subsets over the eligible window,
│                            loss-chain-aware, ranks by total window liability
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
  `compute_*` / `match_history`. (Sidecar schema `…/v3`; a v2 sidecar still
  loads, `account_wrapper` defaulting to `None`.)
- GBP cost basis is carried as posting metadata (`gbp-rate`, `trade-date`)
  and as `Transaction.gbp_rate`. All section 104 / same-day / 30-day
  matching happens in `tax/uk/section_104.py` from the sidecar — beancount's
  booking methods do not implement UK matching rules.
- Rate sources are pluggable via the `GbpRateSource` protocol:
  `HmrcMonthlyAverageSource` (`data/fx/hmrc-monthly-average.csv`) and
  `NullSource` (default). When an amount can't be converted to GBP it is
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
- Template registered but empty → log WARN, return `[]`, **skip** the
  regex/LLM fallback. With `strict=True` raise `TemplateExtractionError`.

The skip is deliberate: falling through historically papered over template
regressions with `Equity:Uncategorized`-balanced placeholder entries. The
fix surfaces the empty result as a missing entry (so the next `bean-check`
notices the imbalance) or as an exception under `--strict`.

`ingest --strict` and `rebuild --strict` both turn this on; `rebuild
--strict` also escalates `bean-check` warnings to errors and reconcile
coverage gaps to a failed rebuild.

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
  An existing destination is left untouched; an unplaceable / unreadable PDF
  is reported and skipped. `--dry-run` prints planned moves. `source` /
  `dest` are positional, falling back to `import_source_glob` →
  `import_source_dir` and `import_archive_dir`. Pictet (both locales) is
  recognised today via `archive.FIELD_PARSERS`; a second bank is data-only.
- `ingest` — classify + extract + render one or more PDFs; supports
  `--check <ledger>` and `--strict`. Always writes a
  `<stem>.transactions.jsonl` sidecar next to the output `.beancount`.
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
- `net-worth` — net-worth-over-time. Values *every* statement at its own
  date and builds a combined timeline (as-of forward-fill, same-date
  duplicates deduped). Writes `net-worth.md` + `net-worth.csv`.
- `allocation` — asset-allocation-over-time. At each timeline date reports
  the asset-class mix as a share of gross long, with net cash shown
  separately. Writes `allocation.md` + `allocation.csv`.
- `portfolio-allocation` — per-portfolio allocation of the *latest*
  valuation (cross-portfolio summary + per-portfolio asset-class + holdings;
  cash netted within a portfolio). Writes `portfolio-allocation.md` + `.csv`.
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

### Rebuild / validation

- `check` — standalone `bean-check` wrapper.
- `reconcile` — statement-balance reconciliation. Runs `bean-check`, parses
  balance-assertion failures, and writes `drift.csv` + `summary.txt` (drift
  rows, earliest-divergence date, coverage gaps). Additive to `check`;
  reports the whole grid instead of aborting on the first failure. Exits
  nonzero on any drift; `--strict` also fails on coverage gaps. Defaults:
  `ledger=main.beancount`, `--balances=data/balances.beancount`.
- `rebuild` — end-to-end run driven by `banking-pipeline.toml`. Owns the
  `clean → ingest per source → prices/portfolio/balances → reports →
  reconcile → check` sequence. `[post.reports]` (off by default) regenerates
  the analytical reports (income / concentration / net-worth / allocation /
  portfolio-allocation, plus an opt-in `trial_balance` toggle, with
  per-report toggles) *before* reconcile/check so they land even when
  `bean-check` later exits nonzero; its `statements`
  glob falls back to `balance_statements` when unset. `[post.reconcile]`
  (off by default) runs *before* `check` for the same reason.

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
- `beneficiary_bank_map` (self-to-self destinations like Revolut) and
  `counterparty_account_map` (third-party named counterparties → account
  segments), both under `[settings.<map>]`.

**UK tax** (all optional, default to no-op)

- `gbp_rate_source` (`"null"` | `"hmrc-monthly"`), `hmrc_rate_path`
  (default `data/fx/hmrc-monthly-average.csv`).
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
  `concentration_reports_dir` (`reports/concentration`),
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

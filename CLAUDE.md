# CLAUDE.md

Project-specific instructions for Claude working in this repo. The
`README.md` is the authoritative user-facing guide; this file captures
the non-obvious context and conventions that aren't worth re-reading
the README for every session. For the rationale behind the load-bearing
choices see `docs/design-decisions.md`; for what's shipped vs. open see
`CHANGELOG.md` / `docs/backlog.md` (`docs/README.md` maps every doc).

## Project summary

`banking-pipeline` is a Python data pipeline that ingests banking
correspondence (PDFs and one Revolut-CSV side path), classifies each
document by language → bank → doctype, extracts the fields that matter
for accounting, and emits [beancount](https://beancount.github.io/)
entries. It is single-user, built around Pictet's Luxembourg and Madrid
templates plus a Vanguard UK Stocks & Shares ISA, and structured so
adding a new bank is a data-only change (new fixtures + new ruleset +
new template package).

The Vanguard ISA is a **tax-free wrapper**: its holdings carry
`account_wrapper="isa"` on each `Transaction`, the `tax-report` stage
drops every wrapped transaction before any CGT / dividend / interest
computation (see the UK-tax section), and its beancount accounts live
under a dedicated `…:Vgd:ISA:…` subtree (the Vanguard writer profile's
`account_prefix` is the two-segment `Vgd:ISA`).

The pipeline never imports `beancount` itself (GPL-2.0) — output is
plain text. Validation shells out to the `bean-check` binary.

## Tone

Direct and professional, not chatty. Skip preamble; lead with the
answer or the change.

## Tech / build

- Python **3.14** strict (`requires-python = ">=3.14"`,
  `mypy strict`, `target-version = "py314"`).
- Dependency manager: **uv**. Use `uv run …` for every command — do
  not invoke `python`/`pytest`/`mypy`/`ruff` from outside `uv run`,
  and do not `pip install` anything; edit `pyproject.toml` and let
  `uv sync` resolve.
- Lint / typecheck / test:

  ```
  uv run ruff check .
  uv run mypy src
  uv run pytest
  ```

- License hygiene matters: every runtime dep is MIT / BSD / Apache-2.0
  except `python-stdnum` (LGPL-2.1+, dynamic linking only).
  Specifically: **do not add `PyMuPDF`** (AGPL-3.0) and **do not
  `import beancount`** (GPL-2.0). The README's "Libraries and
  licenses" section is the source of truth — read it before adding a
  dependency.

## Architecture in one screen

```
PDF ──► extractors/pdf_text.py ──► RawDocument
                                      │
                                      ▼
                    classifiers/language.py   (en | es | unknown)
                                      │
                                      ▼
                    classifiers/bank.py       (pictet | unknown)
                                      │
                                      ▼
                    classifiers/rules.py      (doctype, per-bank ruleset)
                                      │
                                      ▼
                    fields/hybrid.py ──► [Transaction, …]
                                      │
                                      ▼
                    writer/ ──► beancount text
```

Three facade classifiers live in `classifiers/hybrid.py`:
`HybridClassifier` (single-stage rules+LLM), `TwoStageClassifier`
(bank → doctype), and `LayeredClassifier` (language → bank → doctype —
the default the `Pipeline` instantiates).

Each stage is **hybrid**: deterministic rules first; the Claude LLM
fallback only fires when rule confidence is below
`rule_confidence_threshold` (default `0.75`, from
`BANKPIPE_RULE_CONFIDENCE_THRESHOLD`). LLM branches are skipped
silently when `BANKPIPE_ANTHROPIC_API_KEY` is unset.

## Source layout

```
src/banking_pipeline/
├── cli.py              Typer entrypoint (ingest | dump-transactions |
│                         classify | scan | extract-text | revolut |
│                         dedup-check |
│                         prices | balances | portfolio | concentration |
│                         net-worth | income | property | check | reconcile |
│                         rebuild | tax-report | tax-forecast | tax-pack |
│                         fig-advice)
├── pipeline.py         Top-level Pipeline orchestration
├── models.py           Domain models — DocumentType, BankId, Language,
│                         RawDocument, Classification, Transaction,
│                         FeeItem, ExtractionResult, NO_OUTPUT_DOCTYPES,
│                         TAX_EXEMPT_WRAPPERS (+ Transaction.account_wrapper
│                         / .is_tax_exempt — the ISA tax-shelter flag)
├── config.py           Pydantic settings (env_prefix=BANKPIPE_)
├── batch_config.py     `banking-pipeline.toml` schema for `rebuild`
├── bean_check.py       Shells out to the bean-check binary
├── reconcile.py        Statement-balance reconciliation: parses
│                         bean-check assertion failures into a drift
│                         report (drift rows + earliest-drift +
│                         coverage gaps)
├── concentration.py    Portfolio concentration / exposure report: reads
│                         the latest statement valuation per portfolio
│                         (via extract_balances/prices_from_statement),
│                         values holdings in GBP, breaks down by holding /
│                         asset class / currency / domicile. Leverage-aware
│                         — weights are a share of gross long holdings and
│                         negative cash (a margin/Lombard loan) is netted
│                         by currency and reported separately (the
│                         `concentration` command). `_value_holdings` /
│                         `_raw_from_statement` are shared with net_worth
├── income.py           Income-by-source report: aggregates dividends +
│                         interest *received* from the JSONL sidecars by
│                         period (UK tax year or calendar year) and paying
│                         source, valued in GBP. Reuses SA106's bond-fund
│                         distribution→interest reclassification; unlike the
│                         tax pipeline it *includes* ISA income (flagged via
│                         a wrapper column, not dropped) and counts UK +
│                         foreign alike (the `income` command). Cash interest
│                         counts only credit-balance payments (positive
│                         amount); overdraft interest the user pays is an
│                         expense and excluded
├── net_worth.py        Net-worth-over-time: values each statement
│                         snapshot at its date (reusing concentration's
│                         valuation) and builds a combined timeline across
│                         portfolios via as-of forward-fill, deduping
│                         same-date statements (the `net-worth` command)
├── property.py         Off-ledger residential property: loads
│                         data/property.toml and renders a generated
│                         data/property.beancount (per-property commodity
│                         held at cost + price marks, funded against
│                         Equity:Property:<label>); also feeds the
│                         concentration / net-worth reports (the `property`
│                         command). EUR property gets a GBP price mark via
│                         the rate source
├── beancount_writer.py Back-compat re-export of `writer.*`
├── balances_extract.py Statement → balance assertions. Dispatches by
│                         bank: Pictet monthly statement + Vanguard ISA
│                         regular statement (each no-ops on the other's
│                         text). Vanguard emits a cash assertion + one per
│                         non-zero holding.
├── prices_extract.py   Per-trade + statement → price directives. Trade
│                         prices read the ledger's cost-basis / `@` marks
│                         (ISIN *or* ticker commodities); statement marks
│                         come from Pictet valuation pages + the Vanguard
│                         ISA valuation snapshot.
├── vanguard_statement.py  Shared Vanguard ISA "Your ISA investments at
│                            <date>" valuation parser (date, account,
│                            net-per-ticker holdings, cash) consumed by
│                            balances_extract + prices_extract
├── portfolio_aggregate.py Central account opens + closes + per-year
│                         includes (closes ISIN accounts that net to zero
│                         across the full history — see below)
├── commodities_metadata.py  TOML loader for `data/commodities.toml`
│                              (ISIN → domicile, reporting status,
│                              asset class, `deeply_discounted`,
│                              `distributions_as_interest`)
├── opening_positions.py     TOML loader for `data/opening-positions.toml`
│                              — pre-ledger section-104 lots (cost basis)
├── cgt_losses.py            TOML loader for `data/cgt-losses.toml`
│                              — pre-ledger brought-forward CGT losses
│                              seeding the loss-carry-forward chain
├── transaction_sidecar.py   JSONL `*.transactions.jsonl` writer/reader
│                              — the structured substrate `tax-report`
│                              consumes; each line also carries a derived
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
│   └── hybrid.py         Template → regex → LLM dispatch + post-
│                           extraction enrichment (GBP rate stamp,
│                           withholding-country override)
├── fx/
│   └── gbp_rates.py    `GbpRateSource` protocol +
│                         `HmrcMonthlyAverageSource` (data/fx CSV) +
│                         `NullSource`; `build_rate_source(settings)`
│                         picks one
├── tax/
│   └── uk/
│       ├── tax_year.py    UK tax-year boundary helpers (6 Apr–5 Apr,
│       │                    label `"YYYY-YY"`)
│       ├── currency.py    `to_gbp(...)` — preferred per-tx rate, else
│       │                    `GbpRateSource` fallback, else None
│       ├── section_104.py Section 104 pool + same-day + 30-day
│       │                    "bed and breakfast" share-matching +
│       │                    dated pool-cost adjustments (ERI uplift)
│       ├── sa108.py       SA108 CGT row builder (reads sidecars);
│       │                    `match_history` runs the matcher over the
│       │                    full history (shared with the loss chain);
│       │                    routes deeply-discounted → income, non-
│       │                    reporting → offshore-income-gains
│       ├── cgt_allowance.py  Annual exempt amount + loss carry-forward:
│       │                    statutory deduction order + optimal mid-year
│       │                    rate-change allocation + year-to-year chain
│       ├── sa106.py       SA106 foreign dividend + interest aggregation
│       ├── eri.py         Excess reportable income + equalisation
│       │                    (data/eri.toml → income + base-cost uplift)
│       ├── residence.py   Split-year arrival filtering + 4-year FIG
│       │                    window/eligibility + UK-vs-foreign situs
│       │                    (the residence/FIG corrections to the
│       │                    arising-basis default)
│       ├── rates.py       Statutory income-tax bands/rates + CGT rate
│       │                    percentages by tax year (the `tax-forecast`
│       │                    inputs; Settings exposes both as overridable)
│       ├── liability.py   UK stacking engine: turns the SA108/SA106
│       │                    amounts into an estimated £ liability
│       │                    (non-savings → savings → dividends → CGT,
│       │                    with PA taper + foreign tax credit relief)
│       ├── tax_pack.py    Pure Markdown renderer: ties the computed
│       │                    SA108/SA106 figures to HMRC form boxes (the
│       │                    `tax-pack` filing aid; box numbers caveated)
│       └── fig_advice.py  Multi-year FIG claim optimiser: brute-forces
│                            the 2^k claim subsets over the eligible
│                            window, loss-chain-aware, ranks by total
│                            window liability (the `fig-advice` command)
├── templates/
│   ├── __init__.py       TEMPLATE_REGISTRY (populated at import)
│   ├── pictet/           ~40 per-doctype templates (EN + ES locales)
│   └── vanguard_uk/      ISA templates — contract_note_buy/sell,
│                           regular_statement (deposit + interest only),
│                           direct_debit_details (account fee); NoOpTemplate
│                           for the paper-only doctypes. Ticker is the
│                           commodity (resolve_ticker maps fund name →
│                           ticker since sell notes omit it inconsistently)
├── writer/
│   ├── dispatch.py       Doctype → builder routing, render()/render_all()
│   ├── format.py         Amount/posting/account-name primitives
│   ├── profile.py        Per-bank writer config (account_prefix, …;
│   │                       Pictet → `Pic`, Vanguard → `Vgd:ISA`)
│   └── builders/         One module per render shape (incl. vanguard.py:
│                           ISA deposit/interest + account fee)
└── revolut/              CSV import side path (separate from PDF flow)
```

## Domain conventions

- `DocumentType` values are intentionally kept in the **issuer's own
  vocabulary** for locale-specific variants — `SWITCH_SALIDA`,
  `COMPRA`, `REEMBOLSO`, `PAGO_INTERNA` etc. Don't anglicise them.
  Each enum value's docstring explains the doctype's distinguishing
  features at the PDF-text level — read it before authoring rules or
  templates.
- `Language` values are ISO 639-1 two-letter codes (`en`, `es`) plus
  the `unknown` sentinel.
- `NO_OUTPUT_DOCTYPES` (in `models.py`) is the single source of truth
  for "this doctype legitimately emits zero transactions". Two
  callers consult it: the writer short-circuits to `""`, and the
  extractor treats an empty template result as expected (not a
  regression). Periodic statements (monthly/quarterly/annual, both
  locales) and paired-advice openings (`FX_FORWARD`,
  `CAMBIO_DE_DIVISAS_APERTURA`,
  `LIQUIDACION_AVISO_PREVIO_RECEPCION`) belong here.
- `Transaction` is the canonical row. `currency`/`amount` is the
  **cash-leg currency** (the client account that's debited/credited);
  `security_currency` is the **trade-execution currency**. On FX
  trades they differ and `subtotal_security` + `fees` carry the
  pre-conversion amounts so the writer can emit a beancount
  `@@ <subtotal> <ccy>` annotation without re-deriving and risking
  rounding drift. `Transaction.is_fx` is the single branch point.
- Internal cross-currency transfers between the user's own accounts
  are modelled as **one** `Transaction` with both legs
  (`counter_currency` / `counter_amount`), not two transactions
  balanced against `Equity:Uncategorized`. Same pattern for
  self-to-self payments (`gross_amount` / `counter_account`).
- `Transaction` also carries the UK-tax substrate the `tax-report`
  CLI needs without re-parsing beancount: `gbp_rate` (GBP per 1 unit
  of `currency` at trade date, stamped during extraction);
  `gross_income` / `withholding_tax` / `withholding_country` for
  foreign-dividend WHT (model invariant: `gross_income -
  withholding_tax == amount`); `accrued_interest` for bond
  buys/sells (UK accrued-income-scheme split — keep it signed
  as Pictet prints it, the bond builder flips the sign for the
  income leg); and `document_type` provenance stamped by `Pipeline`
  after classification so sidecar consumers can tell a buy from a
  sell from a dividend without re-classifying.

## Classifier / template extension points

Adding a new bank is a data-only change:

1. Add a `BankId` enum value in `models.py`.
2. Add a bank-detection `BankRule` in `classifiers/bank.py`.
3. Add a per-language ruleset in `classifiers/rules.py` and register
   it under `RULESETS_BY_BANK`.
4. Create a `templates/<bank>/` package with per-doctype templates,
   each exposing a `template_id: str` class attribute and an
   `extract(doc) -> list[Transaction]` method. Register them in that
   package's exported tuple (mirror `PICTET_TEMPLATES`).
5. Add a `BankWriterProfile` in `writer/profile.py` keyed on the new
   `BankId` (sets the `account_prefix` and any future bank-specific
   knobs).
6. Drop fixtures under `tests/fixtures/<lang>/<bank>/<doctype>.txt` —
   the parametric tests pick them up automatically.

The classifier rule format is `Rule(doc_type, template_id, patterns,
weight, bank)` — `patterns` is a tuple of compiled regexes; the
document scores `weight * hits / len(patterns)` against each rule and
the highest score wins. Confidence is a saturating function tuned so
that a full pattern match hits ~0.95.

`template_id` strings follow `<bank>.<doctype>.v<n>` (e.g.
`pictet.subscription_notice.v1`). The rule emits this; the hybrid
extractor routes on it.

## Fixtures and tests

Fixtures live at `tests/fixtures/<language>/<bank>/<doctype>[.<tag>].txt`.
The folder/file names **must match** the enum values exactly — that's
how `conftest.discover_fixtures` derives the expected classification
without a manifest. Optional `.<tag>` after the doctype disambiguates
multiple samples of the same doctype (e.g.
`redemption_notice.anonymised.txt`).

Beancount goldens sit next to their text fixtures with a `.beancount`
suffix (e.g. `tests/fixtures/en/pictet/buy_bonds.beancount`).
`tests/test_render_goldens.py` re-renders against the corresponding
`.txt` and diffs.

When adding a new doctype, the loop is: dump text with
`uv run banking-pipeline extract-text --show-rules <file>`, add a
`Rule` and a fixture, then a template, then a golden. Parametric
suites in `test_pictet_fixtures.py` and `test_language.py` pick up
the new fixture automatically and assert each stage clears
`rule_confidence_threshold`.

## Anonymisation and the PII guard

Fixtures are scrubbed of **personal identifiers** before they're
committed — the name, NI number, DOB, home address, and IBANs are
replaced; security names and trade amounts are kept (they're needed
for meaningful goldens and aren't identity PII). Account numbers use a
**dummy placeholder**: the Pictet portfolio body is `999999` (most EN
fixtures), `123456` (ES fixtures + buy/sell-shares), or `000000`
(numbers-only test inputs) — never the real account number; the
Vanguard ISA account is `VG0000000`, never the real value; the NI
placeholder is `AB123456C`. When you add a fixture from a real PDF, scrub it to one of
these forms (mirror an existing sibling).

`scripts/check_pii.py` enforces this as a **pre-commit guard** (install
once: `git config core.hooksPath scripts/git-hooks`). It blocks a commit
whose staged content carries a Pictet/Vanguard account that isn't an
allow-listed placeholder, a UK-NI-shaped string, or any regex in a
local git-ignored `.pii-deny` (template: `.pii-deny.example`). The guard
allow-lists placeholders rather than embedding real values, so it never
leaks anything itself; `python3 scripts/check_pii.py --all` audits the
whole tracked tree. Don't put real account numbers in docstrings or test
inputs — use a placeholder, or the guard will (correctly) reject them.

The guard catches identifiers, not *derived* figures. So also never cite
real amounts, balances, gains/losses, or holdings taken from the
gitignored personal data (`data/`, `reports/`) in committed docs,
commit messages, or backlog/changelog entries — describe the issue
generically (e.g. "a disallowed foreign loss", not the £ amount). The
numbers are personal financial data even when no account number appears.

## Strict-mode dispatch (the failure-mode worth knowing)

`HybridExtractor` has a non-obvious dispatch when a registered
template returns `[]`:

- Doctype in `NO_OUTPUT_DOCTYPES` → log INFO, return `[]`. Expected.
- No template registered → fall through to regex / LLM.
- Template registered but empty → log WARN, return `[]`, **skip** the
  regex/LLM fallback. With `strict=True` raise
  `TemplateExtractionError` instead.

The skip is deliberate. Falling through historically papered over
template regressions with `Equity:Uncategorized`-balanced placeholder
entries that landed silently in the user's ledger. The fix: surface
the empty result as a missing entry (so the next `bean-check` notices
the imbalance) or as an exception under `--strict`.

`banking-pipeline ingest --strict` and `banking-pipeline rebuild
--strict` both turn this on; `rebuild --strict` also escalates
`bean-check` warnings to errors regardless of `[post.check] strict`,
and escalates reconcile coverage gaps to a failed rebuild regardless
of `[post.reconcile] strict`.

## Beancount output conventions

- Account paths are `Assets:<prefix>:<portfolio>:<currency>` for cash
  legs and `Assets:<prefix>:<portfolio>:<ISIN>` for security holdings.
  `<prefix>` comes from `writer/profile.py` (Pictet → `Pic`). Vanguard's
  prefix is the **two-segment** `Vgd:ISA`, which is what puts every ISA
  account under the dedicated `…:Vgd:ISA:…` subtree — the prefix is a
  plain string, so a multi-segment value just works everywhere it's
  interpolated. Vanguard ISA holdings key on the fund **ticker**
  (`VMIG`, `VGVA`) as the commodity, not an ISIN (the buy contract notes
  print no ISIN); contributions post against `Equity:Vgd:ISA:Contributions`
  and the account fee against `Expenses:Vgd:ISA:Fees`.
- Portfolio identifiers are sanitised through `portfolio_segment` —
  Pictet prints `K-123456.001`; beancount segments don't allow `-` or
  `.`, so it becomes `K123456001`.
- Per-currency rounding tolerances live in the hand-curated
  `main.beancount` at the repo root (`inferred_tolerance_default
  "<ccy>:0.005"` for cent-precision fiat, `JPY:0.5` for yen). ISIN
  commodities deliberately don't set a default — beancount's
  inferred-from-decimals picks the right value per fund.
- `data/portfolio.beancount` is **generated** (by `portfolio_aggregate`):
  it owns `option "operating_currency"`, the booking method, the
  central account opens, and the `close` directives. Don't hand-edit it.
  Hand-curated overrides go in `main.beancount`, which `include`s the
  aggregate.
  - **Close directives are aggregate-only.** The per-source `ingest` /
    `rebuild` output carries *no* closes — a per-batch close can't see a
    *later* source re-acquiring a wound-down holding, and beancount can't
    reopen a closed account (re-buy → "Invalid reference to inactive
    account"). The aggregate reads every source file, sums each ISIN
    asset account across the full history, and closes only those that net
    to exactly zero (dated the day after the account's last posting,
    which is by construction after any re-buy). Don't re-add a close call
    to `ingest`; it reuses `writer.render_close_directives` over the
    combined source text.
  - `main.beancount` (the `bean-check` root) must itself declare
    `option "booking_method" "FIFO"` and `operating_currency`:
    beancount reads those options **only from the root file**, not
    from an included one, so the copies in `portfolio.beancount` only
    take effect when it's loaded directly (e.g. in Fava). Omitting
    `booking_method` from the root drops booking to `STRICT`, and every
    `{}` switch-out that matches more than one lot then fails
    bean-check with "Ambiguous matches".
- `portfolio_aggregate` only treats flat per-year ingest files as
  sources: it skips any `*.beancount` that itself contains an
  `include` (a stale or per-account aggregate it once wrote), the aux
  files (`prices`/`balances`, which it includes but doesn't source), and
  `property.beancount` (the `_IGNORED_FILENAMES` set — it's included
  directly by `main.beancount` and owns its own commodity/open
  directives, so sourcing it would double-count holdings and re-including
  it would double-include the file). It also only constrains a `…:<CCY>`
  account leaf to a currency when that token actually appears as a
  posting currency (so `…:Earnout:IBM` isn't mistaken for a currency).
  All guard against `bean-check` errors.
- `examples/accounts.beancount` is a starter chart of accounts for
  external readers — not loaded by the rebuild.

## UK tax reporting

The tax pipeline is deliberately separate from the beancount writer
and reads the JSONL sidecars, not the ledger:

- Every `ingest` / `rebuild` writes a `<stem>.transactions.jsonl`
  next to each `.beancount` (see `transaction_sidecar.py`). This
  is the **load-bearing** substrate `tax-report` consumes — never
  re-parse beancount text for tax math. `dump-transactions <pdf>`
  prints the same JSONL to stdout for ad-hoc inspection.
- **Tax-free wrappers (ISA):** every `Transaction` carries an optional
  `account_wrapper` (`"isa"` for the Vanguard ISA, set by the template);
  `Transaction.is_tax_exempt` is true when it's in `TAX_EXEMPT_WRAPPERS`.
  The `tax-report` CLI filters `[tx for tx … if not tx.is_tax_exempt]`
  immediately after loading the sidecars, **before** any `compute_*` /
  `match_history` call — a single choke point, so an ISA's disposals
  and income never reach SA108 / SA106 / the loss-carry-forward chain.
  Don't add per-report wrapper filters; keep the one choke point. (The
  sidecar schema is `…/v3` for this additive field; a v2 sidecar still
  loads, `account_wrapper` defaulting to `None`.)
- GBP cost basis is carried as posting **metadata** (`gbp-rate`,
  `trade-date`) and as the structured `Transaction.gbp_rate` in
  the sidecar. All section 104 / same-day / 30-day matching
  happens in `tax/uk/section_104.py` from the sidecar, not in
  beancount — beancount's booking methods do not implement UK
  matching rules and would compete with the correct answer. The
  rationale is documented at length in `docs/design-decisions.md`.
- Rate sources are pluggable via the `GbpRateSource` protocol in
  `fx/gbp_rates.py`. Today there are two: `HmrcMonthlyAverageSource`
  (user-maintained CSV at `data/fx/hmrc-monthly-average.csv`,
  columns `month` `YYYY-MM`, `currency`, `rate`) and `NullSource`
  (default — no rate, the rest of the pipeline behaves exactly as
  before). `BANKPIPE_GBP_RATE_SOURCE=hmrc-monthly` opts in.
- Rate-coverage check: when an amount can't be converted to GBP (no
  per-tx `gbp_rate` and no source rate) it's *excluded* from the
  figures, so it would silently understate. `to_gbp` returning `None`
  is recorded as a `RateGap(isin, currency, month)` (`tax/uk/currency.py`)
  on each report's `missing_rates`; `tax-report` and `tax-forecast`
  surface these as actionable warnings naming the exact HMRC CSV row to
  add, and `--strict` makes any gap a non-zero exit (CI gate).
- Reporting status (`reporting` / `non-reporting` / `uk-domestic` /
  `unknown`) lives in `data/commodities.toml` keyed by ISIN and is
  loaded by `commodities_metadata.py`. The `tax-report` command
  routes disposals to SA108 (CGT, for reporting / uk-domestic) vs.
  SA106 offshore income gains (non-reporting), and flags unknown
  status in `summary.txt` rather than guessing. Two further per-ISIN
  flags reroute income out of CGT/dividends: `deeply_discounted`
  (gain taxed as income) and `distributions_as_interest` (a >60%
  interest-bearing "bond fund" — its distributions and ERI are
  foreign interest, not dividends).
- Four user-maintained TOMLs feed `tax-report`, all gitignored with a
  committed `.example.toml`: `data/commodities.toml` (status / flags),
  `data/opening-positions.toml` (pre-ledger section-104 lots — seeds
  cost basis so a disposal isn't matched at zero cost; the summary
  warns "disposed more than acquired" when one is missing),
  `data/eri.toml` (excess reportable income; the displayed date is the
  fund *distribution* date and units are measured at the period end six
  months earlier — see `tax/uk/eri.py`), and `data/cgt-losses.toml`
  (a single `brought_forward_gbp` — pre-ledger allowable losses seeding
  the loss-carry-forward chain).
- CGT annual exempt amount + loss carry-forward live in
  `tax/uk/cgt_allowance.py`, layered on the section-104 gains. The chain
  runs the matcher over the full history (`sa108.match_history`), buckets
  disposals by tax year, and threads allowable losses forward to the
  requested year, applying HMRC's deduction order: current-year losses
  first (even if that wastes the AEA), then brought-forward losses *only
  down to the AEA*, then the AEA. In a mid-year rate-change year it
  absorbs relief against the higher-rate (`post`) bucket first so the
  taxable remainder sits in the lower-rate (`pre`) bucket. AEA values are
  `cgt_annual_exempt_amount` (statutory; a year missing there is treated
  as 0 and flagged in `summary.txt`). Losses are claimed automatically.
  The pool is tracked by arising year (FIFO); a brought-forward loss
  relieved more than 4 years after it arose is flagged
  (`WARN_LOSS_CLAIM_WINDOW` / `expired_loss_claims`) since it's allowable
  only if it was notified to HMRC within the statutory window — but the
  figures are *not* adjusted (a loss notified in-year carries forward
  indefinitely, and the tool can't tell). Pre-ledger losses aren't dated,
  so they aren't flagged.
- `tax-report --year 2025-26` writes, under `reports/uk-tax/<year>/`:
  `sa108-disposals.csv` (CGT; `period` splits gains pre / on-or-after
  the year's CGT rate-change date from `cgt_rate_change_dates`),
  `sa106-dividends.csv`, `sa106-interest.csv`,
  `sa106-offshore-income-gains.csv`, `sa106-deep-discounted.csv`,
  `sa106-eri.csv`, `cgt-loss-carryforward.csv` (the year-by-year AEA +
  allowable-loss chain), and `summary.txt` (which carries a "CGT
  allowances and loss relief" block: net gain, losses used, AEA, taxable
  gain split pre/post, and losses carried forward). Current-account
  interest is *not* foreign income — it posts to `Expenses` (loan
  interest the user pays), so it has no SA106 line.
- The model invariant `gross_income - withholding_tax == amount`
  (within a cent) is enforced in `Transaction`'s
  `@model_validator` — break it and `pydantic` raises at
  construction time. Don't paper over it; fix the template.

## CLI surface (run via `uv run banking-pipeline …`)

- `ingest` — classify + extract + render one or more PDFs; supports
  `--check <ledger>` to validate in the same step and `--strict`.
  Always writes a `<stem>.transactions.jsonl` sidecar next to the
  output `.beancount` (no flag needed; it's part of the output
  contract).
- `dump-transactions` — extract one or more PDFs and print the JSONL
  sidecar to stdout. Same structured form `ingest` writes, but
  without touching the ledger.
- `dedup-check` — read-only audit. Walks `*.transactions.jsonl`
  sidecars under a directory (default `data`), content-keys each
  transaction (date + signed amount + currency + ISIN + doctype +
  account, *not* the per-document ref), and reports groups that
  collide as suspected double-counts — `EXACT` (same ref → same
  document ingested twice) vs `POSSIBLE` (review). `--output` writes
  a CSV; exits nonzero when any duplicate is found.
- `classify` — just print the language/bank/doctype verdict.
- `scan` — walk a directory; one row per PDF; `--json` for JSONL.
- `extract-text` — dump PDF text; `--show-rules` shows which rules
  fired at each stage (the rule-authoring workhorse).
- `prices`, `balances`, `portfolio` — aggregators that read the
  ingest output under `data/`. `prices` / `balances` also parse
  statement PDFs passed via `--statement` / discovered by glob; both
  handle Pictet monthly statements and the Vanguard ISA regular
  statement (the parsers self-select on the document's text).
- `concentration` — portfolio concentration / exposure report. Reads the
  latest statement valuation per portfolio (`--statement` /
  `--statements-dir`, same discovery as `prices`), values holdings in GBP
  (FX via `--rate-source`), and writes `concentration.md` + `holdings.csv`
  to `<concentration_reports_dir>/` (default `reports/concentration/`):
  breakdowns by holding / asset class / currency / domicile. Weights are a
  share of **gross long holdings**; a negative cash balance (margin /
  Lombard loan) is netted by currency and reported separately, so a
  leveraged book doesn't read as >100%. Non-GBP holdings with no rate (or
  no statement mark) are excluded + flagged; `--strict` exits non-zero on
  any such gap. Domicile stands in for issuer (no issuer field on the
  metadata yet); Vanguard ISA tickers carry no `commodities.toml` entry so
  land in an `unknown` bucket.
- `net-worth` — net-worth-over-time. Same statement discovery /
  `--rate-source` as `concentration`, but values *every* statement at its
  own date and builds a combined timeline: at each statement date, each
  portfolio contributes its latest valuation on or before that date (as-of
  forward-fill), with same-date duplicate statements deduped per commodity
  so a holding isn't double-counted. Writes `net-worth.md` (gross long /
  net cash / net worth per date + Δ) + `net-worth.csv` to
  `<net_worth_reports_dir>/` (default `reports/net-worth/`). Reuses
  `concentration._value_holdings` for the per-snapshot valuation.
- `income` — income-by-source report. Reads the `*.transactions.jsonl`
  sidecars under `--source` (default `data`, same loader as `tax-report`),
  aggregates dividend + interest income by `--period` (`tax-year` default,
  or `calendar`) and paying source, converts to GBP (`--rate-source`), and
  writes `income.md` + `income.csv` to `<income_reports_dir>/` (default
  `reports/income/`). Income = `DIVIDEND_TYPES` advices (bond-fund
  distributions reclassified to interest via `distributions_as_interest`,
  as SA106 does) + credit-balance current-account interest + Vanguard ISA
  "Cash Account Interest". Unlike `tax-report` it does **not** apply the
  ISA tax-exempt filter — ISA income is real income, flagged in a wrapper
  column instead — and counts UK + foreign alike. An amount with no GBP
  rate is excluded + flagged; `--strict` exits non-zero on any such gap.
- `property` — generate the residential-property ledger from
  `data/property.toml`. Each property becomes a commodity held at cost
  (`1 <COMMODITY> {price ccy}`) revalued by `price` directives, funded
  against `Equity:Property:<label>` (beancount infers the equity leg). A
  non-GBP property also gets a GBP price mark (converted via
  `--rate-source`) so a GBP-operating-currency load values it. Writes
  `<property_ledger_path>` (`data/property.beancount`); `include` it from
  `main.beancount` to bring property onto bean-check / Fava. The
  `concentration` / `net-worth` reports auto-load `property.toml` (or
  `--property`) and fold property in (asset class `property`, domicile =
  country) so they total *all* wealth, not just the investment accounts.
  Funding note: the financing (e.g. the Lombard loan) already sits on the
  investment ledger, so adding the asset against equity raises net worth
  by the property value without double-counting.
- `check` — standalone `bean-check` wrapper.
- `reconcile` — statement-balance reconciliation. Runs `bean-check`
  over the ledger, parses its balance-assertion failures, and writes a
  full report to `<reconciliation_dir>/` (default
  `reports/reconciliation/`): `drift.csv` (every reconciled row) and
  `summary.txt` (drift rows with signed differences, the earliest date
  each account diverged, and coverage gaps — statement months with no
  assertion). Additive to `check`, not a replacement: it reports the
  whole grid and localises each divergence instead of aborting on the
  first failure. The drift verdict is `bean-check`'s own, so it agrees
  with a load by construction (no tolerance re-implementation). Exits
  nonzero on any drift; `--strict` also fails on coverage gaps.
  Defaults: `ledger=main.beancount`, `--balances=data/balances.beancount`.
- `rebuild` — end-to-end run driven by `banking-pipeline.toml`
  (gitignored; copy from `banking-pipeline.example.toml`). Owns the
  `clean → ingest per source → prices/portfolio/balances → reconcile
  → check` sequence. `[post.reconcile]` (off by default) runs *before*
  `check` so its drift report lands even though `bean-check` exits
  nonzero on the same drift.
- `revolut` — separate side path; Revolut Personal CSV → beancount.
- `tax-report` — read `*.transactions.jsonl` sidecars under
  `--source` (default `data`), apply UK tax-year bounds + section
  104 matching, and write the SA108 / SA106 CSVs plus the CGT
  AEA/loss-carry-forward chain (`cgt-loss-carryforward.csv`) to
  `<tax_reports_dir>/<year>/` (default `reports/uk-tax/<year>/`).
  `--rate-source` overrides `gbp_rate_source` for the run; `--strict`
  exits non-zero if any amount lacked a GBP rate. Residence-aware (see
  below): a pre-residence year is skipped; under a FIG claim the foreign
  items move off SA108/SA106 onto `fig-designation.csv`.
- `tax-forecast --income <gbp> [--year 2026-27]` — current-year
  liability estimate (defaults to the in-progress tax year). Reuses the
  `tax-report` machinery to compute year-to-date taxable amounts, then
  stacks them in UK order (non-savings income from `--income` + offshore
  income gains + deeply-discounted profit → savings/interest → dividends
  → CGT on the remaining basic-rate band), applies the statutory
  rates/bands (`income_tax_bands` / `cgt_forecast_rates`), nets foreign
  tax credit relief on WHT, and writes `forecast-summary.txt` +
  `forecast.csv`. Year-to-date *actuals* only (no run-rate
  extrapolation); ISA-wrapped transactions are excluded at the same
  choke point as `tax-report`. England/Wales/NI rates, single taxpayer.
  When the year is FIG-eligible it computes the liability with and
  without the claim and recommends the cheaper (the PA/AEA forfeiture
  often outweighs the relief for small foreign amounts). Surfaces missing
  GBP-rate gaps (which silently understate the estimate); `--strict`
  exits non-zero on any gap.
- `tax-pack [--year 2025-26]` — renders `tax-pack.md`, a single per-year
  filing aid that ties the computed SA108/SA106 figures to the boxes on
  the HMRC forms (CGT listed-shares boxes + allowance computation,
  foreign dividends/interest with FTCR, offshore income gains,
  deeply-discounted, ERI, and the FIG designation under a claim).
  Recomputes from the sidecars on the same residence/FIG path as
  `tax-report` (pure renderer in `tax/uk/tax_pack.py`). A filing aid, not
  tax advice; box numbers are indicative and caveated in the output. Same
  `--source/--out/--commodities/--rate-source/--opening-positions/--eri`
  options.
- `fig-advice --income <gbp>` — recommends *which* FIG years to claim,
  optimised jointly across the eligible window rather than per year.
  Because claiming forfeits a year's allowable foreign losses (not just
  its PA/AEA), and those losses would otherwise carry forward, the
  cheapest claim set isn't the per-year answer. Brute-forces the `2^k`
  claim subsets (k ≤ 4), threading the loss chain per subset, and writes
  `fig-advice.txt` (ranked patterns + recommendation + per-year
  breakdown). Year-to-date actuals, same income assumed each year; later
  eligible years with no configured rates are omitted with a note. Core
  in `tax/uk/fig_advice.py`. A planning aid, not tax advice.

## UK residence and the FIG regime

The tax pipeline assumes UK arising-basis residence across the whole
history unless `uk_residence_start_date` is set. `tax/uk/residence.py`
applies two corrections, both config-driven and both leaving the section
104 pool untouched (acquisitions feed it whenever they happened — only
the taxable *output* is residence-filtered):

- **Pre-residence (split-year):** income/gains arising before the arrival
  date drop out (non-resident / overseas part of a split year); whole tax
  years before arrival are skipped entirely. SA106 and SA108 take an
  `arrival` parameter for the in-year split; the loss chain starts at the
  residence-start year.
- **4-year FIG claim (`fig_claim_years`, from 2025-26):** for an eligible
  year, foreign income and non-UK gains are relieved to nil but the
  personal allowance and CGT AEA are forfeited. Foreign-vs-UK situs is
  `CommodityMetadata.resolved_uk_situs` (the optional `uk_situs` flag,
  else derived from domicile / `uk-domestic` status). The chain relieves
  foreign gains + zeroes the AEA; the liability engine zeroes the PA +
  drops foreign income; the CLI partitions foreign items onto
  `fig-designation.csv`.

Out of scope (documented simplifications): the 10-prior-non-resident
eligibility test, whole-arrival-year ERI attribution, temporary-non-
residence clawback, and former-remittance-basis rebasing/TRF — the full
list and the rationale live in `docs/design-decisions.md`. Not tax
advice — verify against HMRC guidance.

## Configuration

- Runtime config: `Settings` in `config.py`. Three sources, highest
  precedence first: `BANKPIPE_`-prefixed env vars → `.env` → the
  **`[settings]` table of `banking-pipeline.toml`** → field defaults
  (see `settings_customise_sources`). The TOML table is the home for
  structured / personal config (keyed maps, the residence/FIG knobs) —
  readable TOML instead of JSON-in-env; env still overrides it, and
  secrets (`anthropic_api_key`) stay env-only. The same
  `banking-pipeline.toml` carries the `rebuild` BatchConfig in its other
  tables; `load_config` drops `[settings]` and `Settings` reads only it,
  so the two schemas don't collide. Notable knobs: `anthropic_api_key`,
  `anthropic_model`, `rule_confidence_threshold`, `default_currency`,
  plus two dict maps driving payment-routing — `beneficiary_bank_map`
  (self-to-self destinations like Revolut) and `counterparty_account_map`
  (third-party named counterparties → account segments), both set under
  `[settings.<map>]`.
- UK-tax knobs (all optional, default to the no-op behaviour):
  `gbp_rate_source` (`"null"` | `"hmrc-monthly"`), `hmrc_rate_path`
  (defaults to `data/fx/hmrc-monthly-average.csv`),
  `commodities_metadata_path` (defaults to `data/commodities.toml`
  when present), `opening_positions_path` (`data/opening-positions.toml`),
  `eri_path` (`data/eri.toml`), `tax_reports_dir` (defaults to
  `reports/uk-tax`), `cgt_rate_change_dates` (`{label: date}`,
  default `{"2024-25": 2024-10-30}` — the mid-year CGT rate change),
  `cgt_annual_exempt_amount` (`{label: Decimal}`, statutory AEA per tax
  year), and `cgt_losses_path` (`data/cgt-losses.toml` when present —
  pre-ledger brought-forward losses).
- `tax-forecast` knobs: `income_tax_bands` (`{label: IncomeTaxBands}`)
  and `cgt_forecast_rates` (`{label: CgtRateSchedule}`) — statutory
  England/Wales/NI defaults from `tax/uk/rates.py`, frozen across
  2024-25..2026-27 (CGT split 10/20 → 18/24 on 30 Oct 2024). A
  forecast year missing from `income_tax_bands` aborts with a clear
  error rather than guessing; add new years to `rates.py` as HMRC sets
  them.
- Residence / FIG knobs: `uk_residence_start_date` (`date | None`,
  default `None` = resident throughout — the split-year arrival date) and
  `fig_claim_years` (`frozenset[str]`, default empty — the years a FIG
  claim is applied). A per-ISIN `uk_situs` override lives in
  `data/commodities.toml` (else situs is derived). See the UK residence
  section above.
- `reconciliation_dir` (defaults to `reports/reconciliation`) — output
  directory for the `reconcile` command's `summary.txt` / `drift.csv`.
- `concentration_reports_dir` (defaults to `reports/concentration`) —
  output directory for the `concentration` command's `concentration.md` /
  `holdings.csv`.
- `net_worth_reports_dir` (defaults to `reports/net-worth`) — output
  directory for the `net-worth` command's `net-worth.md` / `net-worth.csv`.
- `income_reports_dir` (defaults to `reports/income`) — output directory
  for the `income` command's `income.md` / `income.csv`.
- `property_path` (defaults to `data/property.toml` when present) — the
  off-ledger residential-property table; `property_ledger_path` (defaults
  to `data/property.beancount`) is the generated ledger the `property`
  command writes. Both consumed by `property` and the valuation reports.
- Batch config: `banking-pipeline.toml` (gitignored, schema in
  `batch_config.py`). Carries personal Dropbox/iCloud paths, plus
  `[post.reconcile]` (off by default) and `[post.check]` toggles. Its
  `[settings]` table feeds `Settings` (above); the rest is BatchConfig.
- `.env.example` / `banking-pipeline.example.toml` are the committed
  templates; copy to `.env` / `banking-pipeline.toml` for local work.

## When working in this repo

- Prefer editing rules / fixtures / templates over editing the
  pipeline core — the architecture is intentionally
  data-driven, and most "the classifier got it wrong" or "the
  extraction missed a field" issues resolve there.
- Read the relevant `DocumentType` enum docstring before writing a
  new template — it usually documents the exact PDF-text markers
  that distinguish the doctype from its near-neighbours.
- If you find yourself reaching for `Equity:Uncategorized` in
  generated output, stop — that's the deliberate placeholder that
  the strict-mode dispatch was built to eliminate. Find the right
  account in `writer/format.py` and the per-shape builder, or extend
  `Transaction` with a new field and a builder branch.
- Don't run the LLM fallback paths in tests — they require
  `BANKPIPE_ANTHROPIC_API_KEY` and are non-deterministic. The
  rule/template paths are the test surface.

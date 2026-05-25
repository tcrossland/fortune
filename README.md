# banking-pipeline

Ingest banking PDFs (account statements, trade confirmations, dividend
notices, fee invoices, FX advices, wire confirmations, etc.), classify them
in three layered stages, extract the fields that matter for accounting
(trade date, currency, amount, ISIN, account number), and emit
[beancount](https://beancount.github.io/) entries.

## Design

```
PDF ──► extractors/pdf_text.py ──► RawDocument
                                      │
                                      ▼
                    classifiers/language.py (en | es | …)
                                      │
                                      ▼
                    classifiers/bank.py     (pictet | …)
                                      │
                                      ▼
                    classifiers/rules.py    (doctype, per-bank ruleset)
                                      │
                                      ▼
                    fields/hybrid.py ──► [Transaction, …]
                                      │
                                      ▼
                    beancount_writer.py ──► text
```

Each stage is a module with a narrow interface (see `models.py`).
Classification is **layered**: the detected language narrows the vocabulary,
the detected bank narrows the ruleset, then the doctype rules fire against
the document's text. Every stage is **hybrid**: deterministic rules run
first, and the Claude LLM fallback only kicks in when rule confidence is
below `rule_confidence_threshold`. Classifiers in `classifiers/hybrid.py`
expose three facades — `HybridClassifier` (single-stage rules+LLM),
`TwoStageClassifier` (bank → doctype), and `LayeredClassifier` (the
three-stage default wired into `Pipeline`).

Rules themselves live in `classifiers/rules.py` as `PICTET_EN_RULES`,
`PICTET_ES_RULES`, and `GENERIC_RULES`. Each `Rule` is a bag of compiled
regexes; a document scores `weight * hits / len(patterns)` against each
rule, and the first rule to reach the highest score wins.

## Libraries and licenses

This project is released under the **MIT License** (see [`LICENSE`](LICENSE)).

Every runtime dependency is MIT, BSD, or Apache-2.0 except `python-stdnum`
(LGPL-2.1+, which is fine for dynamic linking like we do here). MIT is
compatible with all of these — including `python-stdnum`'s LGPL, since we
link it dynamically.

| Area | Library | License | Why |
|---|---|---|---|
| PDF → text | `pypdfium2` | Apache-2.0 / BSD-3-Clause | Google's PDFium bindings; fast, permissive |
| PDF layout / tables | `pdfplumber` | MIT | Better for tabular statements |
| Models | `pydantic` | MIT | Runtime-validated typed domain objects |
| Config | `pydantic-settings` | MIT | `.env` / env-var config |
| Date parsing | `dateparser` | BSD-3-Clause | Handles multi-locale bank date formats |
| ISIN / IBAN | `python-stdnum` | LGPL-2.1+ | Validated normalisation of identifiers |
| Number/currency | `babel` | BSD-3-Clause | Locale-aware number parsing |
| LLM fallback | `anthropic` | MIT | Official Claude SDK, supports tool use for structured output |
| CLI | `typer` | MIT | Type-hint driven, Click-backed |
| Pretty output | `rich` | MIT | Tables, colour, tracebacks |
| Logging | `structlog` | Apache-2.0 / MIT | Structured logs for pipeline stages |

### Why not PyMuPDF?

`PyMuPDF` (`pymupdf`) is the most popular MuPDF binding, but it is
**AGPL-3.0** — viral copyleft — unless you buy a commercial licence from
Artifex. For a permissive project, `pypdfium2` is the best drop-in
replacement: similar speed, Apache-2.0/BSD-3-Clause, maintained. If you
hit a PDF that PDFium chokes on, `pdfplumber` (MIT, built on
`pdfminer.six`) is a good second backend.

### Why not import `beancount`?

`beancount` itself is **GPL-2.0**. We avoid linking against it and instead
emit beancount plain text directly. If you want to validate the output,
shell out to the `bean-check` CLI as a separate process — that's a normal
program invocation, not library linking, and doesn't bind this codebase
to GPL.

### Optional extras

`uv sync --extra ocr` installs `pytesseract` + `ocrmypdf` for scanned
PDFs. Tesseract itself must be installed separately (Apache-2.0).

## Supported documents

The classifier is driven by fixtures under `tests/fixtures/<lang>/<bank>/`
where the filename stem matches a `DocumentType` enum value. Today the
ruleset covers Pictet's Luxembourg and Madrid templates in English and
Spanish — 29 document types and growing. Trade confirmations
(`subscription_notice`, `redemption_notice`, `compra`, `suscripcion`,
`reembolso`, `switch_salida`/`switch_entrada`, `buy_structured_products`,
`spot`), security events (`dividend_notice`, `final_redemption`), FX
(`fx_forward`, `settle_fx_forward`), cash movements (`payment`,
`incoming_payment`, `internal_transfer`), fees (`debit_of_fees`,
`factura`, `debito_de_gastos`), interest (`interest_payment`,
`interest_scale`), credit (`limit_extension`), order reporting
(`order_information_report`), and the periodic portfolio statements
(`monthly_statement`, `quarterly_statement`, `annual_statement`,
`estado_mensual`, `estado_trimestral`, `estado_anual`). See
`DocumentType` in `src/banking_pipeline/models.py` for the canonical list.

Adding support for a new bank is a matter of dropping fixtures under
`tests/fixtures/<lang>/<new_bank>/`, adding a `BankId` enum value, a
bank-detection rule in `classifiers/bank.py`, and a per-doctype ruleset
in `classifiers/rules.py` registered under `RULESETS_BY_BANK`.

## Quickstart

```bash
# Install
uv sync                    # runtime + dev deps
uv sync --extra ocr        # add OCR support for scanned PDFs

# Configure (optional — only needed for the LLM fallback)
cp .env.example .env
# then set BANKPIPE_ANTHROPIC_API_KEY

# Classify a single PDF (prints language / bank / doc-type + confidences)
uv run banking-pipeline classify path/to/statement.pdf

# Walk a folder and print one row per PDF
uv run banking-pipeline scan path/to/inbox/              # top-level only
uv run banking-pipeline scan -r path/to/inbox/           # recursive
uv run banking-pipeline scan -r path/to/inbox/ --json    # JSONL output
uv run banking-pipeline scan -r path/to/inbox/ --json -o results.jsonl

# End-to-end: classify, extract transactions, emit beancount
uv run banking-pipeline ingest path/to/statement.pdf --output out.beancount

# Validate the new entries against your ledger in the same step
uv run banking-pipeline ingest statement.pdf -o out.beancount --check ledger.beancount

# Or check an existing ledger ad-hoc
uv run banking-pipeline check examples/accounts.beancount
```

## Batch rebuild

The full year-by-year rebuild that used to live in `run.sh` is now
config-driven. Copy the example, edit your local paths, then run the
single rebuild command:

```bash
cp banking-pipeline.example.toml banking-pipeline.toml
$EDITOR banking-pipeline.toml          # change the Dropbox paths to yours

uv run banking-pipeline rebuild --dry-run   # preview what each step would do
uv run banking-pipeline rebuild             # actually rebuild
```

`banking-pipeline.toml` is gitignored — it carries personal Dropbox /
iCloud paths that shouldn't land in the repo. The schema lives in
`src/banking_pipeline/batch_config.py`: `data_dir`, a list of
`[[sources]]` (each `label` becomes `<data_dir>/<label>.beancount`),
and a `[post]` block toggling the `prices` / `portfolio` / `balances` /
`reconcile` / `check` post-processing steps.

`[post.reconcile]` (off by default) runs the reconciliation step just
before `check`: because `bean-check` exits nonzero on a drifted
assertion, reconcile goes first so its localised drift report under
`reports/reconciliation/` is always produced. Drift fails the rebuild;
`strict = true` also fails it on coverage gaps. Enable it alongside
`[post] balances = true` so there's a `balances.beancount` to compare
against.

## Output: beancount + structured sidecar

Every generated `.beancount` file is accompanied by a
`<stem>.transactions.jsonl` sidecar holding the raw extracted
`Transaction` objects (one JSON object per line, after a `_schema`
header line). The rendered beancount encodes much of the
UK-tax-relevant data — GBP rate, withholding tax, accrued interest —
into free-text postings and metadata; the sidecar preserves the
structured form so downstream tooling (the UK tax-report stage) can
consume it without re-parsing beancount text. `ingest` and `rebuild`
write sidecars automatically; `banking-pipeline dump-transactions
<pdf>` prints the same JSONL to stdout for ad-hoc inspection.

## UK tax reporting

`banking-pipeline tax-report --year 2025-26` reads the JSONL sidecars
(never the beancount text — so it stays clear of the GPL constraint),
applies UK tax-year boundaries and the section 104 / same-day / 30-day
share-matching rules, and writes CSV inputs for the self-assessment
forms:

```bash
uv run banking-pipeline tax-report --year 2025-26 \
    --source data --out reports/uk-tax/2025-26
```

Outputs (all GBP):

- `sa108-disposals.csv` — capital-gains disposals for reporting-status
  and UK-domestic securities: `disposal_date`, `isin`,
  `commodity_name`, `reporting_status`, `quantity`, `proceeds_gbp`,
  `cost_gbp`, `gain_gbp`, `match_type`
  (`same-day` / `bed-and-breakfast` / `s104`), `acquisition_dates`.
- `sa106-dividends.csv` — foreign dividends grouped by source country
  and ISIN: `country`, `isin`, `commodity_name`, `gross_gbp`,
  `wht_gbp`, `net_gbp`, `document_count`.
- `sa106-interest.csv` — foreign interest, same columns as the
  dividends CSV. Holds distributions from offshore funds flagged
  `distributions_as_interest` in `data/commodities.toml` (the UK
  ">60% interest-bearing" bond-fund rule), which are taxed as interest
  rather than dividends.
- `sa106-offshore-income-gains.csv` — disposals of non-reporting funds
  (taxed as income, not CGT): `disposal_date`, `isin`,
  `commodity_name`, `quantity`, `proceeds_gbp`, `cost_gbp`, `gain_gbp`,
  `match_type`, `acquisition_dates`.
- `sa106-deep-discounted.csv` — disposals of securities flagged
  `deeply_discounted` in `data/commodities.toml` (gain taxed as income,
  loss generally not allowable).
- `sa106-eri.csv` — excess reportable income for accumulating reporting
  funds (from `data/eri.toml`), split dividend / interest:
  `taxable_income_gbp`, `equalisation_gbp`, `base_cost_adjustment_gbp`.
  The base-cost adjustment also uplifts the section 104 pool, so a later
  disposal isn't taxed again on income already charged.
- `summary.txt` — totals plus warnings for anything not on a CSV.

For `sa108-disposals.csv`, the `period` column splits a year's gains
before / on-or-after its CGT rate-change date (e.g. 30 Oct 2024 for
2024-25), and `acquisition_dates` carries the section 104 pool's
earliest acquisition ("acquired since") for pooled disposals.

Three optional user-maintained TOMLs refine the figures, each gitignored
with a committed `.example.toml`: `data/commodities.toml` (reporting
status and the `deeply_discounted` / `distributions_as_interest` flags),
`data/opening-positions.toml` (pre-ledger section 104 cost basis — pass
`--opening-positions`), and `data/eri.toml` (excess reportable income —
pass `--eri`). Current-account interest is *not* foreign income: it's
loan interest the user pays, booked to `Expenses`.

### GBP rates

GBP figures use each transaction's trade-date `gbp_rate` stamped
during `ingest` (when `BANKPIPE_GBP_RATE_SOURCE` is set), with
`--rate-source hmrc-monthly` available as a fallback for older
sidecars whose `gbp_rate` is unset.

The HMRC monthly-average source reads a user-maintained CSV at
`data/fx/hmrc-monthly-average.csv` (override with
`BANKPIPE_HMRC_RATE_PATH`). Columns: `month` (`YYYY-MM`),
`currency` (ISO-4217), `rate` (GBP per 1 unit of `currency`).
HMRC publishes the rates in their "Exchange rates from HMRC in
CSV and XML format" tables on GOV.UK; populate the CSV from
whichever months and currencies you trade in. A missing month or
currency yields `None` rather than failing — the missing ISINs
are flagged in `summary.txt`. Per-date / daily rates can be
plugged in by adding a new implementation of the
`GbpRateSource` protocol in `banking_pipeline.fx.gbp_rates`;
no daily source ships today.

### Commodity metadata (`data/commodities.toml`)

`tax-report` needs to know each ISIN's reporting status to route
disposals correctly. The hand-curated `data/commodities.toml` is
the source — one section per ISIN with at least `name` and
`reporting_status` (`reporting` / `non-reporting` / `uk-domestic` /
`unknown`); `domicile` (ISO 3166-1 alpha-2) overrides the ISIN
prefix as the withholding-tax country. Two optional booleans reroute
income: `deeply_discounted` (gain taxed as income) and
`distributions_as_interest` (a >60% interest-bearing "bond fund" — its
distributions and ERI are foreign interest, not dividends). See
`data/commodities.example.toml` for the schema. `portfolio
--list-missing-metadata` prints every in-use ISIN not yet in the
file, which is the loop for keeping it in sync.

**Notes / limitations:** unclassified holdings (no commodity metadata)
are flagged in `summary.txt` rather than guessed. Cost basis falls back
to zero (and the summary warns "disposed more than acquired") when a
disposal pre-dates the ledger and no `data/opening-positions.toml` lot
covers it. The annual exempt amount, rate arithmetic, and FIG-regime
relief are left to the user / their software — `tax-report` produces the
figures, not the final tax computation.

## Validation

The pipeline ships with a `bean-check` integration so writer
regressions and balance drift surface inside the rebuild instead of
lurking until the next ledger load. The validator runs as the final
post-step of `rebuild` (gated on `[post.check]`); it can also be
invoked standalone via `banking-pipeline check <ledger>`, or piggy-backed
on a single-PDF `ingest` via `--check <ledger>`. All three exit with
`bean-check`'s own return code so cron / CI can branch on success.

`bean-check` itself comes from the `beancount` package (GPL-2.0). We
shell out rather than import — install with `uv tool install beancount`.
A missing binary degrades to a warning, not a failure; set
`[post.check] enabled = false` to skip the step entirely.

### Reconciliation

`bean-check` enforces the balance assertions extracted from monthly
statements (`data/balances.beancount`), but it aborts on the first
failure and can't tell you about a statement you never ingested.
`banking-pipeline reconcile` is the friendlier, additive view:

```bash
uv run banking-pipeline reconcile               # main.beancount vs data/balances.beancount
uv run banking-pipeline reconcile main.beancount -b data/balances.beancount -o reports/reconciliation
```

It runs `bean-check` once, parses every balance-assertion failure, and
writes two files under `reports/reconciliation/` (override with
`--output` or the `reconciliation_dir` setting):

- `summary.txt` — drifted assertions with expected / actual / signed
  difference, the **earliest** date each account diverged (so a missed
  or misclassified document is localised to one statement month), and
  **coverage gaps** (statement months with no assertion at all).
- `drift.csv` — every reconciled row, machine-readable.

Because the drift verdict is `bean-check`'s own, `reconcile` agrees
with a ledger load by construction — no separate tolerance to keep in
sync. It exits nonzero on any drift (CI-friendly, like `check`);
`--strict` also fails on coverage gaps.

### Duplicate audit

Where `reconcile` catches *missing* or drifted entries, `dedup-check`
catches the opposite — the same economic event counted twice (the same
advice PDF matched by two source globs, a file copied into two year
folders, a re-issued document). It's read-only and never touches the
ledger:

```bash
uv run banking-pipeline dedup-check               # audit data/
uv run banking-pipeline dedup-check data -o reports/duplicates.csv
```

It walks the `*.transactions.jsonl` sidecars, assigns each transaction
a **content key** (trade date + signed amount + currency + ISIN +
doctype + account — deliberately *not* the per-document reference, so
the same event from two documents collides), and reports each group
sharing a key:

- **EXACT** — the members share one document reference, i.e. the same
  advice was ingested twice. Near-certain duplicate.
- **POSSIBLE** — same content, different/absent references. Could be a
  genuine pair of identical events (two equal dividends same day) —
  review these.

Each sidecar line also carries the `dedup_key` so external tooling can
group without re-deriving it. `dedup-check` exits nonzero when any
duplicate is found.

## Authoring classifier rules

To see what text the classifier is working from, dump it:

```bash
# Print to stdout with page markers
uv run banking-pipeline extract-text some_statement.pdf

# Raw text (no separators), pipe to grep while prototyping regexes
uv run banking-pipeline extract-text some_statement.pdf --raw | grep -i -E "isin|trade date"

# Write one .txt next to each input PDF for a whole folder
uv run banking-pipeline extract-text inbox/*.pdf -o extracted/

# See which existing rules matched (helps spot false positives / gaps)
uv run banking-pipeline extract-text some_statement.pdf --show-rules
```

Typical loop: run `extract-text --show-rules`, find a phrase distinctive
to the document type you care about, add a `Rule` in
`src/banking_pipeline/classifiers/rules.py`, drop a short text fixture
under `tests/fixtures/<lang>/<bank>/<doctype>.txt`, and rerun. The
parametric tests in `tests/test_pictet_fixtures.py` and
`tests/test_language.py` will automatically pick up the new fixture and
assert it clears the confidence threshold.

## Adding a new bank template

1. Drop a new module in `src/banking_pipeline/templates/`, e.g.
   `ibkr_trade_confirmation.py`.
2. Expose a class with a `template_id` str and an
   `extract(doc) -> list[Transaction]` method.
3. Register it in `TEMPLATE_REGISTRY` in that package's `__init__.py`.
4. Add a matching `Rule` in `classifiers/rules.py` so the rule-based
   classifier can route documents to it, and add the bank to `BankId`
   plus a bank-detection rule in `classifiers/bank.py`.
5. Drop text fixtures under `tests/fixtures/<lang>/<bank>/<doctype>.txt`.

## Tests

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Parametric suites discover every fixture under `tests/fixtures/` and
assert language + bank + doctype all clear the confidence threshold —
so adding a fixture automatically extends test coverage, and a rule
regression surfaces as a failing parametrisation against the exact
fixture it broke on.

## Project layout

```
src/banking_pipeline/
├── cli.py              Typer entrypoint (classify | scan | ingest |
│                         dump-transactions | dedup-check | extract-text |
│                         revolut | prices | balances | portfolio | check |
│                         reconcile | rebuild | tax-report)
├── config.py           Pydantic settings
├── models.py           Domain models (RawDocument, Transaction, DocumentType, BankId, Language)
├── pipeline.py         Top-level orchestration
├── beancount_writer.py Back-compat re-export of writer/
├── balances_extract.py Pictet monthly-statement → balance assertions
├── prices_extract.py   Per-trade + monthly-statement → price directives
├── portfolio_aggregate.py Central account opens + per-year includes
├── commodities_metadata.py  TOML loader for data/commodities.toml
├── transaction_sidecar.py   JSONL *.transactions.jsonl reader/writer
├── reconcile.py        Statement-balance reconciliation (drift report)
├── dedup.py            Duplicate-transaction audit (dedup-check)
├── extractors/
│   └── pdf_text.py     pypdfium2-based PDF → text
├── classifiers/
│   ├── language.py     Stopword-frequency language detection (en | es)
│   ├── bank.py         Hit-count-saturating bank identification
│   ├── rules.py        Per-bank, per-language doctype rule engine
│   ├── llm.py          Claude-based fallback classifier
│   └── hybrid.py       HybridClassifier / TwoStageClassifier / LayeredClassifier
├── fields/
│   ├── regex_extract.py  Generic regex field extraction
│   ├── llm_extract.py    Claude tool-use structured extraction
│   ├── validators.py     ISIN / IBAN via python-stdnum
│   └── hybrid.py         Template → regex → LLM + GBP-rate enrichment
├── fx/
│   └── gbp_rates.py    GbpRateSource protocol + HMRC monthly source
├── tax/
│   └── uk/             SA108 / SA106 builders, section 104 matcher,
│                         tax-year helpers, GBP conversion
├── writer/             Doctype → builder routing, per-shape builders
├── templates/          Per-bank extractors (add your own)
└── revolut/            Revolut CSV side path

tests/fixtures/<lang>/<bank>/<doctype>.txt
                     ^      ^       ^
                     |      |       └── matches DocumentType enum value
                     |      └────────── matches BankId enum value
                     └───────────────── matches Language enum value (ISO 639-1)
```

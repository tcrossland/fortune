# banking-pipeline

A single-user **UK wealth & tax toolkit** built on a banking-document
ingestion pipeline. The front end ingests banking PDFs (account statements,
trade confirmations, dividend notices, fee invoices, FX advices, wire
confirmations, etc.), classifies them in three layered stages, extracts the
accounting-relevant fields (trade date, currency, amount, ISIN, account
number), and emits [beancount](https://beancount.github.io/) entries plus a
structured JSONL sidecar. On top of that sidecar substrate it then produces:

- **UK self-assessment tax reports** — SA108 / SA106, section 104 matching,
  excess reportable income, the 4-year Foreign Income & Gains regime;
- **tax forecasting & planning** — `tax-forecast`, `fig-advice`,
  `fig-projection` (crystallise-vs-defer across the FIG window);
- **wealth / valuation reports** — net worth over time, holdings cost basis &
  unrealised P&L, allocation, an interactive balance sheet, mandate returns;
- **completeness & reconciliation** checks against the ledger and the bank's
  own exports.

Single-user, built around Pictet's Luxembourg and Madrid templates (English
and Spanish) plus a Vanguard UK Stocks & Shares ISA. Adding a new bank is a
data-only change. (The CLI and Python package are named `banking-pipeline`
after that ingestion front end; the repository is `fortune`.)

## How it works

```
import ──► dated archive ──► PDF ──► extract ──► classify(lang → bank → doctype)
                                       ──► fields ──► writer ──► beancount (+ jsonl sidecar)
```

Upstream of the per-document flow, the `import` command files raw downloads
(a folder or the bank's `.zip`) into a dated `<year>/<account>/` archive that
the later stages read.

Classification is **layered** — the detected language narrows the
vocabulary, the detected bank narrows the ruleset, then the doctype rules
fire against the document's text. Every stage is **hybrid**: deterministic
rules run first, and the Claude LLM fallback only kicks in when rule
confidence is below `rule_confidence_threshold` (and is skipped entirely
when no API key is set). The UK-tax stage is separate and reads the JSONL
sidecars, never the ledger.

For the module map, per-command reference, configuration, and the recipe for
adding a bank, see [docs/architecture.md](docs/architecture.md).

## Libraries and licenses

This project is released under the **MIT License** (see [`LICENSE`](LICENSE)).

Every runtime dependency is MIT, BSD, or Apache-2.0 except `python-stdnum`
(LGPL-2.1+, which is fine for the dynamic linking we do here).

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

Two deliberate exclusions keep the licence posture permissive: **PyMuPDF**
(AGPL-3.0) is avoided in favour of `pypdfium2`, and `beancount` itself
(GPL-2.0) is never imported — the pipeline emits beancount plain text and
shells out to the `bean-check` binary to validate it. The full reasoning is
in [docs/design-decisions.md](docs/design-decisions.md) §"Licence hygiene".

**Optional extras:** `uv sync --extra ocr` installs `pytesseract` +
`ocrmypdf` for scanned PDFs. Tesseract itself must be installed separately
(Apache-2.0).

## Supported documents

The classifier is driven by fixtures under `tests/fixtures/<lang>/<bank>/`
where the filename stem matches a `DocumentType` enum value. Today the
ruleset covers Pictet's Luxembourg and Madrid templates in English and
Spanish — 29 Pictet document types and growing: trade confirmations
(`subscription_notice`, `redemption_notice`, `compra`, `suscripcion`,
`reembolso`, `switch_salida`/`switch_entrada`, `buy_structured_products`,
`spot`), security events (`dividend_notice`, `final_redemption`), FX
(`fx_forward`, `settle_fx_forward`), cash movements (`payment`,
`incoming_payment`, `internal_transfer`), fees (`debit_of_fees`, `factura`,
`debito_de_gastos`), interest (`interest_payment`, `interest_scale`), credit
(`limit_extension`), order reporting (`order_information_report`), and the
periodic portfolio statements (`monthly_statement`, `quarterly_statement`,
`annual_statement`, `estado_mensual`, `estado_trimestral`, `estado_anual`),
plus the Vanguard UK ISA contract notes and statements. See `DocumentType`
in `src/banking_pipeline/models.py` for the canonical list, and
[docs/architecture.md](docs/architecture.md#adding-a-new-bank) for how to add
a bank.

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

# First stage: file fresh downloads (a folder or the bank's .zip) into a
# dated archive tree (<dest>/<year>/<account>/<YYYYMMDD>-<reference>.pdf)
uv run banking-pipeline import path/to/inbox/ path/to/archive/ --dry-run  # preview
uv run banking-pipeline import path/to/inbox/ path/to/archive/            # move
uv run banking-pipeline import ~/Downloads/files-20260528.zip path/to/archive/
# Or configure import_source_glob = "~/Downloads/files-*.zip" (+ archive
# dir) in banking-pipeline.toml and just run: banking-pipeline import

# Trim the archived Pictet Realised/Unrealised P&L tax reports to policy
# (month-end + year-end / 5-Apr anchors); dry-run by default
uv run banking-pipeline prune-tax-reports path/to/archive/            # preview
uv run banking-pipeline prune-tax-reports path/to/archive/ --apply    # move

# End-to-end: classify, extract transactions, emit beancount
uv run banking-pipeline ingest path/to/statement.pdf --output out.beancount

# Validate the new entries against your ledger in the same step
uv run banking-pipeline ingest statement.pdf -o out.beancount --check ledger.beancount

# Or check an existing ledger ad-hoc
uv run banking-pipeline check examples/accounts.beancount
```

The full command list and per-command behaviour is in
[docs/architecture.md](docs/architecture.md#cli-reference).

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
`[[sources]]` (each `label` becomes `<data_dir>/<label>.beancount`), and a
`[post]` block toggling the `prices` / `portfolio` / `balances` / `reports`
/ `reconcile` / `check` post-processing steps.

`[post.reconcile]` (off by default) runs the reconciliation step just before
`check`: because `bean-check` exits nonzero on a drifted assertion, reconcile
goes first so its localised drift report under `reports/reconciliation/` is
always produced. Drift fails the rebuild; `strict = true` also fails it on
coverage gaps. Enable it alongside `[post] balances = true` so there's a
`balances.beancount` to compare against.

## Output: beancount + structured sidecar

Every generated `.beancount` file is accompanied by a
`<stem>.transactions.jsonl` sidecar holding the raw extracted `Transaction`
objects (one JSON object per line, after a `_schema` header line). The
rendered beancount encodes much of the UK-tax-relevant data — GBP rate,
withholding tax, accrued interest — into postings and metadata; the sidecar
preserves the structured form so downstream tooling (the UK tax-report
stage) can consume it without re-parsing beancount text. `ingest` and
`rebuild` write sidecars automatically; `banking-pipeline dump-transactions
<pdf>` prints the same JSONL to stdout for ad-hoc inspection.

## Reports

A family of read-only analytical reports over the pipeline's output. Each
reads the latest **statement** marks, the **ledger** (via `bean-query`), or
the JSONL **sidecars**, and writes Markdown + CSV under `reports/<name>/`
(or, for `balance-sheet`, a standalone HTML). Run any standalone, or enable
them in the `[post.reports]` block of `banking-pipeline.toml` so `rebuild`
regenerates them each run. Full per-command reference — options, `--strict`,
reconciliation caveats — is in
[docs/architecture.md § Reports](docs/architecture.md#reports).

**Wealth / valuation** — values holdings in GBP from each portfolio's latest
statement marks; a negative cash balance (the Lombard loan) is netted by
currency and reported separately. These need a statement source
(`--statement <pdf>` or `--statements-dir <archive>`):

```bash
uv run banking-pipeline concentration --statements-dir <archive>        # exposure by holding / class / currency / domicile / issuer
uv run banking-pipeline net-worth --statements-dir <archive>            # net worth over time
uv run banking-pipeline allocation --statements-dir <archive>           # asset-class mix over time
uv run banking-pipeline portfolio-allocation --statements-dir <archive> # per-portfolio breakdown of the latest valuation
uv run banking-pipeline holdings --statements-dir <archive>             # cost basis + unrealised P&L (UK section 104)
```

`holdings` also reads the sidecars (`--source`, default `data`) for the cost
basis: it joins each holding's statement market value with its UK section 104
pooled cost and reports the unrealised gain/loss, cross-checking the statement
quantity against the pool. The cost basis is a UK-tax lens (`--basis uk`; an
`es` EUR/Spanish lens is reserved but not yet built) — not equal to Pictet's
own figures and never fed to the tax pipeline. Each holding is marked
**foreign** or **UK-situs** (from `data/commodities.toml`) and the unrealised
total is split on that axis, so the report reads in the light of a FIG claim
(under which foreign gains are relievable and foreign losses disallowed); a
holding with no metadata is flagged as unclassified rather than silently
treated as taxable.

**Ledger** — queried via `bean-query` (default `main.beancount`); a missing
`bean-query` binary is a warning, not an error (`uv tool install beancount`):

```bash
uv run banking-pipeline trial-balance          # per-account balances, GBP column on Assets/Liabilities
uv run banking-pipeline balance-sheet --open   # interactive HTML — scrub to any as-of date
```

`balance-sheet` builds a single self-contained, **offline**
`balance-sheet.html` you open in any browser and scrub to *any* date: it
sums each holding up to that date and values it to GBP entirely client-side,
rendering a collapsible account tree, an allocation donut, and the
Assets / Liabilities / net-worth totals. The artifact inlines real balances,
so `reports/balance-sheet/` is git-ignored. Both ledger reports are the
**ledger-faithful** view and deliberately **do not reconcile** with the
wealth reports above — different source (ledger positions vs latest
statement snapshot), as-of, and scope.

**Income** — dividends + interest *received*, from the sidecars (default
`--source data`):

```bash
uv run banking-pipeline income                   # by tax year & paying source, in GBP
uv run banking-pipeline income --period calendar # ... by calendar year instead
```

Unlike `tax-report`, `income` **includes** ISA income (flagged in a wrapper
column, not dropped) and counts UK + foreign alike.

**Mandate (Pictet)** — the three-step cost / return / value-add view (these
also read the statement archive; `mandate-scorecard` additionally queries
the ledger for costs):

```bash
uv run banking-pipeline mandate-scorecard --statements-dir <archive>   # all-in explicit cost block
uv run banking-pipeline mandate-returns --statements-dir <archive>     # time- & money-weighted returns
uv run banking-pipeline benchmark --statements-dir <archive>           # value-add vs a benchmark index
```

## UK tax reporting

`banking-pipeline tax-report --year 2025-26` reads the JSONL sidecars
(never the beancount text — so it stays clear of the GPL constraint),
applies UK tax-year boundaries and the section 104 / same-day / 30-day
share-matching rules, and writes CSV inputs for the self-assessment forms:

```bash
uv run banking-pipeline tax-report --year 2025-26 \
    --source data --out reports/uk-tax/2025-26
```

Outputs (all GBP):

- `sa108-disposals.csv` — capital-gains disposals for reporting-status
  and UK-domestic securities: `disposal_date`, `isin`, `commodity_name`,
  `reporting_status`, `quantity`, `proceeds_gbp`, `cost_gbp`, `gain_gbp`,
  `match_type` (`same-day` / `bed-and-breakfast` / `s104`),
  `acquisition_dates`.
- `sa106-dividends.csv` — foreign dividends grouped by source country and
  ISIN: `country`, `isin`, `commodity_name`, `gross_gbp`, `wht_gbp`,
  `net_gbp`, `document_count`.
- `sa106-interest.csv` — foreign interest, same columns as the dividends
  CSV. Holds distributions from offshore funds flagged
  `distributions_as_interest` in `data/commodities.toml` (the UK ">60%
  interest-bearing" bond-fund rule), taxed as interest rather than dividends.
- `sa106-offshore-income-gains.csv` — disposals of non-reporting funds
  (taxed as income, not CGT).
- `sa106-deep-discounted.csv` — disposals of securities flagged
  `deeply_discounted` in `data/commodities.toml` (gain taxed as income,
  loss generally not allowable).
- `sa106-eri.csv` — excess reportable income for accumulating reporting
  funds (from `data/eri.toml`), split dividend / interest. The base-cost
  adjustment also uplifts the section 104 pool, so a later disposal isn't
  taxed again on income already charged.
- `cgt-loss-carryforward.csv` — the year-by-year annual-exempt-amount and
  allowable-loss chain (see [Capital gains allowances and
  losses](#capital-gains-allowances-and-losses)).
- `fig-designation.csv` — only under a FIG claim: the foreign income and
  non-UK gains relieved that year (see [UK residence and the FIG
  regime](#uk-residence-and-the-fig-regime)).
- `summary.txt` — totals plus warnings for anything not on a CSV.

For `sa108-disposals.csv`, the `period` column splits a year's gains before /
on-or-after its CGT rate-change date (e.g. 30 Oct 2024 for 2024-25), and
`acquisition_dates` carries the section 104 pool's earliest acquisition for
pooled disposals.

Three optional user-maintained TOMLs refine the figures, each gitignored with
a committed `.example.toml`: `data/commodities.toml` (reporting status and
the `deeply_discounted` / `distributions_as_interest` flags),
`data/opening-positions.toml` (pre-ledger section 104 cost basis — pass
`--opening-positions`), and `data/eri.toml` (excess reportable income — pass
`--eri`). Current-account interest is *not* foreign income: it's loan
interest the user pays, booked to `Expenses`.

### GBP rates

GBP figures use each transaction's trade-date `gbp_rate` stamped during
`ingest` (when `BANKPIPE_GBP_RATE_SOURCE` is set), with a rate source as the
fallback for older sidecars whose `gbp_rate` is unset. **The stamp wins** — to
switch a whole ledger to a different source you re-ingest / `rebuild` with that
source set, not just pass `--rate-source` at report time (that only fills
un-stamped rows).

Two sources ship, both implementing the `GbpRateSource` protocol in
`banking_pipeline.fx.gbp_rates` (add another by implementing `get_rate`):

- **`hmrc-monthly`** — HMRC's monthly-average rates, from a user-maintained
  CSV at `data/fx/hmrc-monthly-average.csv` (`BANKPIPE_HMRC_RATE_PATH`).
  Columns `month` (`YYYY-MM`), `currency`, `rate` (GBP per 1 unit). Populate
  it from HMRC's GOV.UK tables, or run `scripts/fetch_hmrc_rates.py`.
- **`ecb-daily`** — the ECB daily euro reference rates, a **daily spot proxy**
  closer to trade-date spot than the monthly average. `scripts/fetch_ecb_rates.py`
  downloads the full history and triangulates it to GBP-per-unit at
  `data/fx/ecb-daily.csv` (`BANKPIPE_ECB_RATE_PATH`). These are ECB *reference*
  (mid-market) rates — a consistent CGT basis, **not** a broker's dealt rate,
  so they won't equal a custodian's booked GBP (which carries a spread); for
  that, stamp the per-transaction rate from the trade advice.

Pick **one** source and use it consistently across the whole section 104
history — mixing them across acquisitions corrupts the pooled cost. Whichever
you choose, an amount that can't be converted is excluded (a coverage gap),
never guessed.

An amount that can't be converted (no per-transaction `gbp_rate` and no
source rate) is **excluded** from the figures rather than guessed — so a gap
silently *understates* the report. To make that actionable, every
unconvertible amount is recorded as a coverage gap naming the exact CSV row
to add — e.g. `USD 2024-05 (US0378331005)` — and surfaced in `summary.txt`
(and on the forecast/pack). Pass `--strict` to `tax-report` / `tax-forecast`
to turn any gap into a non-zero exit, so a CI run can't silently
under-report.

### Commodity metadata (`data/commodities.toml`)

`tax-report` needs to know each ISIN's reporting status to route disposals
correctly. The hand-curated `data/commodities.toml` is the source — one
section per ISIN with at least `name` and `reporting_status` (`reporting` /
`non-reporting` / `uk-domestic` / `unknown`); `domicile` (ISO 3166-1
alpha-2) overrides the ISIN prefix as the withholding-tax country. Two
optional booleans reroute income: `deeply_discounted` (gain taxed as income)
and `distributions_as_interest` (a >60% interest-bearing "bond fund" — its
distributions and ERI are foreign interest, not dividends). See
`data/commodities.example.toml` for the schema. `portfolio
--list-missing-metadata` prints every in-use ISIN not yet in the file, which
is the loop for keeping it in sync.

**Notes / limitations:** unclassified holdings (no commodity metadata) are
flagged in `summary.txt` rather than guessed. Cost basis falls back to zero
(and the summary warns "disposed more than acquired") when a disposal
pre-dates the ledger and no `data/opening-positions.toml` lot covers it. None
of this is tax advice — verify against HMRC guidance.

### Capital gains allowances and losses

`tax-report` threads the capital-gains **annual exempt amount** (AEA) and
**allowable losses** across tax years, writing `cgt-loss-carryforward.csv`
and a CGT-allowance block in `summary.txt`. It runs the section 104 matcher
over the full history, buckets disposals by tax year, and applies HMRC's
statutory deduction order: current-year losses first (even where that wastes
the AEA), then brought-forward losses *only down to the AEA*, then the AEA
itself. In a year with a mid-year rate change it absorbs relief against the
higher-rate gains first. The AEA per year is the statutory
`cgt_annual_exempt_amount` setting; pre-ledger brought-forward losses are
seeded from `data/cgt-losses.toml` (a single `brought_forward_gbp`). The
4-year loss-claim time limit is not enforced.

### Forecasting the liability (`tax-forecast`)

`tax-forecast --income <gbp>` turns the figures above into an estimated pound
liability for the current (incomplete) tax year, so there are no April
surprises:

```bash
uv run banking-pipeline tax-forecast --year 2025-26 --income 60000
```

`--income` is your expected non-savings, non-dividend income (e.g. salary +
rent) before the personal allowance — it sets the marginal band the
investment income and gains stack on top of. The estimate stacks income in
UK order (non-savings → savings → dividends → capital gains on the remaining
basic-rate band), applies the statutory rates/bands, the personal-allowance
taper, and foreign tax credit relief on withholding tax, and writes
`forecast-summary.txt` + `forecast.csv`. It uses year-to-date *actuals* only
(no run-rate extrapolation), assumes England/Wales/NI rates and a single
taxpayer, and excludes ISA-wrapped transactions. Statutory rates/bands live
in `banking_pipeline.tax.uk.rates` (overridable via the `income_tax_bands` /
`cgt_forecast_rates` settings).

### Tax pack (`tax-pack`)

`tax-pack` renders `tax-pack.md`, a single per-year filing aid that ties the
computed SA108 / SA106 figures to the boxes on the HMRC forms — the CGT
listed-shares boxes and allowance computation, foreign dividends/interest
with FTCR, offshore income gains, deeply discounted securities, ERI, and the
FIG designation under a claim. It is a filing aid, not tax advice; HMRC
re-numbers the forms, so the box numbers are indicative and carry a
verify-against-the-form caveat in the output.

### UK residence and the FIG regime

By default the tax stage assumes UK arising-basis residence across the whole
history. Set `BANKPIPE_UK_RESIDENCE_START_DATE` (a split-year arrival date)
to correct that: income and gains arising before it aren't UK-taxable, and
whole tax years before it are skipped. The section 104 pool is unaffected —
acquisitions feed it whenever they happened; only the taxable *output* is
residence-filtered.

`BANKPIPE_FIG_CLAIM_YEARS` (a JSON array of tax-year labels) applies the
**4-year Foreign Income & Gains regime** for the listed years (available from
2025-26, for the first four UK-resident years). A claimed year relieves
foreign income and non-UK gains to nil but forfeits the personal allowance
and the CGT annual exempt amount — so it's worthwhile only when the relieved
amounts outweigh those allowances. `tax-report` moves the relieved items onto
`fig-designation.csv`; `tax-forecast` computes the year with and without the
claim and recommends the cheaper. Foreign-vs-UK situs is derived from a
holding's domicile / `uk-domestic` status, or set explicitly with a
`uk_situs` flag in `data/commodities.toml`.

Because claiming a year also *disallows* that year's foreign losses — which
would otherwise carry forward and shelter later gains — the cheapest set of
years to claim isn't the per-year answer. `fig-advice --income <gbp>`
evaluates every claim combination across the eligible window jointly
(threading the loss chain) and recommends the cheapest, writing
`fig-advice.txt`. It uses year-to-date actuals, so a recommendation touching
the current year is provisional.

Where `fig-advice` optimises the *realised* facts, `fig-projection --income
<gbp>` is the forward companion: it takes the **foreign** unrealised gains from
the holdings report and prices the CGT you'd avoid by **crystallising** them in
a claimed window year (relieved to nil, resetting the base cost) versus
**deferring** to a taxable post-window disposal — surfacing the saving, the
**act-by date** (the window's close), and the base cost each winner would reset
to (its current market), so the post-window position is visible too. An upper
bound (the saving is only real
if you eventually dispose; CGT is uplifted on death), priced by stacking the
gain above `--income`; it flags the 30-day bed-and-breakfast mechanic but does
not pick lots. Writes `fig-projection.md` + `.csv`. Planning aid, not advice.

This is a filing aid with documented simplifications (the
10-prior-non-resident eligibility test, temporary non-residence clawback, and
former-remittance-basis rebasing are not modelled) — the full list and
rationale are in [docs/design-decisions.md](docs/design-decisions.md). Not
tax advice; verify against HMRC guidance.

## Validation

The pipeline ships with a `bean-check` integration so writer regressions and
balance drift surface inside the rebuild instead of lurking until the next
ledger load. The validator runs as the final post-step of `rebuild` (gated on
`[post.check]`); it can also be invoked standalone via `banking-pipeline
check <ledger>`, or piggy-backed on a single-PDF `ingest` via `--check
<ledger>`. All three exit with `bean-check`'s own return code so cron / CI
can branch on success.

`bean-check` itself comes from the `beancount` package (GPL-2.0). We shell
out rather than import — install with `uv tool install beancount`. A missing
binary degrades to a warning, not a failure; set `[post.check] enabled =
false` to skip the step entirely.

### Reconciliation

`bean-check` enforces the balance assertions extracted from monthly
statements (`data/balances.beancount`), but it aborts on the first failure
and can't tell you about a statement you never ingested. `banking-pipeline
reconcile` is the friendlier, additive view:

```bash
uv run banking-pipeline reconcile               # main.beancount vs data/balances.beancount
uv run banking-pipeline reconcile main.beancount -b data/balances.beancount -o reports/reconciliation
```

It runs `bean-check` once, parses every balance-assertion failure, and writes
two files under `reports/reconciliation/` (override with `--output` or the
`reconciliation_dir` setting):

- `summary.txt` — drifted assertions with expected / actual / signed
  difference, the **earliest** date each account diverged (so a missed or
  misclassified document is localised to one statement month), and
  **coverage gaps** (statement months with no assertion at all).
- `drift.csv` — every reconciled row, machine-readable.

Because the drift verdict is `bean-check`'s own, `reconcile` agrees with a
ledger load by construction — no separate tolerance to keep in sync. It exits
nonzero on any drift (CI-friendly, like `check`); `--strict` also fails on
coverage gaps.

### Completeness & transaction cross-checks

Where `reconcile` compares *balances*, two further checks compare
*transactions* against the bank's own records — catching a document you never
ingested (which no balance assertion would reveal if a later statement still
reconciles):

```bash
uv run banking-pipeline completeness              # cash-ledger vs sidecars
uv run banking-pipeline reconcile-transactions    # portal trades vs sidecars
```

- `completeness` cross-checks the Pictet current-account cash ledger — both
  the monthly `Financial-statement` PDFs and the portal "cash statement by
  value date" CSV export — against the sidecars, transaction by transaction,
  and reports any statement line with no matching ingested row.
- `reconcile-transactions` cross-checks the portal **Transactions** CSV export
  (every trade leg, both mandates) against the sidecars by `Order nr.`,
  catching a securities trade the pipeline failed to ingest — which would
  corrupt the section 104 pool and the CGT figures.

Both write per-source Markdown + CSV findings and exit nonzero on a real gap
(a missing / amount-mismatched row), so a rebuild fails loudly; they're wired
into `rebuild` via `[post.completeness]` and `[post.reconcile_transactions]`.

### Duplicate audit

Where `reconcile` catches *missing* or drifted entries, `dedup-check` catches
the opposite — the same economic event counted twice (the same advice PDF
matched by two source globs, a file copied into two year folders, a re-issued
document). It's read-only and never touches the ledger:

```bash
uv run banking-pipeline dedup-check               # audit data/
uv run banking-pipeline dedup-check data -o reports/duplicates.csv
```

It walks the `*.transactions.jsonl` sidecars, assigns each transaction a
**content key** (trade date + signed amount + currency + ISIN + doctype +
account — deliberately *not* the per-document reference, so the same event
from two documents collides), and reports each group sharing a key:

- **EXACT** — the members share one document reference, i.e. the same advice
  was ingested twice. Near-certain duplicate.
- **POSSIBLE** — same content, different/absent references. Could be a genuine
  pair of identical events (two equal dividends same day) — review these.

Each sidecar line also carries the `dedup_key` so external tooling can group
without re-deriving it. `dedup-check` exits nonzero when any duplicate is
found.

## Tests

```bash
uv run pytest
uv run ruff check .
uv run mypy src
```

Parametric suites discover every fixture under `tests/fixtures/` and assert
language + bank + doctype all clear the confidence threshold — so adding a
fixture automatically extends test coverage, and a rule regression surfaces
as a failing parametrisation against the exact fixture it broke on.

## Contributing / internals

The module map, the data flow, the full CLI and configuration reference, the
rule-authoring loop, and the step-by-step recipe for adding a bank or a
doctype live in **[docs/architecture.md](docs/architecture.md)**. The durable
*why* behind the load-bearing choices is in
[docs/design-decisions.md](docs/design-decisions.md), and the hard
constraints that bind every change are in [CLAUDE.md](CLAUDE.md).
[docs/README.md](docs/README.md) maps every doc.

# CLAUDE.md

Project-specific instructions for Claude working in this repo. The
`README.md` is the authoritative user-facing guide; this file captures
the non-obvious context and conventions that aren't worth re-reading
the README for every session.

## Project summary

`banking-pipeline` is a Python data pipeline that ingests banking
correspondence (PDFs and one Revolut-CSV side path), classifies each
document by language → bank → doctype, extracts the fields that matter
for accounting, and emits [beancount](https://beancount.github.io/)
entries. It is single-user, designed around Pictet's Luxembourg and
Madrid templates today, but structured so adding a new bank is a
data-only change (new fixtures + new ruleset + new template package).

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
│                         prices | balances | portfolio | check |
│                         reconcile | rebuild | tax-report)
├── pipeline.py         Top-level Pipeline orchestration
├── models.py           Domain models — DocumentType, BankId, Language,
│                         RawDocument, Classification, Transaction,
│                         FeeItem, ExtractionResult, NO_OUTPUT_DOCTYPES
├── config.py           Pydantic settings (env_prefix=BANKPIPE_)
├── batch_config.py     `banking-pipeline.toml` schema for `rebuild`
├── bean_check.py       Shells out to the bean-check binary
├── reconcile.py        Statement-balance reconciliation: parses
│                         bean-check assertion failures into a drift
│                         report (drift rows + earliest-drift +
│                         coverage gaps)
├── beancount_writer.py Back-compat re-export of `writer.*`
├── balances_extract.py Pictet monthly-statement → balance assertions
├── prices_extract.py   Per-trade + monthly-statement → price directives
├── portfolio_aggregate.py Central account opens + per-year includes
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
│       └── eri.py         Excess reportable income + equalisation
│                            (data/eri.toml → income + base-cost uplift)
├── templates/
│   ├── __init__.py       TEMPLATE_REGISTRY (populated at import)
│   └── pictet/           ~40 per-doctype templates (EN + ES locales)
├── writer/
│   ├── dispatch.py       Doctype → builder routing, render()/render_all()
│   ├── format.py         Amount/posting/account-name primitives
│   ├── profile.py        Per-bank writer config (account_prefix, …)
│   └── builders/         One module per render shape
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
  `<prefix>` comes from `writer/profile.py` (Pictet → `Pic`).
- Portfolio identifiers are sanitised through `portfolio_segment` —
  Pictet prints `K-123456.001`; beancount segments don't allow `-` or
  `.`, so it becomes `K123456001`.
- Per-currency rounding tolerances live in the hand-curated
  `main.beancount` at the repo root (`inferred_tolerance_default
  "<ccy>:0.005"` for cent-precision fiat, `JPY:0.5` for yen). ISIN
  commodities deliberately don't set a default — beancount's
  inferred-from-decimals picks the right value per fund.
- `data/portfolio.beancount` is **generated** (by `portfolio_aggregate`):
  it owns `option "operating_currency"`, the booking method, and the
  central account opens. Don't hand-edit it. Hand-curated overrides
  go in `main.beancount`, which `include`s the aggregate.
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
  `include` (a stale or per-account aggregate it once wrote), and only
  constrains a `…:<CCY>` account leaf to a currency when that token
  actually appears as a posting currency (so `…:Earnout:IBM` isn't
  mistaken for a currency). Both guard against `bean-check` errors.
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
- GBP cost basis is carried as posting **metadata** (`gbp-rate`,
  `trade-date`) and as the structured `Transaction.gbp_rate` in
  the sidecar. All section 104 / same-day / 30-day matching
  happens in `tax/uk/section_104.py` from the sidecar, not in
  beancount — beancount's booking methods do not implement UK
  matching rules and would compete with the correct answer. The
  rationale is documented at length in `docs/uk-tax-prompts.md`.
- Rate sources are pluggable via the `GbpRateSource` protocol in
  `fx/gbp_rates.py`. Today there are two: `HmrcMonthlyAverageSource`
  (user-maintained CSV at `data/fx/hmrc-monthly-average.csv`,
  columns `month` `YYYY-MM`, `currency`, `rate`) and `NullSource`
  (default — no rate, the rest of the pipeline behaves exactly as
  before). `BANKPIPE_GBP_RATE_SOURCE=hmrc-monthly` opts in.
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
  as 0 and flagged in `summary.txt`). Losses are claimed automatically —
  the 4-year claim time limit is not enforced.
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
  ingest output under `data/`.
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
  `--rate-source` overrides `gbp_rate_source` for the run.

## Configuration

- Runtime config: `Settings` in `config.py`, env-prefixed
  `BANKPIPE_`. Notable knobs: `anthropic_api_key`, `anthropic_model`,
  `rule_confidence_threshold`, `default_currency`, plus two dict maps
  driving payment-routing — `beneficiary_bank_map` (self-to-self
  destinations like Revolut) and `counterparty_account_map`
  (third-party named counterparties → account segments).
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
- `reconciliation_dir` (defaults to `reports/reconciliation`) — output
  directory for the `reconcile` command's `summary.txt` / `drift.csv`.
- Batch config: `banking-pipeline.toml` (gitignored, schema in
  `batch_config.py`). Carries personal Dropbox/iCloud paths, plus
  `[post.reconcile]` (off by default) and `[post.check]` toggles.
- `.env.example` lists the env vars; copy to `.env` for local work.

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

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
├── cli.py              Typer entrypoint (ingest | classify | scan |
│                         extract-text | revolut | prices | balances |
│                         portfolio | check | rebuild)
├── pipeline.py         Top-level Pipeline orchestration
├── models.py           Domain models — DocumentType, BankId, Language,
│                         RawDocument, Classification, Transaction,
│                         FeeItem, ExtractionResult, NO_OUTPUT_DOCTYPES
├── config.py           Pydantic settings (env_prefix=BANKPIPE_)
├── batch_config.py     `banking-pipeline.toml` schema for `rebuild`
├── bean_check.py       Shells out to the bean-check binary
├── beancount_writer.py Back-compat re-export of `writer.*`
├── balances_extract.py Pictet monthly-statement → balance assertions
├── prices_extract.py   Per-trade + monthly-statement → price directives
├── portfolio_aggregate.py Central account opens + per-year includes
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
│   └── hybrid.py         Template → regex → LLM dispatch
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
`bean-check` warnings to errors regardless of `[post.check] strict`.

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
- `examples/accounts.beancount` is a starter chart of accounts for
  external readers — not loaded by the rebuild.

## CLI surface (run via `uv run banking-pipeline …`)

- `ingest` — classify + extract + render one or more PDFs; supports
  `--check <ledger>` to validate in the same step and `--strict`.
- `classify` — just print the language/bank/doctype verdict.
- `scan` — walk a directory; one row per PDF; `--json` for JSONL.
- `extract-text` — dump PDF text; `--show-rules` shows which rules
  fired at each stage (the rule-authoring workhorse).
- `prices`, `balances`, `portfolio` — aggregators that read the
  ingest output under `data/`.
- `check` — standalone `bean-check` wrapper.
- `rebuild` — end-to-end run driven by `banking-pipeline.toml`
  (gitignored; copy from `banking-pipeline.example.toml`). Owns the
  `clean → ingest per source → prices/portfolio/balances → check`
  sequence.
- `revolut` — separate side path; Revolut Personal CSV → beancount.

## Configuration

- Runtime config: `Settings` in `config.py`, env-prefixed
  `BANKPIPE_`. Notable knobs: `anthropic_api_key`, `anthropic_model`,
  `rule_confidence_threshold`, `default_currency`, plus two dict maps
  driving payment-routing — `beneficiary_bank_map` (self-to-self
  destinations like Revolut) and `counterparty_account_map`
  (third-party named counterparties → account segments).
- Batch config: `banking-pipeline.toml` (gitignored, schema in
  `batch_config.py`). Carries personal Dropbox/iCloud paths.
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

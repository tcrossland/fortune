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

Every runtime dependency is MIT, BSD, or Apache-2.0 except `python-stdnum`
(LGPL-2.1+, which is fine for dynamic linking like we do here).

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
| Templating | `jinja2` | BSD-3-Clause | Beancount entry rendering |
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

# Optional validation against your ledger
bean-check examples/accounts.beancount out.beancount
```

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
├── cli.py              Typer entrypoint (classify | scan | ingest | extract-text)
├── config.py           Pydantic settings
├── models.py           Domain models (RawDocument, Transaction, DocumentType, BankId, Language)
├── pipeline.py         Top-level orchestration
├── beancount_writer.py Render Transactions as beancount text
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
│   └── hybrid.py         Template → regex → LLM
└── templates/          Per-bank extractors (add your own)

tests/fixtures/<lang>/<bank>/<doctype>.txt
                     ^      ^       ^
                     |      |       └── matches DocumentType enum value
                     |      └────────── matches BankId enum value
                     └───────────────── matches Language enum value (ISO 639-1)
```

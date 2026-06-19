# Plan: statement-completeness cross-check

## Goal

Detect **missing or dropped cash bookings** by diffing the Pictet
*current-account statement* — the authoritative, complete list of every
cash movement for a period — against the transactions the pipeline
actually ingested. An advice PDF that never made it into the archive (or
that was misclassified to a no-output doctype) silently drops a
transaction today; nothing surfaces it unless it happens to break a
later balance assertion.

Output: a report listing every statement cash line with **no matching
ledger transaction** (the prime signal — a likely un-ingested document),
plus the reverse (ledger transactions with no statement line — a
possible spurious or misdated booking). A new `banking-pipeline
completeness` command and an optional `rebuild` post-step, gated by
`--strict`, mirroring how `reconcile` is wired.

## Background

### Why this is new parsing

The annual `Financial-statement-*.pdf` files
(`~/Dropbox/2-areas/26-pictet/<year>/reports/`) contain four sections:
portfolio valuation, **current-account statement (EUR + USD)**,
deposits/withdrawals summary, glossary. The existing statement parsers
read **only the portfolio-valuation page**:

- [`prices_extract.py`](../../src/banking_pipeline/prices_extract.py) →
  per-holding market prices.
- [`balances_extract.py`](../../src/banking_pipeline/balances_extract.py)
  → per-holding / per-currency *balance* assertions
  (`data/balances.beancount`), consumed by
  [`reconcile.py`](../../src/banking_pipeline/reconcile.py).

Nothing reads the **current-account statement** — the line-by-line cash
ledger (`Bonificación`, `Suscripción`/`Reembolso`/`Compra`, `Dividendo`,
`Gastos de custodia`/`Honorarios …`, `Transferencia`). That section is
the ground truth this check needs.

### Why `reconcile` doesn't already cover it

`reconcile` compares *balances* (statement marks vs. ledger sums) via
`bean-check`'s assertion verdicts. A balance assertion only fires monthly
and only catches a drop if it lands on the wrong side of a checkpoint —
two equal-and-opposite errors, or an error after the last assertion,
slip through. A **transaction-level** diff localises the exact missing
line. This is additive to `reconcile`, same shape (pure diff module + CLI
+ rebuild step), different granularity.

### Match substrate

Compare against the **JSONL sidecars** (`data/<year>-K.transactions.jsonl`),
never the ledger text — same invariant as the tax pipeline. A real
sidecar row carries the fields we match on:

```
trade_date, settlement_date, currency, amount,
account_number ("K-999999.001"), document_type, narration
```

`transaction_number` is frequently `None`, and the statement line carries
**no reference number at all** (only BOOK DATE / DESCRIPTION / VALUE DATE
/ DEBIT / CREDIT / BALANCE). So the match key is heuristic:

> **(portfolio, currency, date, signed-amount)** with the description as
> a tiebreaker.

Statement DEBIT = cash out (subscription/purchase/fee/transfer-out);
CREDIT = cash in (deposit/dividend/redemption/transfer-in). VALUE DATE
maps to `settlement_date`; BOOK DATE to `trade_date`. Match on value
date first, fall back to book date (advices vary on which they stamp).

### The hard part: N:1 line→transaction mappings

A naive 1:1 amount match produces false "missing" hits. Three known
shapes break it — the design must handle them or the report is noise:

1. **Quarterly fees.** The statement prints **three** lines per quarter
   (`Gastos de custodia`, `Honorarios de administración`, `Honorarios de
   gestión`), but a single `debit_of_fees` advice books **one**
   `Transaction` with several expense postings. → Aggregate consecutive
   same-date fee lines before matching, or match the fee *group* sum.
2. **Internal FX transfer.** `Transferencia a/de su cuenta ordinario`
   appears as **two** lines — a debit in the EUR section and a credit in
   the USD section — for **one** `transferencia_interna` `Transaction`
   carrying both legs (`counter_currency`/`counter_amount`). → Pair the
   two legs across currency sections; match the pair to one tx.
3. **Page-structure noise.** `Balance carried forward`, section headers,
   the `^ Deposits/withdrawals` footer total, `Statement without
   reversals` — never bookings. → Skip by line shape.

### Built-in integrity check (free byproduct)

Each section carries a running BALANCE column. Parsing it lets us
**self-verify the parse** (Σ debits/credits from the opening
carried-forward balance must reproduce each printed balance) and recover
**period start/end cash balances** — a cheap cross-check on the existing
`balances.beancount` assertions. Use it as a parser assertion, not a
separate feature.

## Design

A pure module + a CLI command + a rebuild step, mirroring `reconcile`:

```
statement_completeness.py   ── parse current-account section → list[CashLine]
                               diff(CashLine[], sidecar rows) → CompletenessReport
cli/reports.py: completeness ── discover FS pdfs, dump text, load sidecars,
                               run diff, render md/csv, exit non-zero on findings
cli/rebuild.py / batch_config ── optional post-step, gated by --strict
```

`statement_completeness.py` stays **pure** (text + parsed sidecar rows
in, report out) so the parse/diff/aggregation logic is unit-testable
without PDFs or the `bean-check` binary — same discipline as
`reconcile.py`.

Reuse: `prices_extract._parse_statement_date` and the Spanish-header
account-number regexes in `balances_extract.py` for the statement header
(portfolio number, period dates, EUR/USD section boundaries).

### Data shapes (sketch)

```python
@dataclass(frozen=True)
class CashLine:
    portfolio: str        # "K999999001"
    currency: str         # "EUR" | "USD"
    book_date: str
    value_date: str
    description: str
    amount: Decimal       # signed: +credit / -debit
    running_balance: Decimal

@dataclass(frozen=True)
class CompletenessRow:
    line: CashLine
    status: Status        # MATCHED | MISSING_IN_LEDGER | UNMATCHED_IN_LEDGER
    matched_dedup_key: str | None
```

## Phasing

Worked in order; status folded in below.

### Phase 1 — current-account parser
- `parse_current_account(text) -> list[CashLine]`, handling the EUR and
  USD sections, the carried-forward page breaks, and the multi-line wrap
  seen in the dumps.
- Self-check against the running-balance column; raise on mismatch.
- Skip-list for non-booking lines (carried forward, footers, headers).
- Fixture: add a **scrubbed** financial-statement text fixture that
  includes the current-account section (the existing
  `tests/fixtures/en/pictet/annual_statement.txt` is valuation-oriented —
  confirm whether it has the cash section; if not, add
  `…/financial_statement.txt`). Anonymise per the PII guard: portfolio
  body `999999`, scrub name/IBAN. Unit tests for parse + balance
  self-check.

### Phase 2 — diff against sidecars
- `diff(lines, sidecar_rows) -> CompletenessReport`.
- Match key (portfolio, currency, value→book date fallback, signed
  amount); description tiebreaker.
- **Aggregate quarterly fee groups** (shape 1) and **pair FX legs across
  sections** (shape 2) before matching.
- Classify into MATCHED / MISSING_IN_LEDGER / UNMATCHED_IN_LEDGER.
- Unit tests covering each N:1 shape with hand-built rows (no PDFs).

### Phase 3 — CLI command + rendering
- `completeness` command in [`cli/reports.py`](../../src/banking_pipeline/cli/reports.py):
  discover FS PDFs (own glob — they live under `reports/`, not the
  ingest tree), dump text, load the matching year's sidecar, diff,
  render Markdown + CSV (mirror `render_summary`/`render_csv`).
- Exit non-zero when MISSING_IN_LEDGER rows exist.
- Decide statement↔sidecar-year pairing (FS date → which `<year>-K`
  sidecar; the mid-2023 statements span a partial year — scope by the
  statement's `From … to …` header).

### Phase 4 — rebuild wiring + docs
- **Count refinement.** `excluded` / `out-of-period` were tallied over the
  *whole* `data/` sidecar tree (every year of the portfolio), so a single
  statement reported e.g. `excluded 86, out-of-period 506` — diagnostics
  that scale with the dataset, not the statement, and read as noise.
  Bound the diff to the statement's window: skip sidecar rows whose date
  falls outside `[start − tolerance, end + tolerance]` before counting, so
  both tallies become per-statement-meaningful. Safe for matching — a
  statement line (in-period) can only match a sidecar settling within
  `±tolerance` of an in-period date, all of which fall inside the window.
- Optional `rebuild` post-step + `batch_config` block (mirror the
  `reconcile` config), `--strict` escalates UNMATCHED to failure (MISSING
  always fails once enabled). Shared `_run_completeness` core so the
  `completeness` command and the rebuild step behave identically (mirrors
  `_run_reconcile`).
- Docs: command + config in
  [`docs/architecture.md`](../architecture.md), the
  ledger-vs-statement-vs-sidecar rationale in
  [`docs/design-decisions.md`](../design-decisions.md), one line in
  [`CLAUDE.md`](../../CLAUDE.md) reconcile/completeness section.

## Risks / open questions

- **False positives from the heuristic key** are the main failure mode.
  If they're noisy even after handling the three N:1 shapes, the command
  is worse than nothing. Gate it behind `--strict` (off by default) until
  the 2021–2023 statements diff clean against known-good sidecars — that
  back-test *is* the acceptance criterion.
- **Same-day, same-amount collisions** (two identical subscriptions on
  one day) — fall back to consuming matches greedily and report any
  residual ambiguity rather than guessing.
- **Statement language drift** — these are English-titled but
  Spanish-bodied; the EN Luxembourg statements use English line verbs.
  Parser must key off the structural columns, not the verb vocabulary,
  or carry both lexicons.
- **Partial-year statements** (2023-05/06) — confirm the `From … to …`
  window is honoured so we don't flag the whole year's tail as missing.

## Definition of done

Standard loop (`ruff` / `mypy src` / `pytest`) clean; new unit tests for
parser + each N:1 diff shape; the 2021/2022/2023 financial statements
back-tested to **zero** MISSING_IN_LEDGER against the current sidecars
(or every residual explained); `code-reviewer` clean; PII guard clean;
docs + this plan updated.

## Status

**Complete (phases 1–4).** Shipped: the `completeness` command + the
`[post.completeness]` rebuild step, validated through the real pypdfium2
loader (0 missing / 0 unmatched across 2021–2023).

Shipped in Phase 4:
- **Count refinement** — `diff` skips sidecar rows outside
  `[start − tol, end + tol]` before tallying, so `excluded` /
  `out_of_period` are per-statement (2023-05: `86/506` → `12/3`), not
  dataset-wide. Safe for matching (proven: a statement line only matches a
  sidecar within `tol` of an in-period date).
- **Shared core** `_run_completeness` in `cli/_main.py` (with
  `_load_sidecar_rows`), used by both the command and the rebuild step
  (mirrors `_run_reconcile`).
- **`[post.completeness]`** config (`CompletenessStep`: `enabled`,
  `statements` glob, `strict`) wired into `rebuild` between reconcile and
  check — MISSING fails the rebuild, UNMATCHED fails under strict. Tests in
  [`test_rebuild_completeness.py`](../../tests/test_rebuild_completeness.py).
- **Docs**: `completeness` command + `[post.completeness]` + `completeness_dir`
  in [architecture.md](../architecture.md); the by-transaction-vs-by-balance
  rationale in [design-decisions.md](../design-decisions.md).

Final verification: ruff + mypy clean, 927 tests, PII guard clean.

---

### Original phasing (all done)

Shipped in Phase 3:
- `banking-pipeline completeness` command
  ([`cli/reports.py`](../../src/banking_pipeline/cli/reports.py)):
  `--statement` (repeatable) / `--statements-dir` (scans
  `Financial-statement-*.pdf`), `--source` (sidecar dir, default `data`),
  `--out`, `--strict`. Discovers + parses each statement, diffs against
  the sidecars (per-portfolio, period-bounded), writes `summary.txt` +
  `findings.csv`. Exits non-zero on any MISSING; `--strict` adds UNMATCHED.
- `render_summary` / `render_csv` in the (pure) module; `completeness_dir`
  setting (`reports/completeness`); CLI tests in
  [`test_cli_completeness.py`](../../tests/test_cli_completeness.py).
- **One file pair per statement, keyed by period-end date**
  (`summary-<end>.txt` / `findings-<end>.csv`) so successive runs — or a
  one-statement-at-a-time workflow — don't clobber a shared
  `summary.txt`. `render_*` take a single `(name, report)`; the CLI loops.

Real-loader validation (the important bit — Phases 1–2 were validated
against `pdftotext -layout`, but the pipeline reads PDFs via **pypdfium2**,
which lays the table out differently): the command run over the four
archived PDFs through the production `load_pdf` path matched **76 / 90 /
55 / 63** lines, **0 missing, 0 unmatched**.

That surfaced a real bug Phases 1–2 missed: pypdfium2 (a) repeats the
`Current account statement in EUR` header at every page top and (b) drops
the running-balance number from page-break `Balance carried forward`
lines. The old "reset balance on every section header" logic then hit a
numberless anchor and raised. Fixed by tracking the running balance
**per currency** (restart only on a real EUR→USD change, never on a
repeated same-currency header) — locked in by
`test_repeated_header_and_numberless_page_break`.

Note: a sidecar field deviation from the plan's sketch — the FX/transfer
counter-leg, fee splitting, etc. all matched without a `dedup_key` index;
the simple `(currency, amount, date≈)` greedy match suffices.

Shipped in Phase 2 (all in
[`statement_completeness.py`](../../src/banking_pipeline/statement_completeness.py)
+ [`test_statement_completeness.py`](../../tests/test_statement_completeness.py),
now 21 tests):
- `diff(cash_lines, sidecar_rows, *, period=None)` → `CompletenessReport`
  (matched / `missing_in_ledger` / `unmatched_in_ledger` / `excluded` /
  `out_of_period`). Match key `(currency, amount)` exact + nearest date
  within tolerance; each sidecar event consumed once.
- `sidecar_cash_events(row)` — expands a row into its current-account
  leg(s): FX/internal transfers yield two (the `counter_*` leg),
  securities settlements yield none.
- `parse_statement_period(text)` — the `From … to …` / `Del … al …`
  window, used to bound `UNMATCHED_IN_LEDGER` so a partial-year statement
  doesn't flag the whole tail.

Real-data back-test (the acceptance criterion) — **all four statements
diff clean, zero findings**:

| Statement | Lines | Matched | Missing | Unmatched | Excluded | Out-of-period |
|---|---|---|---|---|---|---|
| 2021 | 76 | 76 | 0 | 0 | 16 | 0 |
| 2022 | 90 | 90 | 0 | 0 | 20 | 0 |
| 2023-05 (ES) | 55 | 55 | 0 | 0 | 50 | 116 |
| 2023-06 | 63 | 63 | 0 | 0 | 50 | 108 |

Findings that refined the design vs. the plan's guesses:
- **Fee-triple aggregation was *not* needed.** Pictet books custody /
  admin / management as three separate current-account debits, each its
  own advice → 1:1 matching. The plan's anticipated N:1 fee handling is
  dropped.
- **FX-leg expansion *was* needed** (confirmed) — the transfer's
  `counter_*` leg is a real second statement line.
- **Securities-settlement exclusion *was* needed** — verified in the
  ledger that `switch_*` posts to `Assets:…:Switch:<ccy>` and
  `liquidacion_recepcion_de_valores` to `Equity:…:Transfers`, neither
  touching the EUR/USD current account.
- **Period bounding is load-bearing** for the partial-year (2023)
  statements; bound strictly to `[start, end]` (no end slack — an event
  settling after the cut-off is the next statement's).

Shipped in Phase 1:
- [`statement_completeness.py`](../../src/banking_pipeline/statement_completeness.py)
  — `parse_current_account(text) -> list[CashLine]`. Signs each movement
  from the running-balance delta (so debit/credit detection *is* the
  self-check); raises `StatementParseError` on an unreconcilable row.
  Handles both locales (English `Balance carried forward` / Spanish
  `Saldo traspasado` etc.) and degrades to `[]` on digit-masked input.
- [`test_statement_completeness.py`](../../tests/test_statement_completeness.py)
  — 12 tests.

Validation: all four archived statements parse with the self-check
passing on **every** row — 2021 (76), 2022 (90), 2023-05 (55), 2023-06
(63) = 284 movements, zero reconcile failures.

Deviations from the plan:
- **No fixture file.** Followed `test_pictet_balances.py`'s convention of
  inline synthetic statement strings instead. The discovered
  `<lang>/<bank>` statement fixtures mask every digit to `9`, which can't
  exercise a running-balance self-check; and a depth-3 `.txt` would have
  to map to a `DocumentType` (`test_fixture_tree.py`). Inline strings
  sidestep both.
- **Sign-from-balance-delta** (vs. reading the DEBIT/CREDIT column
  position) — added because `pdftotext -layout` column alignment is
  fragile, and it folds the self-check and the credit/debit call into one
  operation. This is the key design choice that made the real-statement
  validation above possible.

Prerequisite confirmed during Phase 1: no existing code reads the
current-account section — `prices_extract` / `balances_extract` parse
only the valuation page.

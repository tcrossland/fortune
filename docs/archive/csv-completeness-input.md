# Use the portal cash-statement CSV as a `completeness` input, and archive it

**Status:** ✅ Shipped 2026-07-04. Verified on real data — 0 missing / 0
unmatched for both mandates; CSV archived to `cash-statements/`. Code-reviewed
(clean bill; 3 non-blocking items folded in — `_superseded/` excluded from
discovery, unresolved-portfolio warns, self-check docstring softened). See
CHANGELOG.

- [x] A1 — `parse_cash_statement_csv` (+ `_csv_iso_date`)
- [x] A2 — add `limit_extension` to `_NON_CURRENT_ACCOUNT_DOCTYPES`
- [x] A3 — refactor `_run_completeness` to the (portfolio, lines, period) group model (`_completeness_groups`)
- [x] A4 — CLI `.csv` discovery (`_discover_financial_statements`, skips `_superseded/`) + docstrings
- [x] B1 — `archive.file_cash_statements` (keep-latest, `_superseded/`)
- [x] B2 — `ImportStep.cash_statement_globs` + `_run_import` wiring (CSV independent of PDF sources)
- [x] B3 — config: local `banking-pipeline.toml` + tracked `banking-pipeline.example.toml`
- [x] Tests (parser self-check, grouping, portfolio map, `limit_extension` exclusion, filing keep-latest, `_superseded` skip)
- [x] Docs (architecture CLI/config reference; CHANGELOG) + code-review + commit

## Context

The `completeness` command cross-checks the Pictet current-account cash ledger
against the ingested sidecars. Today it parses that ledger out of a
`Financial-statement-*.pdf` — but only **4** exist (K mandate, periods ending
2021-12-31 → 2023-06-30, all downloaded mid-2023). None pulled since, none for
P, so completeness is dormant on current data.

This session validated the portal `Cash statements by value date` **CSV** export
is the same ledger, structured, across **both mandates** and **all currency
sub-accounts** to 2026 — reconciling *exactly* to the PDF parser (284/284 cash
lines, signs included) and the sidecars (0 missing). A strictly better, current
completeness source. Decisions: **keep-latest** retention; **wire into rebuild**.
Parsing uses stdlib `csv` — no `openpyxl`, no licence-hygiene impact.

## Part A — CSV as a completeness input

**A1.** `parse_cash_statement_csv(path) -> list[CashLine]` in
`statement_completeness.py`: read **cp1252** (the `°` in `N° de transacción` is
byte `0xb0` — UTF-8 crashes), delimiter `;`, `newline=''` (CRLF). Map each row →
`CashLine` (`portfolio = _sanitise_portfolio(Account nr.)`, currency,
book/value dates from `YYYY/MM/DD`, description, `amount = Decimal(Net amount)`
**already signed**, `running_balance = Decimal(Balance)`). Keep the
`prev + net == balance` self-check per `(portfolio, currency)` in value-date
order → raise `StatementParseError` on a break (preserves the PDF parser's
guarantee). Reuse `_to_decimal` / `_sanitise_portfolio`.

**A2.** Add `"limit_extension"` to `_NON_CURRENT_ACCOUNT_DOCTYPES`
(`statement_completeness.py:320`): a limit-extension advice is `Net amount =
0.00` (cash-neutral) but not excluded, so a full-range diff would flag the
2025/2026 ones as spurious `UNMATCHED_IN_LEDGER`. Latent today (the 4 PDFs
predate any limit extension); correct regardless of the CSV.

**A3.** Refactor `_run_completeness` (`cli/_main.py:412`) to iterate
**(portfolio, lines, period) groups**. Helper `_completeness_groups(path)`:
`.csv` → parse, group `CashLine`s by portfolio, `period = (min, max
value_date)` per group; `.pdf`/`.txt` → existing single group. Loop body (diff,
key `<portfolio>-<period-end>`, write summary/findings, accumulate) reused
per group. `diff` / `sidecar_cash_events` / `render_*` untouched. `period` end =
max value_date → settlement-timing events fall out-of-period, tallied not
flagged (matches the manual diff).

**A4.** `completeness` CLI (`cli/reports.py:1044`): `--statement <file>.csv`
already flows through (suffix-detected in the worker). Extend
`_discover_financial_statements` to also pick up
`Cash_statements_by_value_date_*.csv`. Update docstring.

## Part B — Archive the CSV (keep latest)

**B1.** `archive.file_cash_statements(csv_paths, dest_root, *, dry_run) ->
list[FilingPlan]`: filename-recognised, **not** through
`file_documents`/classifier/`load_pdf` (not a PDF). Dest
`<root>/cash-statements/Cash statement by value date <YYYYMMDD>.csv`
(`<YYYYMMDD>` = max value_date via `parse_cash_statement_csv`). Keep-latest
supersede (reuse tax-report `_superseded/`, never delete): differently-dated
existing → move to `_superseded/`; byte-identical same-name → `skip`; differing
same-name → replace. Returns `FilingPlan`s.

**B2.** `ImportStep.cash_statement_globs: list[str]` (`batch_config.py:363`). In
`_run_import` (`cli/rebuild.py:235`), after the PDF `file_documents` call,
expand + call `file_cash_statements`; fold counts into the summary.

**B3.** Config: local (gitignored) `banking-pipeline.toml` **and** tracked
`banking-pipeline.example.toml`:
- `[import] cash_statement_globs = ["~/Downloads/Cash_statements_by_value_date_*.csv"]`
- `[post.completeness] enabled = true`, `statements =
  ["~/Dropbox/2-areas/26-pictet/cash-statements/*.csv"]`.

## Tests

Scrubbed cp1252 fixture CSV (placeholder accounts `K-999999.001`/`P-999999.002`,
invented amounts): parser fields + signed amounts + self-check (break raises) +
multi-portfolio grouping + period synthesis; `sidecar_cash_events` returns `[]`
for `limit_extension`; `completeness` over the fixture → one summary per mandate,
0 missing; `file_cash_statements` dest + keep-latest supersede. Reuse
`test_cli_prices` / archive test harnesses.

## Verification (real data)

1. `completeness --statement <real value-date CSV>` → **0 missing**; unmatched
   only out-of-period settlement-timing, **0** `limit_extension`. Cross-check
   the manual `Order nr.` diff.
2. Import dry-run → files to `<archive>/cash-statements/…`; second differently-
   dated run supersedes.
3. `rebuild` with `[post.completeness]` → both mandates' reports land.
4. DoD: ruff / mypy / pytest clean; PII guard clean; `code-reviewer`; no new deps.

## Non-goals

Value-date CSV only (booking-date and `.xlsx` not wired). Reconciliation input,
never an ingest source — archive-only, like the tax reports.

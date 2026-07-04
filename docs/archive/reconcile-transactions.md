# `reconcile-transactions` — cross-check the sidecars against the portal Transactions CSV

**Status:** ✅ Shipped 2026-07-04. Verified on the real export and a full
rebuild — 0 missing / 0 unmatched / 0 amount-mismatch both mandates (626 K +
140 P matched, 9 FX-opens excluded); `[post.reconcile_transactions]` gates the
rebuild (exit 0). See CHANGELOG.

## Context

`completeness` (just shipped) validates the *cash* subset of the ledger — the
current-account movements — but deliberately excludes the securities legs
(switches, settlements, FX forwards, in-specie). So nothing cross-checks that
the **securities trades themselves** — the events that build the section-104
pool and drive CGT — are all ingested and correctly extracted. A missing or
mis-extracted trade → wrong pool → wrong tax, the highest-stakes error class.

`reconcile-transactions` closes that gap: it diffs the ingested sidecars against
Pictet's portal **Transactions** CSV export (every trade leg, both mandates,
`Order nr.` join key), catching a trade the broker booked but the pipeline
didn't ingest (or mis-valued). This session's one-off run already proved it out
(0 gaps; 441 single-leg trades agreed to the cent). It reuses the pattern and
plumbing just built for the cash-statement CSV. (Named to pair with the
balance-level `reconcile` command; the transaction-level counterpart of the
cash-level `completeness`.)

Decisions taken: **gate on MISSING** (a trade in the export not in the
sidecars fails the rebuild; UNMATCHED fails under `--strict`); **presence +
single-leg amount** (also assert the export cash amount == sidecar amount to
the cent on single-leg orders). Keep-latest archive + rebuild wiring, mirroring
completeness. Parsing is stdlib `csv`, cp1252 — no new dep.

## New module: `src/banking_pipeline/transactions_export.py`

Mirrors `statement_completeness.py` but ID-keyed (exact `Order nr.` match, no
date tolerance). Reuses the **public** portfolio helpers from
`statement_completeness` (`lettered_portfolio_map`, `resolve_portfolio`,
`portfolio_is_known`) so the CSV's bare `Account nr.` resolves to the lettered
sidecar portfolio; the trivial CSV helpers (cp1252 read, `YYYY/MM/DD`→ISO,
`Decimal`, sanitise) are ~5 lines, inlined to avoid cross-module private imports.

- `parse_transactions_csv(path) -> list[ExportRow]` — cp1252 / `;` / CRLF. Each
  row → `ExportRow(order_number, portfolio, trade_date, transaction_type,
  currency, cash_amount)` where `cash_amount = Decimal(Net amount in current
  account currency)` (may be blank on a non-cash leg → `None`).
- `reconcile(export_rows, sidecar_rows, *, portfolio, period) -> ReconcileReport`:
  - Group export rows by `order_number`. An order whose rows are all
    `transaction_type == "Forex forward open"` is **excluded** (we book FX
    forwards at settlement — the open leg is never ingested; verified the only
    export-only class after the order-number capture shipped).
  - Sidecar set = `{transaction_number}` filtered to `portfolio` (via the
    resolved lettered form) and `period`.
  - **MISSING** = ingestable export orders (in period) absent from the sidecar
    set — a trade booked but not ingested.
  - **UNMATCHED** = sidecar `transaction_number`s (in period) absent from the
    export — a phantom / duplicate ingest.
  - **AMOUNT_MISMATCH** = a matched **single-leg** order (exactly one export
    row) whose export `cash_amount` ≠ the sidecar `amount` beyond a cent.
    Multi-leg (FX) orders stay presence-only.
  - `period` = (min, max `trade_date`) across the export's rows for that
    portfolio; sidecars outside it aren't flagged UNMATCHED (out-of-window),
    matching completeness.
- `ReconcileReport` dataclass (matched, missing, unmatched, amount_mismatches,
  excluded) + `render_summary` / `render_csv`.

## Worker + CLI

- `_run_reconcile_transactions(export_paths, sidecar_dir, out_dir)` in
  `cli/_main.py` — mirror `_run_completeness`: load sidecars once, build the
  lettered map, per path group the export by portfolio (a CSV is multi-mandate),
  `reconcile` per group, write `summary-<portfolio>-<period-end>.txt` +
  `findings-<...>.csv`. Returns `(missing, unmatched, mismatches, written)`.
- `reconcile-transactions` command in `cli/reports.py`: `--transactions`
  (repeatable) / `--transactions-dir` (discovers `Transactions*.csv`, skipping
  `_superseded/` — the same fix `_discover_financial_statements` got),
  `--source` (default `data`), `--out` (default `reconcile_transactions_dir` =
  `reports/reconcile-transactions`), `--strict`. Exit non-zero on any MISSING or
  AMOUNT_MISMATCH; `--strict` also on UNMATCHED.

## Archiving (keep-latest)

- Factor the keep-latest loop out of `archive.file_cash_statements` into a small
  internal `_file_keep_latest(csv_paths, dest_dir, stem, date_of, *, dry_run)`
  (reusing the existing `_supersede` / `_same_bytes`); `file_cash_statements`
  calls it (its tests confirm no regression). Add `archive.file_transactions_csv`
  → `<root>/transactions/Transactions <YYYYMMDD>.csv`, `<YYYYMMDD>` = max trade
  date via `parse_transactions_csv`. Not through the PDF classifier.
- `ImportStep.transactions_globs` (mirror `cash_statement_globs`); `_run_import`
  expands + calls `file_transactions_csv`.

## Config + rebuild wiring

- `PostSteps.reconcile_transactions: ReconcileTransactionsStep` (`enabled`,
  `statements` glob, `strict`) — new step in `batch_config.py`. Wire into
  `cli/rebuild.py` after the completeness step: `_run_reconcile_transactions(...)`,
  MISSING/mismatch fails, UNMATCHED under strict.
- Local `banking-pipeline.toml` + tracked `banking-pipeline.example.toml`:
  `[import] transactions_globs = ["~/Downloads/Transactions_*.csv"]` and
  `[post.reconcile_transactions] enabled = true`, `statements =
  ["~/Dropbox/2-areas/26-pictet/transactions/*.csv"]`.

## Tests

Scrubbed cp1252 fixture Transactions CSV (placeholder accounts, invented
amounts, ≥1 single-leg securities order + a 2-leg FX order + a Forex-forward-open
row): parser fields; `reconcile` → clean case, a MISSING (drop a sidecar), an
UNMATCHED (extra sidecar), an AMOUNT_MISMATCH (perturb one amount), FX-open
excluded, out-of-period not flagged; `file_transactions_csv` keep-latest +
`_file_keep_latest` regression on `file_cash_statements`. Reuse the
`test_statement_completeness` / `test_cli_import` harness patterns.

## Verification (real data)

1. `reconcile-transactions --transactions <real Transactions CSV> --source data`
   → expect **0 missing, 0 unmatched, 0 amount-mismatch** (excluding the 9
   Forex-forward opens), matching this session's manual `Order nr.` diff.
2. Import dry-run → files to `<archive>/transactions/Transactions <YYYYMMDD>.csv`;
   a newer export supersedes.
3. Full `rebuild` with `[post.reconcile_transactions]` enabled → passes (exit 0).
4. DoD: ruff / mypy / pytest clean; PII guard clean (scrubbed fixture);
   `code-reviewer`; no new deps (stdlib `csv`). Remove the `reconcile-export`
   backlog item on ship (it ships as `reconcile-transactions`); move this plan
   to `docs/archive/`; CHANGELOG entry.

## Non-goals

Multi-leg (FX) amount reconciliation (presence-only there). Booking-date and
`.xlsx` variants (value-date-agnostic here — the join is by `Order nr.`, so the
trade-date window is all that's needed). Reconciliation input, never an ingest
source — archive-only, like the cash statement.

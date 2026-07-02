# Plan: close the Pictet P-mandate balance-reconciliation hole

*Shipped.* Account numbers, balances, and holdings below are anonymised /
synthetic per the repo's PII rules (placeholder body `123456`; the real
figures live only in the gitignored `data/`).

## Goal

The Pictet **P mandate** (a leveraged Lombard account, `Assets:Pic:P<body>002`:
a large negative cash position funding off-ledger property, plus a sleeve of
~17 thematic ETFs and single stocks) was **never** cross-checked against the
ledger. Its monthly valuations use Pictet's by-name "Financial Statement"
layout, which the balance parser returned **0 rows** for — so the
`banking-pipeline.toml` claim that "Both Pictet portfolios … reconcile
cleanly" was *vacuously true* for P.

Assert P's cash **and** every by-name holding against the ledger on every
load, and make a whole-portfolio hole impossible to hide silently.

Three independent gaps let the whole portfolio hide:

1. **Parser blind spot.** P valuations print holdings *by name with no
   `ISIN:` marker* and cash rows in an expanded currency-led shape. The
   parser matched neither, returning 0 rows (the sibling K statement returned
   31). The header (as-at date + account) parsed; only the rows failed.
2. **Zero-parse invisible.** `balance_statements` already globbed the P
   monthlies, but `statement_coverage_gaps` early-returned `[]` on empty
   extraction, so the `--strict` coverage guard never saw the drop.
3. **Zero-assertion portfolio invisible.** `find_coverage_gaps` skipped any
   portfolio with `< 2` asserted months, so a portfolio asserted *not at
   all* never entered the map.

**Task-3 check (larger potential gap):** P trades **are** ingested
(`2025-P.beancount`, `2026-P.beancount`, P sidecars, `Pic-P<body>002.beancount`,
`Assets:Pic:P<body>002` refs) — so there was **no** second trade-ingestion
gap; the hole was purely balance reconciliation.

## Decisions

- **Full security coverage via an alias map.** Assert P cash *and* every
  by-name holding, resolving statement names → ledger ISIN. About half the
  holdings match the commodity `name` after case/punctuation folding; the
  rest carry long contract-note names and need an explicit `statement_names`
  alias in `commodities.toml`.
- **Guard fails under `--strict` only.** A whole-portfolio hole is reported
  always (reconcile summary + coverage output) but escalates to a nonzero
  exit only under `rebuild --strict`, matching the existing coverage-gap
  policy.

## The P "Financial Statement" layout

Synthetic illustration (real values are in the gitignored statements):

```
-30'000.00 Pound United Kingdom GBP -30'000.00 GBP -30'000.00 113.55%      <- cash (2 CCY tokens)
-54'000.00 Dollar USA USD -54'000.00 GBP -40'000.00 79.43%                 <- cash (non-GBP; orig ≠ GBP)
1'365 Acme Defense Etf A Usd USD 39.37 USD 53'740.05 GBP 42'679.63 -13.68%  <- security (3 CCY tokens, no ISIN)
1'500 Widget India Tech Etf GBP 17.48 GBP 26'221.50 GBP 26'221.50 -10.35%   <- security (GBP-denominated)
2'400'000.00 C/A Limit Gbp, <dates> - Bp Level GBP 0.00 GBP 0.00 0.00%      <- noise (2 tokens, qty ≠ val)
Equities GBP 50'000.00 -132.94%                                            <- subtotal (word-led)
```

Clean structural separation drives the parser:

- **Cash row** = leading balance, a **letters-only** currency name, **exactly
  two** `<CCY> <value>` groups, a weight. The letters-only name rejects the
  punctuated `C/A Limit` credit-limit row and every ETF name; word-led
  subtotals have no leading number.
- **Security row** = leading qty, name, **exactly three** currency tokens
  (price + orig valuation + GBP conversion), a weight. The 2-token `C/A Limit`
  row can't match.
- Both run per line alongside the K/ES patterns and are mutually exclusive by
  shape (K cash ends after one balance; K security data is multi-line).

## Changes

1. **`commodities_metadata.py`** — `statement_names: tuple[str, ...]` on
   `CommodityMetadata`; `normalise_security_name()` (case/punctuation folding)
   and `build_statement_name_index()` → `{normalised name: ISIN}` (auto-matches
   the names that already agree, aliases fill the rest, raises on an ambiguous
   collision).
2. **`balances_extract.py`** — `_FS_CASH_ROW_RE` (2-CCY) and
   `_FS_SECURITY_ROW_RE` (3-CCY); a `name_to_isin` map threaded through
   `_pictet_balances` / `extract_balances_from_statement` /
   `statement_coverage_gaps` / `coverage_report` / `generate`. Security rows
   resolve name→ISIN; an unresolved name emits **no** assertion and is
   reported, never silently guessed.
3. **Coverage guard** — new `CoverageGap` kinds `unresolved-holding` (missing
   `statement_names` alias) and `empty-statement` (a Pictet statement with a
   **non-zero portfolio total** that extracted nothing — a whole-statement
   drop), plus a loose P-format cash re-detector.
4. **`reconcile.py`** — `parse_ledger_portfolios()` (from `open` directives,
   scoped to `Assets:Pic:` / `Assets:Vgd:`), `find_missing_portfolios()`,
   `ReconReport.missing_portfolios`, and a MISSING PORTFOLIO summary block.
5. **CLI** — `_resolve_name_to_isin()` wired into the balances step; the
   reconcile step reads `data/portfolio.beancount` opens and passes
   `ledger_portfolios` to `build_report`; `--strict` escalates the new gaps.
6. **Data + docs** — `statement_names` documented in
   `commodities.example.toml` and added to the real (gitignored) P commodity
   entries whose short valuation name differs from their stored `name`; the
   vacuous `banking-pipeline.toml` comment corrected; `docs/architecture.md`
   updated.

## Deviations discovered during verification

The interesting engineering — each found by running against the real archive:

1. **Clamped-weight variant.** Opening months print the weight column as
   `> 999.99%` / `< -999.99%` (the leveraged base makes weights off-scale); the
   regexes needed a `_WEIGHT` fragment allowing a leading `<`/`>`. The
   **empty-statement guard caught this** on the opening statement during dev.
2. **empty-statement scoped tighter than planned.** "Recognised valuation →
   flag" false-positived on legitimately-empty statements (a freshly-opened
   Pictet account with a zero portfolio total; a post-liquidation Vanguard
   ISA). Final rule: fire only for a **Pictet** header with a **non-zero
   portfolio total** that extracted nothing. Vanguard emptiness is real (its
   own `parse_isa_valuation` succeeds with zero holdings), not a drop.
3. **FS-security ISIN-proximity deferral.** An FS security row defers to the K
   ISIN path when an ISIN marker is within 3 lines, so an ISIN-anchored
   holding is never mis-keyed by a name guess.
4. **Accrued-interest cash column.** Mid-quarter, a P cash row prints the
   *booked* balance in the Quantity column and *booked + accrued* in the
   Valuation-Orig column (they diverge). The parser must assert the **booked
   quantity** — the ledger holds no accrued interest. The initial `qty ≈ val`
   symmetry guard both asserted the wrong column *and* dropped the whole row
   when they diverged, silently losing a month's P cash (invisible to the
   loose cash guard too). Fixed by asserting the quantity column, dropping the
   guard (the row shape suffices), and adding the loose P-cash re-detector.
   Whole-unit rounding of the quantity is covered by `render`'s `~ 0.5` fiat
   tolerance.

## Non-goal (flagged, not fixed)

- **`prices_extract` shares the blind spot** — it also keys on per-ISIN price
  rows, so P valuation *prices* are dropped. P holdings still get
  trade-derived prices, so this is a smaller gap. Follow-up.

## Outcome

End-to-end over the full statement archive: **bean-check passes, zero drift
across every assertion, P reconciles cleanly, zero coverage gaps, no missing
portfolio.** The P mandate — never checked before — reconciles. Verification
loop clean: `ruff`, `mypy src`, `pytest`, `check_pii --all`; `code-reviewer`
run with no Critical findings.

# Plan: `holdings` cost-basis report (pluggable per-jurisdiction basis)

**Status:** COMPLETE (Stages 1–4 shipped + reviewed). Stage 5 (the ES/EUR
lens) folded to the backlog rather than built — a bare stub adds no working
capability and the *source* for a EUR cost basis wants rethinking (parsing
Pictet's Spanish tax reports vs. reading the cost/unrealised columns already in
the monthly statement — see the backlog "Financial reporting" note). The UK
report is the shipped deliverable.

- **Stage 4 ✅** — CLI `holdings` command in `cli/reports.py` (mirrors
  `concentration`; `--source` sidecars + `--basis uk|es` + `--opening-positions`;
  `es`/unknown basis exit 2). Builds a `UkSection104Lens` from ISA-excluded
  sidecars + opening positions and the exact-month `rates` (which
  `value_holdings` forward-fills for the mark — satisfying the "tax exact-month
  / valuation forward-fill" split from one source object). Default-off
  `[post.reports] holdings` toggle (`batch_config.py` + `cli/rebuild.py`) and a
  `holdings_reports_dir` setting. Docs: README, architecture (CLI + config),
  design-decisions (pluggable-basis note). Full suite 1023 green; `holdings
  --help` renders.
  - **ERI wired in** (was briefly deferred, then completed on request). Added
    `cumulative_base_cost_adjustments` (eri.py): `compute_eri` scopes to one
    tax year, but the pool is cumulative, so it runs per year across the whole
    `eri` table and merges the adjustments. CLI + rebuild pass them to the
    lens; a GBP-rate gap on any ERI entry warns (cost basis omits that uplift).
    Tests: cumulative-merge (test_eri.py) + lens-applies-uplift (test_basis.py).
    *Adjacent observation, not acted on:* `_compute_tax_year` (cli/tax.py)
    passes only the disposal year's ERI adjustments to `match_history` / a
    year-scoped `compute_sa108`, so a disposal's pool may miss **prior**-year
    ERI uplift — worth a separate check.
  - **Deviation — no separate golden file.** Rendering is covered by
    `test_holdings.py` unit tests + a CLI end-to-end write test (Vanguard
    fixture), matching the `concentration` precedent (assertion-based, not a
    committed MD/CSV golden).

- **Stage 3 ✅** — `holdings.py`: `join_holdings(valuation, basis_map)` is the
  testable core (joins `ValuationResult.securities` with per-ISIN
  `HoldingBasis` by key, computes unrealised, totals, the statement-qty ↔
  pool-qty drift cross-check, and an unmatched-basis list); `build_report`
  wraps it with statement parsing + latest-per-portfolio. `render_markdown` /
  `render_csv_rows` model `concentration.py`. Cost/unrealised totals cover
  matched-basis holdings only; market-value total covers all. ISA/ticker
  holdings render with a blank cost (ticker ≠ ISIN — the known MVP gap the ES
  lens will close). Tests in `test_holdings.py` (join, drift, unmatched, loss,
  MD/CSV, end-to-end via the Vanguard fixture + stub lens). Full suite 1020
  green.
  - **Deviation:** report is securities-only — no cash or property (cost basis
    is a securities concept), unlike `concentration` which folds both in.

- **Stage 2 ✅** — the `BasisLens` seam + UK section-104 lens. Two deviations
  from the sketch below, both to keep the seam pluggable:
  - **Split the seam from the lens.** `basis_lens.py` holds the
    jurisdiction-neutral `BasisLens` protocol + `HoldingBasis` and imports no
    tax code; the UK lens lives in `tax/uk/basis.py` (`UkSection104Lens`,
    wrapping `match_history` → `residual_pools`, GBP, `market_value=None`).
    A single file would have coupled the "neutral" seam to `tax.uk` now and
    `tax.es` later. The Stage 5 ES stub goes in its own module too, not
    `basis_lens.py`.
  - **`basis_for()` takes no argument** (sketch showed `basis_for(holdings)`).
    The UK lens derives quantity from the pool and the future ES lens from a
    parsed report — neither needs the statement holdings passed in. The report
    joins by ISIN in Stage 3.
  - Tests in `test_basis.py`: protocol conformance + identity, held holding
    with GBP cost (fully-disposed omitted), DDS included. Full suite 1012
    green, mypy/ruff clean.

- **Stage 1 ✅** — `section_104.py`: extracted the full matching into a private
  `_match_all` returning `(records, PoolState)`; `match_disposals` is now a
  thin wrapper (unchanged signature/behaviour), plus new
  `match_disposals_with_residual` and `residual_pool`, and a `PoolState`
  (qty + unrounded pooled GBP cost). `sa108.py`: `match_history` now populates
  `MatchedHistory.residual_pools: dict[str, PoolState]` from one matching pass
  per ISIN (covers deeply-discounted holdings too; fully-disposed → qty 0).
  Behaviour-preserving: full suite (1009) + sa108/tax goldens clean; new unit
  tests in `test_section_104.py` (residual: partial/full/over-disposal,
  same-day/B&B exclusion, ERI uplift, sub-penny precision) and
  `test_sa108.py` (residual_pools tracks current holdings incl. DDS).

## Goal

A `holdings` report: current holdings with **market value** (statement mark)
and **cost basis + unrealised P&L**, where cost basis is supplied by a
**pluggable per-jurisdiction lens**. Ship the **UK section-104 (GBP)** lens;
leave a documented, unimplemented **ES (EUR/Spanish)** slot for when the
Pictet-P&L parser lands.

Secondary payoffs:

- Doubles as a **statement-qty ↔ pool-qty cross-check** (a reconciliation
  dividend, in the spirit of `completeness` and the P-mandate reconcile fix).
- Delivers the cost-basis half of the deferred **balance-sheet phase 4**
  (cost-basis / unrealised-P&L column).
- De-risks the larger **CGT year-end harvesting advisor** — its hard, novel
  substrate is exactly this per-holding unrealised table.

## Why now / why this shape

The tax-*outcome* engines already exist and are reusable: the section 104
pool + all three matching rules (`section_104.py`), the AEA config
(`config.py`), the allowance stack (`cgt_allowance.py`). What does **not**
exist is the forward-looking input — current holdings marked to cost basis.

The residency motivation: UK→Spain tax residence may change at some point.
Spain uses **per-lot FIFO in EUR**, not UK averaged pooling, and does **not**
recognise the ISA wrapper (a Spanish resident owes CGT on ISA gains). So a
second cost basis is a real future requirement, not gold-plating — but it has
**opposite provenance** from the UK one:

- **UK section-104 GBP** — *computed* from the sidecars by the existing
  engine. Free.
- **EUR/Spanish FIFO** — *parsed* from Pictet's unrealised P&L report (the
  "parse Pictet's tax reports" backlog item), because computing it means a
  second matching engine the repo deliberately avoids. Plus a *computed*-EUR
  tail for the ISA, which Pictet doesn't cover (Vanguard issues no Spanish
  report).

Therefore the design makes cost basis a **pluggable lens** so the UK column
ships now and the EUR column slots in later without reworking the report.

## The seam

```
BasisLens (protocol)
    name: str                     # "uk-s104" | "es-fifo"
    currency: str                 # "GBP" | "EUR"
    basis_for(holdings) -> dict[isin, HoldingBasis]

HoldingBasis:
    isin: str
    held_qty: Decimal
    cost_amount: Decimal
    currency: str
    market_value: Decimal | None  # None → report uses the statement mark (GBP).
                                  # A non-GBP lens supplies its own market value
                                  # (statement-date FX differs between lenses).
```

- **UK lens** (`UkSection104Lens`): held_qty + pooled cost (GBP) from the
  residual section-104 pool; `market_value=None` → the report joins the
  statement GBP mark. Same-currency, clean unrealised.
- **ES lens** (`EsSpanishLens`, Stage 5, *stub only*): would return qty + EUR
  cost + EUR market value straight from the parsed Pictet unrealised report —
  mixed-provenance, with a *computed*-EUR tail for the ISA.

## Code review (Stages 2–4)

`code-reviewer` run on the Stages 2–4 diff (Stage 1 reviewed separately, clean).
Findings addressed:

- **H1 (High) — same ISIN across two Pictet mandates double-counted.**
  `value_holdings` emits one row per statement portfolio, but the section 104
  pool is NIF-level (account-blind), so a fund in both K and P printed the full
  pool cost on *each* row, double-counted the totals, and false-flagged drift
  (partial qty vs full pool). Fixed: `join_holdings` now consolidates
  securities by key (`_aggregate_by_key`) before the basis join — one row per
  ISIN facing the pool once. Regression test added. (Verified latent, not live:
  no ISIN currently spans both mandates in the latest statements — the fix is
  defensive, correct the day one does.)
- **M1 (Medium) — rebuild dropped ERI rate-gap warnings.** The CLI warned; the
  rebuild block discarded them. Fixed: rebuild emits the same warning.
- **L1 (Low) — GBP-only renderer.** `build_report` now rejects a non-GBP lens
  (`NotImplementedError`) so the future ES lens can't silently render a EUR
  value as £. Test added.
- **L2 (Low) — `_QTY_TOL` comment** reworded (symmetric agreement tolerance,
  not sa108's one-sided over-disposal guard).

All clean afterwards: ruff / mypy / pytest (1027) green.

## Stages

### Stage 1 — expose the residual section-104 pool *(load-bearing)*

`match_disposals` (`section_104.py:126-234`) computes `pool_qty` / `pool_cost`
internally (lines 180-227) but returns only the `MatchedDisposal` list — the
terminal residual is discarded.

- Refactor the pool replay (lines 170-228) into a shared helper returning
  `(matched_list, PoolState(qty, cost_gbp))`. `match_disposals` keeps its
  signature and discards the residual — **zero behaviour change**.
- Add `residual_pools(txns, opening, eri, …) -> dict[isin, PoolState]`
  mirroring `match_history`'s per-ISIN grouping (`sa108.py:139-206`): group
  trades by ISIN, fold in opening positions + ERI cost adjustments, replay,
  read the terminal pool. A fully-disposed ISIN yields no entry (or qty 0).
- **Tests:** unit tests for residual qty/cost across opening positions, ERI
  uplift, partial and full disposal. **Assert the sa108 goldens diff clean** —
  this is the guard that the refactor is behaviour-preserving.

**Risk:** the only stage touching load-bearing tax code. Mitigated by the pure
refactor + golden diff. Do not change matching order or rounding.

### Stage 2 — the seam + UK lens

- Define `BasisLens` protocol + `HoldingBasis` (new `basis_lens.py`).
- `UkSection104Lens` wrapping `residual_pools` → `HoldingBasis(held_qty,
  cost_amount=pool_cost, currency="GBP", market_value=None)`.
- Unit tests for the lens over a small scrubbed sidecar fixture.

### Stage 3 — report module (`holdings.py`)

Modelled on `concentration.py:57-102` (`build_report` → `render_markdown` /
`render_csv_rows`, converging on the statement/valuation path).

- Load statements via the existing `_load_statement_context` / `value_holdings`
  path (`valuation.py:178-270`) → per-ISIN held qty + market value (GBP).
- Join with the selected `BasisLens` per ISIN; compute
  `unrealised = market_value − cost_basis` (both GBP for the UK lens).
- **Cross-check:** statement qty vs pool residual qty; emit a drift warning
  where they disagree (surfaces a missing trade confirmation or an ingest gap).
- `render_markdown` + `render_csv_rows`. Columns: portfolio, ISIN, name, qty,
  market value (GBP), cost basis (GBP), unrealised (GBP).
- **Report-level caveat line:** cost basis is UK section-104 GBP; ISA holdings
  are shown with a UK cost basis but are UK-tax-exempt — a Spanish-resident
  lens would tax them.

### Stage 4 — CLI + wiring

- `@app.command("holdings")` in `cli/reports.py:52+`, following the
  `concentration` shape (`--statements`, `--statements-dir`,
  `--statements-recursive`, `--out`, `--commodities`, `--strict`, `--verbose`).
- **`--basis uk|es` flag** (default `uk`): selects the lens. `es` raises a
  clear "not yet implemented — blocks on the Pictet-P&L parser" error until
  Stage 5. Defining the flag now makes the seam visible and fixes the CLI
  surface so the ES lens is purely additive.
- Optional default-off `[post.reports] holdings` rebuild toggle
  (`batch_config.py` + `cli/rebuild.py`), matching the `net_worth_monthly`
  precedent.
- Golden MD/CSV over a scrubbed fixture.

### Stage 5 — ES slot *(deferred stub, documented not built)*

- Land `EsSpanishLens` as an interface + docstring only. Document: source =
  parsed Pictet unrealised P&L (**blocks on** the "parse Pictet's tax reports"
  backlog item); EUR/Spanish-FIFO; consolidated at NIF level (no portfolio
  dimension); ISA basis **computed** (GBP cost → EUR at trade-date FX), not
  parsed. `--basis es` stays a clean error until this ships.

## Invariants / risks

- **Tax math reads sidecars, not the ledger** ✅ — cost basis comes from the
  section-104 engine over the JSONL sidecars, never from beancount text.
- **No ISA choke-point filter here** — deliberate. This is a holdings/net-worth
  view, not a tax report; ISA holdings are *included* and labelled (foreshadows
  the ES lens where they're taxable). Call this out in the code + review so it
  is not mistaken for a choke-point violation.
- **Stage 1 touches load-bearing code** — the only real risk; mitigated by the
  pure refactor + unchanged sa108 goldens.
- **Price freshness bounded by statement cadence** — same limitation the
  existing valuation reports carry; state it on the report, don't solve it.
- **PII** — scrubbed fixtures + allow-listed placeholders; no real
  amounts/holdings in goldens, docs, or the CHANGELOG line.

## Files touched

- Stage 1: `section_104.py`, `sa108.py`
- Stage 2: new `basis_lens.py` (neutral seam) + new `tax/uk/basis.py` (UK lens)
- Stage 3: new `holdings.py`
- Stage 4: `cli/reports.py`, `batch_config.py`, `cli/rebuild.py`
- Stage 5: `basis_lens.py` (stub) + `cli/reports.py` (flag error path)
- Throughout: tests, fixtures, goldens; docs (README command, `architecture.md`
  CLI/config, a `design-decisions.md` note on the pluggable-basis choice).

~6 core files → plan-mode-appropriate.

## Open decisions (resolved)

1. **Command name:** `holdings`.
2. **ISA inclusion:** include-and-label under the UK lens (not excluded).
3. **`--basis` flag:** defined from Stage 4 (`uk` default, `es` errors until
   Stage 5).

## Definition of Done

- `uv run ruff check .` / `uv run mypy src` / `uv run pytest` clean, with new
  tests for residual pools, the UK lens, and the report renderers.
- sa108 goldens diff clean (Stage 1 refactor is behaviour-preserving); new
  holdings golden reviewed.
- `uv run banking-pipeline check` (or `rebuild --strict`) clean — no new
  bean-check errors, no new reconcile drift.
- `code-reviewer` subagent run on the diff, no Critical findings outstanding.
- CLAUDE.md invariants hold (sidecars-not-ledger; no `import beancount`; no
  `Equity:Uncategorized`).
- `python3 scripts/check_pii.py --all` clean.
- Docs updated (README, architecture, design-decisions); backlog line moves to
  CHANGELOG on ship; this file's status advanced per stage.

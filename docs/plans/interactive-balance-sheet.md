# Plan: interactive balance sheet

## Goal

A `banking-pipeline balance-sheet` command that produces a **single,
self-contained HTML file** — an offline, shareable balance sheet you can
scrub to *any* date: pick an "as of", and the page recomputes every
account's value, the Assets / Liabilities / net-worth totals, a
collapsible account tree, and an allocation chart, entirely client-side.

One artifact, no server, no rebuild to change the date. It answers "what
did I own, and what was it worth, on date *X*?" for the whole book —
every bank account, every ISA, and on-ledger property — in the
operating currency (GBP).

This is a **ledger construct** (the full chart of accounts, including the
equity/liability side), so — like [trial_balance.py](../../src/banking_pipeline/trial_balance.py)
and unlike the statement-valuation reports — it reads the **ledger via
`bean-query`**, not statement marks. See [design-decisions.md](../design-decisions.md)
for why the ledger-vs-statement split exists.

## Background

### Why bean-query, not a hand-rolled parser

A balance sheet needs *booked, balanced* postings across every account.
`bean-query` gives exactly that: beancount's own loader runs booking
(FIFO lot matching) and balances the implicit Income/Expense legs before
the query sees a row. We get the authoritative numbers for free, and we
stay on the right side of the GPL boundary by shelling out — never
`import beancount` — exactly as [bean_query.py](../../src/banking_pipeline/bean_query.py)
and [bean_check.py](../../src/banking_pipeline/bean_check.py) already do.

The decisive advantage over re-parsing `data/*.beancount` ourselves:
beancount auto-balances postings that omit an amount (the Income/Expense
legs), and a text parser can't see those amounts — it would have to
*interpolate* them per transaction and hope the totals close.
`bean-query` returns them already booked. **No interpolation, no
divergence from `bean-check`'s truth.**

### Why client-side as-of

The interactivity is the point, and a server round-trip per date kills
it. Instead: **query the raw postings once**, ship them to the browser,
and let the page aggregate. The key enabler is that a *market-value*
balance sheet needs only **unit sums** per `(account, commodity)` up to
the as-of date — and FIFO booking never changes the *total* units held,
only which lot a sale draws from. So summing posting units client-side is
exact; we don't need beancount to re-book per date. (Cost basis *is*
date-dependent — see Non-goals / phase 4.)

### Prior art

A throwaway prototype exists under `reports/balance-sheet/` (git-ignored).
This plan **supersedes it from scratch** and deliberately does not build
on it; its three lessons, captured so we don't repeat them:

1. it hand-rolled a beancount parser (the interpolation trap above) —
   we use `bean-query`;
2. it loaded Chart.js from a CDN, so the "offline" artifact wasn't —
   we vendor the chart code;
3. its `.gitignore` listed the data JSON but **not** the built HTML,
   which inlines the same data — we ignore both.

## Design

The shape mirrors the other reports: a pure core module + a thin CLI
command + config + rebuild wiring. Two build stages, like the prototype
but as testable functions.

### 1. Data extraction — `balance_sheet.py`

A new module owning the dataset. One `bean-query` call for the postings,
assembled with the existing price / commodity / FX inputs into a
serialisable model.

```python
@dataclass(frozen=True)
class BalanceSheetData:
    operating_currency: str            # "GBP"
    as_of_min: date
    as_of_max: date
    postings: tuple[Posting, ...]      # raw per-leg: (date, account, qty, commodity)
    prices: dict[str, tuple[PricePoint, ...]]   # commodity/ccy -> [(date, price, ccy)]
    commodities: dict[str, CommodityInfo]       # description, asset_class, domicile
    assertions: tuple[Assertion, ...]  # optional drift overlay (date, account, qty, ccy)

def build_data(
    ledger: Path, *,
    commodities: dict[str, CommodityMetadata],
    rate_source: GbpRateSource,
) -> BalanceSheetData: ...

def to_json(data: BalanceSheetData) -> str: ...   # compact, browser-facing
```

Inputs:

- **Postings** — one ungrouped `bean-query`:
  `SELECT date, account, units(position) WHERE ... ORDER BY date`,
  keeping only dated, real postings (Assets / Liabilities / Equity /
  Income / Expenses — the balance-sheet side is derived from all of
  them). Reuse [`bean_query.run_query`](../../src/banking_pipeline/bean_query.py);
  parse amounts with the same helper the trial balance uses
  ([`trial_balance.parse_amounts`](../../src/banking_pipeline/trial_balance.py)).
- **Prices** — the ledger's price database (the pipeline's `prices` step
  writes `data/prices.beancount`: per-ISIN/ticker marks and currency→GBP
  rates). Plus GBP FX for any currency the ledger doesn't price, from the
  configured `GbpRateSource` ([fx/gbp_rates.py](../../src/banking_pipeline/fx/gbp_rates.py)) —
  the same source `concentration` / `net-worth` use, so values tie out
  with those reports where scope overlaps.
- **Commodities** — `description` / `asset_class` / `domicile` from
  [`commodities_metadata.load_commodities`](../../src/banking_pipeline/commodities_metadata.py),
  so the tree can group and colour by asset class.
- **Assertions** (optional) — `data/balances.beancount`, to overlay
  statement-vs-ledger drift (phase 4).

The module is **pure** apart from the one `run_query` shell-out, which is
injected as a `Path` — so the transform (BQL rows → `BalanceSheetData` →
JSON) is unit-testable with fixture rows and no binary.

### 2. The artifact — template + inliner

- `templates/balance_sheet.html` — a **committed** static template:
  layout, CSS, the vanilla-JS valuation/tree/chart logic, and a single
  `__DATA_PLACEHOLDER__` token. No data, no real account numbers.
- `render_html(data: BalanceSheetData) -> str` in `balance_sheet.py` —
  reads the template and substitutes the inlined JSON for the token,
  producing the standalone file. (Mirrors the prototype's `build_artifact`
  step, but as a tested function, not a loose script.)
- **Chart library vendored**, not CDN: commit a pinned, minified
  MIT-licensed chart lib (or hand-roll a dependency-free SVG donut — see
  Open questions) under `templates/` and inline it too, so the output is
  genuinely offline. Record the licence in the README's "Libraries and
  licenses" section.

The client-side JS, for a chosen as-of date:

1. sum posting units per `(account, commodity)` where `date ≤ as-of`;
2. value each holding: `qty × (latest price ≤ as-of)`, chaining
   commodity→native→GBP where needed (the trial balance's valuation
   semantics, moved to the browser);
3. fold into the Assets / Liabilities / net-worth totals (below) and
   render the collapsible tree + allocation chart;
4. a holding with no price ≤ as-of is shown **flagged, not zero** (the
   `missing_prices` / `rate_gaps` discipline the valuation reports use).

### 3. Balance-sheet semantics — reuse the net-worth vocabulary

This book has **no `Liabilities:` tree**: the Lombard / margin loan is a
**negative cash balance** on an `Assets:…:<CCY>` sub-account — a
deliberate, source-faithful choice (see
[design-decisions.md → "The Lombard loan is negative cash, not a
liability"](../design-decisions.md)). So the presentation reuses the
[net_worth](../../src/banking_pipeline/net_worth.py) /
[concentration](../../src/banking_pipeline/concentration.py) framing
rather than inventing accounting buckets:

- **Assets** = positive security values + positive cash (gross long);
- **Liabilities** = the negative cash balances (the loan), shown by
  currency;
- **Net worth** = Assets − Liabilities = the figure the net-worth report
  already produces.

Equity / Income / Expenses are *not* shown as wealth (cumulative flows;
a spot-rate single-currency total would be meaningless — the trial
balance documents this). They're available in the dataset for a future
"where did the money come from" view, but out of scope for the sheet.

Includes ISA + on-ledger property (full wealth) — unlike `tax-report`,
no wrapper filter. Property appears only if `main.beancount` includes
`data/property.beancount` (same caveat as the trial balance).

### 4. CLI command — `balance-sheet`

A new command in [cli/reports.py](../../src/banking_pipeline/cli/reports.py),
mirroring `trial-balance`:

```
banking-pipeline balance-sheet [LEDGER] [--out DIR] [--rate-source ...]
                               [--commodities ...] [--open]
```

- `LEDGER` defaults to `main.beancount` (the tolerance-bearing root).
- `--out` defaults to `settings.balance_sheet_reports_dir`
  (`reports/balance-sheet`).
- Writes `balance-sheet.html` (the artifact) and, for debugging /
  reuse, `balance-sheet-data.json`.
- A missing `bean-query` binary is a **warning, not an error** (degrade
  like the trial balance / mandate scorecard via `QueryResult.binary_missing`).
- `--open` (optional) opens the file in the browser after writing.

### 5. Config + rebuild

- `Settings.balance_sheet_reports_dir: Path = Path("reports/balance-sheet")`
  in [config.py](../../src/banking_pipeline/config.py), alongside the
  other `*_reports_dir`.
- `ReportsStep.balance_sheet: bool = False` in
  [batch_config.py](../../src/banking_pipeline/batch_config.py) (opt-in,
  like `trial_balance` / `mandate_scorecard` — it needs `bean-query`).
- Wire into `_run_rebuild_reports`
  ([cli/rebuild.py](../../src/banking_pipeline/cli/rebuild.py)): add a
  `balance-sheet` entry to the `wanted` list and a build block that uses
  the same `trial_balance_ledger` (→ `[post.check]` ledger) the scorecard
  uses. Document the toggle in `banking-pipeline.example.toml`.

### 6. PII — the artifact is personal data

The JSON and the HTML carry real account numbers, balances, and holdings.

- **Git-ignored:** `balance-sheet-data.json` *and* `balance-sheet.html`
  (the inlined artifact). Add both to a nested
  `reports/balance-sheet/.gitignore` — and note that today the repo-root
  `.gitignore` blanket-ignores `reports/` anyway, so this only bites if
  that ever changes.
- **Committed:** `balance_sheet.py`, `templates/balance_sheet.html` (the
  data-free template), and the vendored chart lib.
- The template must use a **placeholder** account in any example/comment
  (`K123456001`, per the fixtures convention). The improved
  `scripts/check_pii.py` (now scans explicit paths, incl. git-ignored
  files) is the pre-commit backstop.

## Data-model / writer impact

None. No `Transaction` / sidecar / writer change — this is a read-only
reporting feature over the existing ledger. New code is confined to
`balance_sheet.py`, the template, the CLI command, and config.

## Testing

- **Transform unit tests** (`tests/test_balance_sheet.py`, pure): feed
  fixture `bean-query` CSV rows (no binary) → assert the `BalanceSheetData`
  / JSON: posting flattening, price-series assembly, FX fallback, a
  missing-price commodity surfaced (not dropped), `as_of_min/max` bounds.
- **As-of aggregation** is the one piece of logic that lives in JS. Mirror
  it in a small Python reference implementation used by the tests (sum
  units ≤ date, value at latest price ≤ date) so the *algorithm* is
  covered deterministically; keep the JS a thin port. (Or extract it to a
  tiny JS module with a node test — see Open questions.)
- **Template inlining golden**: `render_html` over a fixture dataset →
  assert the token is gone, the JSON is present, and the file is
  self-contained (no `http`/CDN references) — this also guards the
  "offline" property.
- **No bean-query in tests** (binary + non-deterministic load); the
  `run_query` boundary is the seam.

## CLI / config surface

- New command `balance-sheet` (read-only).
- New setting `balance_sheet_reports_dir`.
- New `[post.reports] balance_sheet` toggle (default off), documented in
  the example config.
- README: a "balance sheet" subsection under the reports, plus the
  vendored-chart-lib licence line.

## Non-goals

- **Cost basis / unrealised P&L column.** Needs per-date FIFO booking,
  which client-side unit-sums can't reconstruct. Phase 4: either a
  one-shot `bean-query` "as of today" cost column (static, loses date
  interactivity for that column only) or a server-rendered cost snapshot.
  MVP is **market value only**.
- **Statement-assertion drift overlay.** The dataset will carry the
  assertions; rendering the "ledger vs statement" diff is phase 4 (it
  overlaps `reconcile`).
- **Income / Expense ("flows") views.** The data's there; the *sheet* is
  Assets / Liabilities / net worth only.
- **Multi-entity / multi-currency operating views.** Single book, GBP
  operating currency, like every other report.
- **Reconciling with the statement-valuation reports.** Same divergence
  the trial balance documents (ledger positions/today vs statement
  snapshots/last-statement-date) — by design, not a bug.

## Open questions

1. **Chart lib: vendor or hand-roll?** A pinned minified Chart.js (MIT)
   is ~200 KB inlined into every artifact; a dependency-free SVG donut +
   bar is smaller and fully owned but more code to write. Lean
   hand-rolled if the chart needs are modest (allocation donut + top-N
   bar).
2. **Where does the as-of JS logic get tested?** Python reference impl
   (simplest, no new toolchain) vs a real JS unit test (needs node in
   CI). Default to the Python reference + a thin JS port unless we already
   want node in CI.
3. **Price source of record.** Read `data/prices.beancount` directly, or
   pull prices through `bean-query`'s price db in the same load? The
   latter is one fewer file dependency and guarantees the prices match
   the queried postings' load.
4. **Artifact size.** Thousands of raw postings inlined as JSON — fine for
   one book, but consider a compact schema (short keys `d/a/q/c`, as the
   prototype used) and dropping pre-`as_of_min` postings.

## Status: in progress — phases 1 & 2 shipped

Supersedes the `reports/balance-sheet/` prototype; no dependency on it.
Phasing: (1) `build_data` + JSON + tests → (2) template + `render_html` +
golden → (3) CLI + config + rebuild + vendored chart → (4) cost basis /
assertion-drift overlay.

### Phase 1 — done

`src/banking_pipeline/balance_sheet.py` + `tests/test_balance_sheet.py`.
Pure transform (one injected `run_query` shell-out) → `BalanceSheetData`
→ compact JSON. Verified against the real ledger: 1498 Asset/Liability
postings, as-of 2021-07-28 → 2026-06-09, FX series for all 7 non-GBP
currencies, ~434 KB JSON.

Open-question decisions made (with reasons):

1. **Price source (Q3): parse `data/prices.beancount` directly**, not via
   bean-query's price db. Confirmed against the real file: it carries only
   *security* marks (in the quote ccy — EUR/USD/GBP) and **no** currency→GBP
   directives, so every non-GBP currency's GBP series is **synthesised from
   the `GbpRateSource`** (monthly points across the data span). The plan's
   "prices.beancount: …and currency→GBP rates" was inaccurate.
2. **Schema (Q4): compact short keys** (`d/a/q/c`, `d/p/c`), decimals as
   strings. Pre-`as_of_min` dropping is moot — `as_of_min` *is* the earliest
   posting date. ~434 KB inlined is fine for one book.
3. **Scope: query `Assets` + `Liabilities` only** (the holdings a
   market-value sheet values; the Lombard loan is negative cash in
   `Assets`). Equity/Income/Expense *flows* are deferred — consistent with
   the "Income/Expense views" non-goal — keeping the artifact lean rather
   than querying all five roots.
4. `build_data` returns `(data | None, QueryResult)` so the CLI degrades on
   a missing/erroring `bean-query` (mirrors trial balance). It takes
   explicit `prices_path` / `assertions_path` (a missing file → empty).

### Phase 2 — done

`src/banking_pipeline/balance_sheet_template.html` (data-free, committed)
+ `render_html` + `value_as_of` (the Python reference the template's JS
ports) + tests.

- **Chart (Q1): hand-rolled SVG donut**, built via `innerHTML` (no
  `createElementNS`, so the artifact contains no `http` substring at all —
  not even the SVG namespace URL). No vendored lib, no CDN, no licence line
  to add. A top-N bar wasn't needed; the donut + legend covers allocation.
- **As-of JS testing (Q2): Python reference impl** (`value_as_of`,
  fully tested) mirrored by a thin JS port; no node in CI.
- `render_html` substitutes the JSON for a single `"__DATA_PLACEHOLDER__"`
  token and escapes `</` so a fund description can't break out of the
  `<script>`. Golden tests assert the token is gone, the JSON is present,
  and `"http" not in html` (the offline guarantee).
- **Verified in a browser** against a scrubbed fixture: totals, collapsible
  tree (loan shown as negative red cash), allocation donut, and as-of
  scrubbing all recompute correctly and match `value_as_of`; no console
  errors.

### Phase 3 — next

CLI command `balance-sheet`, `Settings.balance_sheet_reports_dir`, the
`[post.reports] balance_sheet` toggle + rebuild wiring, the
`reports/balance-sheet/.gitignore`, and the README/architecture docs (no
chart-lib licence line needed — nothing vendored).

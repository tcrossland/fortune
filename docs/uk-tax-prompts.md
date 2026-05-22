# UK tax reporting — Claude Code implementation prompts

Seven self-contained prompts for Claude Code to land UK-tax support
in the banking-pipeline. Status and order:

- Prompt 1 — GBP cost basis on `Transaction` — **done**.
- Prompt 2 — FX-aware builders emitting GBP cost annotations —
  depends on 1.
- Prompt 3 — Reporting status on commodity directives — parallel,
  no dependency.
- Prompt 4 — Foreign dividend / interest split with WHT — parallel,
  no dependency on 1–3.
- Prompt 5 — Accrued bond interest split (UK accrued income
  scheme) — parallel, no dependency on 1–3.
- Prompt 6 — Sidecar `transactions.jsonl` alongside `.beancount` —
  groundwork for tax reporting; benefits from 1, 4, 5 being in
  before downstream consumers run, but can be implemented anytime.
- Prompt 7 — `tax-report` CLI producing SA106 / SA108 inputs —
  depends on 2, 3, 4, 5, 6.

Context (high level): UK CGT requires every security acquisition and
disposal to be tracked at its GBP equivalent at the trade-date spot
rate, and the section 104 pool is maintained in GBP. UK tax also
distinguishes UK-reporting-status offshore funds (gains = CGT) from
non-reporting funds (gains taxed as income), so commodity-level
metadata needs to be surfaced for SA108 / SA106 queries. Foreign
dividends and interest need to be reported gross with foreign
withholding tax separately disclosed (SA106), and the UK accrued
income scheme requires accrued interest on bond purchases to be
treated as interest income, not as part of the CGT cost basis.

## Design choice — GBP as metadata, not as embedded `{cost}`

The ledger stays in the native trade currency. GBP figures are
carried as **posting metadata** (`gbp-rate`, `trade-date`) and **all
GBP arithmetic — section 104 pool, same-day and 30-day matching,
gain/loss computation — happens in the `tax-report` CLI** consuming
the sidecar `transactions.jsonl`, not in beancount.

Reasons:

- Beancount's booking methods (`AVERAGE`, `STRICT`, …) do not
  implement UK matching rules. Same-day and 30-day "bed and
  breakfast" matching sit above the section 104 pool; whatever
  beancount computes would be an approximation that prompt 7's
  calculator overrides. Having beancount also compute a (different)
  number creates two competing answers.
- Pictet statements are denominated in EUR/USD/etc. Keeping the
  ledger in those currencies makes reconciliation against the source
  statements trivial and sidesteps per-currency tolerance interactions
  at `bean-check` time.
- HMRC publishes monthly average rates and occasionally republishes;
  a user might also switch from monthly average to daily mid-project.
  Metadata-only means re-running the tax report; embedded `{cost}`
  would mean regenerating ledger history.
- The cash leg of an FX-funded purchase also has a GBP value worth
  carrying. Metadata applies symmetrically to all postings on a
  transaction; `{cost}` does not.
- Smaller writer surface — builders append metadata lines to
  existing postings instead of restructuring cost annotations. The
  back-compat contract ("byte-identical when `gbp_rate is None`") is
  easier to honour.

Trade-offs accepted:

- No Fava-visible realised gains directly on the ledger. The
  `tax-report` CLI optionally emits a derived
  `tax-adjustments.beancount` file (per tax year) that the user can
  `include` from `main.beancount` if they want CGT visibility in
  Fava. This file is generated output and treated like
  `data/portfolio.beancount`.
- Metadata is not validated by beancount the way `{cost}` is. The
  `tax-report` CLI cross-checks the JSONL sidecar against the
  ledger and warns on drift.
- No `Income:CapitalGains:Foreign:*` account is written by the
  writer itself. Capital-gains accounts only appear in the derived
  `tax-adjustments.beancount` if the user opts in.

---

## Prompt 1 — GBP cost basis on the `Transaction` model

Add UK GBP cost-basis tracking to `banking-pipeline`'s `Transaction`
model and extraction layer. Do not change the writer in this pass.

Background: UK CGT requires every security acquisition and disposal
to be recorded at its GBP equivalent at the trade-date spot rate.
Pictet trade confirmations are in EUR/USD/etc; we need to capture the
GBP rate alongside.

Changes:

1. `src/banking_pipeline/models.py` — add
   `gbp_rate: Decimal | None = None` to `Transaction`, with a
   docstring describing it as "GBP per 1 unit of `currency` at trade
   date. `None` means no rate available; downstream builders fall
   back to current behaviour."

2. New `src/banking_pipeline/fx/gbp_rates.py`:
   - A `Protocol` `GbpRateSource` with
     `get_rate(self, on_date: date, currency: str) -> Decimal | None`.
   - `HmrcMonthlyAverageSource` backed by
     `data/fx/hmrc-monthly-average.csv` (columns: `month` `YYYY-MM`,
     `currency`, `rate`). The user will maintain the file. Missing
     entries → `None`. Resolve a date by snapping to its month.
   - `NullSource` that always returns `None`.
   - A small `build_rate_source(settings: Settings) -> GbpRateSource`
     factory.

3. `src/banking_pipeline/config.py` — add
   `gbp_rate_source: Literal["null", "hmrc-monthly"] = "null"` and
   `hmrc_rate_path: Path | None = None`.

4. `src/banking_pipeline/fields/hybrid.py` — after extraction, for
   each `Transaction`:
   - If `currency == "GBP"`, set `gbp_rate = Decimal("1")`.
   - Else, look up the rate using the configured source and the
     transaction's trade date. Populate `gbp_rate` if non-`None`;
     leave as `None` otherwise. Never fail extraction on a missing
     rate.

5. Tests in `tests/test_gbp_rates.py`:
   - `HmrcMonthlyAverageSource` against a tiny in-memory CSV
     (mid-month date snaps to the month's row, missing month →
     `None`, unknown currency → `None`).
   - Pipeline-level enrichment populating `gbp_rate` on a non-GBP
     fixture transaction with a configured source.
   - GBP-denominated transactions get `gbp_rate=Decimal("1")`
     regardless of source.

Constraints: Python 3.14, `uv run` for everything, no
`import beancount`, mypy strict must pass. Validate with
`uv run ruff check .`, `uv run mypy src`, `uv run pytest`. Do not
touch any builder under `src/banking_pipeline/writer/`.

---

## Prompt 2 — FX-aware builders emit GBP-rate metadata

Update the beancount writer in `banking-pipeline` to attach
GBP-rate metadata to postings when `Transaction.gbp_rate` is set.
Builds on prompt 1 (which added `gbp_rate` to `Transaction`). Read
`CLAUDE.md` first — the "Beancount output conventions" and
"Strict-mode dispatch" sections govern this work, and read the
top-of-file "Design choice — GBP as metadata" section to understand
why this is metadata rather than `{cost}`.

Background: the ledger stays in the native trade currency. The GBP
rate is carried as **posting metadata** (`gbp-rate`, `trade-date`)
on every posting that participates in a UK-CGT-relevant event. All
GBP arithmetic (section 104 pool, same-day / 30-day matching, gain
computation) is deferred to the `tax-report` CLI in prompt 7. The
writer's only job here is to make the rate addressable per posting.

Changes:

1. `src/banking_pipeline/writer/format.py` — add
   `format_gbp_metadata(gbp_rate: Decimal, trade_date: date,
   indent: int = 4) -> list[str]` returning the two indented
   metadata lines:

   ```
       gbp-rate: "0.8384"
       trade-date: 2026-05-22
   ```

   Decimal precision: render the rate to 6 significant figures (HMRC
   monthly average rates are published to 4 dp; daily rates to 6 dp;
   6 sig fig covers both). Quote the rate as a string in beancount
   metadata to avoid float coercion by downstream consumers. Make
   the helper accept a list of postings rather than a single string
   so callers can attach metadata to multiple postings in one call.

2. Builders under `src/banking_pipeline/writer/builders/` that
   participate in CGT-relevant events — review every builder;
   targets include `security_trade` (buy/sell), `bond_trade`,
   `switch_trade`, `fx_settlement`, and any redemption / final
   redemption path. For each:
   - When `transaction.gbp_rate is not None`, append
     `format_gbp_metadata(...)` lines **under every posting on the
     entry**, not just the security leg. The cash leg's GBP value is
     useful for FX-disposal sanity checks and for cash-leg
     reconciliation in the tax report.
   - The posting amount, account, and existing `@` annotations are
     unchanged. The cash leg keeps its existing `@ <price>
     <security_ccy>` form on FX-bearing trades.
   - When `gbp_rate is None`, no metadata is emitted — output is
     byte-identical to today.

3. Income builders (`dividend.py`, `interest.py`) — also attach the
   metadata when `gbp_rate` is set on the income transaction, so the
   tax report can compute the GBP value of dividends and coupons
   from the same metadata source as trades.

4. Do **not** add a `capital_gains_account_template` to
   `BankWriterProfile`. The writer does not emit
   `Income:CapitalGains:Foreign:*` postings under this design —
   capital-gains accounts live in the derived
   `tax-adjustments.beancount` produced by the `tax-report` CLI
   (prompt 7), not in source ingest output.

5. Do **not** use beancount's `{cost, date}` lot-annotation syntax
   for UK-CGT purposes. Existing `{cost}` usage for other reasons
   (e.g. tracking fund unit prices) stays as-is; just don't introduce
   GBP-denominated cost annotations.

6. Goldens:
   - Add `tests/fixtures/en/pictet/buy_equity.uk_gbp.txt` and
     `.beancount` exercising a EUR-denominated buy with `gbp_rate`
     populated. The expected output should show
     `gbp-rate: "0.838400"` and `trade-date: YYYY-MM-DD` indented
     under each posting.
   - Add a matching `sell_equity.uk_gbp.txt` and `.beancount` for
     the disposal counterpart. The disposal output has no special
     lot-matching syntax — just metadata, same as the buy.
   - **Do not modify existing fixtures or goldens.** The contract is
     byte-identical output when `gbp_rate is None`. Re-run the full
     golden suite to confirm.

7. Hybrid extractor strict-mode dispatch in `fields/hybrid.py` is out
   of scope — do not change it.

Validate: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`. Then build a tiny ad-hoc ledger including the new
GBP-aware goldens plus the existing `main.beancount` tolerance lines
and confirm `uv run banking-pipeline check` passes (posting metadata
is valid beancount; bean-check should not object to either the
`gbp-rate` string or the `trade-date` date value).

---

## Prompt 3 — Reporting status on commodity directives

Make `banking-pipeline`'s `portfolio_aggregate` emit beancount
`commodity` directives with UK-tax-relevant metadata (ISIN, domicile,
reporting-status, asset class), sourced from a hand-curated file the
user maintains. Read `CLAUDE.md` first — the "Beancount output
conventions" section explains that `data/portfolio.beancount` is
generated and `main.beancount` is hand-curated.

Background: UK tax treats gains on UK-reporting-status offshore funds
as CGT; gains on non-reporting funds are taxed as offshore income
gains (income, not capital). Surfacing the status as commodity
metadata lets disposals be partitioned by query at SA108/SA106 time.

Changes:

1. New file `data/commodities.example.toml` (and document that the
   real `data/commodities.toml` is gitignored, matching the existing
   pattern for `banking-pipeline.toml`):

   ```toml
   [[commodity]]
   isin = "IE00B3VWN518"
   name = "iShares Core MSCI World UCITS ETF"
   domicile = "IE"
   reporting_status = "reporting"   # reporting | non-reporting | uk-domestic | unknown
   asset_class = "equity-etf"       # equity-etf | bond | equity-fund | money-market | other
   first_acquired = 2018-03-15
   ```

2. New `src/banking_pipeline/commodities_metadata.py`:
   - Pydantic `CommodityMetadata` mirroring the TOML schema.
     `reporting_status` is a
     `Literal["reporting", "non-reporting", "uk-domestic", "unknown"]`.
   - ISIN validation via `python-stdnum` (already a dep — see
     `fields/validators.py`).
   - `load_commodities(path: Path) -> dict[str, CommodityMetadata]`
     keyed by ISIN. Raise a clear error on duplicate ISIN.

3. `src/banking_pipeline/portfolio_aggregate.py`:
   - Discover ISINs in use by walking `data/` outputs (use the
     existing discovery logic).
   - For each known ISIN, emit at the top of
     `data/portfolio.beancount` (above account opens):

     ```
     2018-03-15 commodity IE00B3VWN518
       name: "iShares Core MSCI World UCITS ETF"
       isin: "IE00B3VWN518"
       domicile: "IE"
       reporting-status: "reporting"
       asset-class: "equity-etf"
     ```

     using `first_acquired` as the directive date. Note beancount
     metadata keys are kebab-case in output even though the pydantic
     field is snake_case.
   - For ISINs without metadata, emit a stub commodity directive with
     `reporting-status: "unknown"` dated `1970-01-01`, preceded by a
     comment `; missing metadata — add an entry to data/commodities.toml`.

4. Extend the `portfolio` CLI in `src/banking_pipeline/cli.py` with
   `--list-missing-metadata`: when set, print one ISIN per line for
   in-use commodities lacking a metadata entry, then exit. Useful for
   keeping `data/commodities.toml` in sync.

5. Settings: add `commodities_metadata_path: Path | None = None` to
   `config.py`, defaulting to `data/commodities.toml` if it exists.

6. Tests in `tests/test_commodities_metadata.py`:
   - Round-trip a small TOML through `load_commodities`.
   - Reject malformed ISINs and unknown `reporting_status` values.
   - Reject duplicate ISINs.
   - `portfolio_aggregate` emits the expected directive text given a
     stub metadata dict and a stub ISIN-discovery result
     (snapshot-style assertion).
   - Missing-metadata ISINs produce the stub directive with the
     comment line.

Validate: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`, and `uv run banking-pipeline rebuild` against a
local config — confirm `data/portfolio.beancount` now leads with the
commodity directives and the rest of the rebuild still clears
`bean-check`.

---

## Prompt 4 — Foreign dividend / interest split with withholding tax

Split foreign dividend and interest income into a gross income leg
and a separate foreign-withholding-tax leg, partitioned by country.
SA106 needs gross income, foreign tax suffered, and country of source
per security; the current writer routes net amounts to a single
`Income:<prefix>:<portfolio>:<ISIN>:Dividend` account, which loses
the WHT information needed to claim foreign tax credit relief.

Read `CLAUDE.md` first — the "Beancount output conventions" section
and the `DocumentType` enum docstrings in
`src/banking_pipeline/models.py` (especially `DIVIDEND_NOTICE`,
`DISTRIBUCION`, `INTEREST_PAYMENT`, coupon doctypes) are the source
of truth for what each advice carries.

Changes:

1. `src/banking_pipeline/models.py` — extend `Transaction` with:
   - `gross_income: Decimal | None = None` — pre-tax amount Pictet
     printed on the advice (positive).
   - `withholding_tax: Decimal | None = None` — foreign tax withheld
     at source (positive, same currency as the income leg).
   - `withholding_country: str | None = None` — ISO 3166-1 alpha-2
     code (e.g. `"LU"`, `"US"`, `"IE"`) identifying the country that
     levied the WHT. Source it from the issuer / domicile fields on
     the advice; fall back to the security's domicile from the
     commodity metadata (prompt 3) when the advice doesn't print it
     explicitly.
   - Invariant (assert in a model validator): when `withholding_tax`
     is set, `gross_income` must be set and
     `gross_income - withholding_tax == amount` to within the
     currency's tolerance. The cash leg (`amount`) remains the net
     amount that hit the account.

2. Templates under `src/banking_pipeline/templates/pictet/` that
   handle income events (review every template; targets include
   `dividend_notice`, `distribucion`, `interest_payment`, any coupon
   doctype, plus the Madrid-locale dividend variants) — populate
   `gross_income`, `withholding_tax`, `withholding_country` from the
   PDF text. For advices with no WHT printed (typical for Lux funds
   distributing to a Lux holder), leave WHT fields unset; the income
   leg renders gross only.

3. `src/banking_pipeline/writer/builders/dividend.py` and
   `interest.py`:
   - When `withholding_tax` is set, emit a three-leg entry:
     ```
     Income:<prefix>:<portfolio>:<ISIN>:Dividend  -<gross_income> <ccy>
     Expenses:Tax:Withholding:<country>            <withholding_tax> <ccy>
     Assets:<prefix>:<portfolio>:<ccy>             <amount> <ccy>
     ```
   - When WHT is not set, render exactly as today (two-leg net).
   - For interest events, swap `:Dividend` → `:Interest` in the
     income-account suffix.
   - Make the WHT account template configurable on
     `BankWriterProfile` in `writer/profile.py` as
     `withholding_tax_account_template: str =
     "Expenses:Tax:Withholding:{country}"` (the writer will format
     it with `country=tx.withholding_country.upper()`).

4. `writer/dispatch.py` — when rendering `open` directives, ensure
   any `Expenses:Tax:Withholding:<country>` accounts used in a batch
   are opened once. Keep the existing per-ISIN `Income:…:Dividend`
   open behaviour intact.

5. Goldens — add at least two new fixture pairs under
   `tests/fixtures/en/pictet/` and `tests/fixtures/es/pictet/`:
   - A dividend advice with WHT (e.g. a US-domiciled equity dividend
     with 15% US WHT under treaty rates).
   - A coupon payment with WHT.
   Do not modify existing goldens — the contract is that when WHT
   fields are `None`, output is byte-identical to today.

6. Tests:
   - Model validator rejects `withholding_tax > gross_income`,
     mismatched arithmetic, or WHT without a country.
   - Round-trip a fixture with WHT through the full pipeline and
     diff against the new golden.

Validate: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`. Construct a small ad-hoc ledger including a
WHT-bearing golden and confirm `uv run banking-pipeline check`
passes (the new `Expenses:Tax:Withholding:*` accounts must be opened
in scope).

---

## Prompt 5 — Accrued bond interest split (UK accrued income scheme)

Split bond purchase and sale advices that include accrued interest
so the accrued component is booked as interest income/expense rather
than rolled into the security's cost basis. Under the UK accrued
income scheme, accrued interest paid on a bond purchase is a
deduction against the buyer's interest income for that year, and
accrued interest received on a sale is taxed as interest, not as
part of the capital proceeds.

`Transaction.accrued_interest` already exists (see
`src/banking_pipeline/models.py`) but the bond builders treat it as
part of the cash leg. This prompt rewires the bond-trade builder to
emit a dedicated accrued-interest posting.

Changes:

1. `src/banking_pipeline/models.py` — confirm
   `accrued_interest: Decimal | None` is present (it is, per current
   code) and clarify its docstring to say "positive on a purchase
   (buyer pays seller), positive on a sale (seller receives from
   buyer); the sign for posting is determined by the trade direction
   in the builder."

2. `src/banking_pipeline/writer/builders/bond_trade.py`:
   - When `tx.accrued_interest` is set on a **purchase**
     (`BUY_BONDS` or its Spanish counterpart), split the cash leg
     into:
     ```
     Assets:<prefix>:<portfolio>:<ISIN>   <N> <ISIN> @ <price> <ccy>
     Income:<prefix>:<portfolio>:<ISIN>:AccruedInterest  -<accrued_interest> <ccy>
     Assets:<prefix>:<portfolio>:<ccy>    -<gross_cash> <ccy>
     ```
     The security leg remains in trade currency — UK CGT cost basis
     is recovered downstream from the `gbp-rate` posting metadata
     emitted by prompt 2 plus this transaction's accrued-interest
     split.

     The `Income:…:AccruedInterest` posting is a debit (positive on
     income-side = a reduction of interest income — this is the UK
     accrued income scheme treatment) but emitted as the
     signed-positive amount the buyer paid. Document this clearly in
     the docstring because the sign is counter-intuitive.
   - On a **sale** (`SELL_BONDS` etc.), the accrued interest the
     seller receives is income:
     ```
     Income:<prefix>:<portfolio>:<ISIN>:AccruedInterest  -<accrued_interest> <ccy>
     ```
     and the cost-basis disposal proceeds for CGT purposes are the
     gross cash *minus* the accrued interest (split out from the
     `@` price-annotation leg).
   - When `tx.accrued_interest` is `None` or zero, render exactly as
     today.

3. Templates under `src/banking_pipeline/templates/pictet/` covering
   bond trades (`buy_bonds.py`, `sell_bonds.py`, Spanish variants if
   any) — ensure they populate `accrued_interest` from the PDF
   text's `Interest` / `Intereses corridos` line in the CASH EFFECT
   block. The `DocumentType.BUY_BONDS` enum docstring already
   describes the marker; cite it.

4. Goldens — add new fixture pairs:
   - `tests/fixtures/en/pictet/buy_bonds.accrued.txt` + `.beancount`
     for a coupon-bearing bond purchase mid-coupon-period.
   - `tests/fixtures/en/pictet/sell_bonds.accrued.txt` + `.beancount`
     for the disposal counterpart.
   Existing `buy_bonds` and `sell_bonds` goldens stay unchanged —
   they presumably either had zero accrued interest or pre-date this
   split; the contract is back-compat when `accrued_interest is
   None`.

5. Profile knob: add
   `accrued_interest_account_template: str =
   "Income:{prefix}:{portfolio}:{isin}:AccruedInterest"` on
   `BankWriterProfile`.

6. Tests:
   - A unit test on the bond builder asserting the three-leg shape
     when `accrued_interest` is set on a buy.
   - The mirror test for a sell.
   - Existing parametric fixture suite continues to pass for
     accrued-free bond trades.

Validate: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`, and confirm the new accrued-interest goldens clear
`bean-check` against a ledger that opens the
`Income:…:AccruedInterest` accounts (the dispatch's open-directive
collection should pick these up automatically).

---

## Prompt 6 — Sidecar `transactions.jsonl` alongside `.beancount`

Emit a structured sidecar JSONL of every extracted `Transaction`
next to each generated `.beancount` file. This is the data substrate
the tax-report CLI (prompt 7) will consume; carrying the structured
form alongside the rendered ledger avoids re-parsing beancount text
and works around the constraint that we cannot `import beancount`
(GPL-2.0).

Background: the pipeline already produces `.beancount` output per
document. The information we need for UK tax reporting — GBP rate,
WHT, accrued interest, security ISIN, trade date, gross/net amounts
— is on the `Transaction` object but is partially encoded into
free-text postings in the rendered output. Persisting the
`Transaction` directly avoids that lossy round-trip.

Changes:

1. New `src/banking_pipeline/transaction_sidecar.py`:
   - `def dump_transactions(transactions: Iterable[Transaction],
     path: Path) -> None` — write one JSON object per line. Use
     `Transaction.model_dump(mode="json")` (pydantic v2) so
     `Decimal`, `date`, and any enum fields serialise stably.
   - `def load_transactions(path: Path) -> list[Transaction]` —
     companion reader. Validate every line through `Transaction`'s
     model so we get strict typing back on read.
   - Include a top-of-file JSON line with a schema marker (single
     object with `_schema: "banking-pipeline/transactions/v1"` and
     `source_document: <pdf relative path>`) so future schema
     migrations have a hook.

2. `src/banking_pipeline/pipeline.py` — after `Pipeline.ingest`
   renders the `.beancount` for a document, also write
   `<output>.transactions.jsonl` next to it using
   `dump_transactions`. Empty results (e.g. `NO_OUTPUT_DOCTYPES`)
   write a header-only file so downstream consumers can distinguish
   "no transactions, expected" from "file missing".

3. `src/banking_pipeline/cli.py`:
   - `ingest` and `rebuild` write sidecars automatically — no new
     flag needed; the sidecar is part of the contract.
   - Add a new `dump-transactions` subcommand:
     `banking-pipeline dump-transactions <pdf>` — runs through to
     extraction and prints the JSONL to stdout. Useful for ad-hoc
     inspection and for piping into the tax-report CLI without
     touching the on-disk ledger.

4. `src/banking_pipeline/batch_config.py` — if there's a
   `[post.clean]` style cleanup step that removes generated files,
   extend its glob to also remove `*.transactions.jsonl` so a clean
   rebuild doesn't leave stale sidecars.

5. Tests:
   - Round-trip a `Transaction` (with all UK-relevant fields:
     `gbp_rate`, `gross_income`, `withholding_tax`,
     `withholding_country`, `accrued_interest`) through
     `dump_transactions` → `load_transactions` and assert equality.
   - Decimal precision is preserved (no float coercion).
   - Pipeline integration: a single-PDF ingest produces both the
     `.beancount` and the `.transactions.jsonl`, with the JSONL
     containing the expected count of transactions.
   - `dump-transactions` CLI emits to stdout without touching the
     output directory.

6. Document the sidecar in the README under "Output" — one
   paragraph noting that each `.beancount` is accompanied by a
   `.transactions.jsonl` and that downstream tools (the tax-report
   CLI) consume the latter.

Constraints: `Transaction.model_dump(mode="json")` already handles
most pydantic v2 serialisation, but verify `Decimal` round-trips as
a string (the default) rather than a float — write a regression test
for this specifically.

Validate: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`, `uv run banking-pipeline rebuild` on a local config
and confirm every generated `.beancount` has a matching
`.transactions.jsonl` sibling.

---

## Prompt 7 — `tax-report` CLI producing SA106 / SA108 inputs

Implement a new `banking-pipeline tax-report` subcommand that
consumes the sidecar `.transactions.jsonl` files (prompt 6), applies
UK tax-year boundaries and section 104 / matching rules, and emits
CSVs ready to be transcribed onto SA106 (foreign income) and SA108
(capital gains).

This is the consumer side of all prior prompts. **All GBP arithmetic
lives here** — the writer (prompts 2, 4, 5) only carries the GBP
rate as metadata and renders source-currency postings. The
`tax-report` CLI does the GBP conversion, section 104 matching,
same-day and 30-day rules, gain computation, and reporting-status
partitioning.

It relies on the GBP-rate metadata produced by prompt 2 (via
`Transaction.gbp_rate` from prompt 1), commodity reporting-status
metadata (3), WHT fields on `Transaction` (4), the accrued-interest
split (5), and the structured sidecar (6).

Read `CLAUDE.md`'s licence-hygiene note — `import beancount` remains
forbidden. The CLI works entirely off the JSONL sidecars and the
commodity-metadata TOML.

Changes:

1. New package `src/banking_pipeline/tax/uk/` containing:

   - `tax_year.py` — `def tax_year_bounds(label: str) -> tuple[date,
     date]` parsing labels like `"2025-26"` into
     `(date(2025, 4, 6), date(2026, 4, 5))`. A `date_to_tax_year`
     inverse helper for stamping rows. Reject ambiguous labels.

   - `section_104.py` — pool calculator. For a single ISIN, iterate
     acquisitions and disposals in chronological order applying:
     1. Same-day rule: acquisitions and disposals on the same date
        match against each other first.
     2. 30-day bed-and-breakfast rule: a disposal matches against
        acquisitions in the **30 days following** the disposal
        before drawing from the pool.
     3. Section 104 pool: weighted-average GBP cost (qty-weighted).

     Output: a list of `MatchedDisposal` records with `disposal_qty`,
     `proceeds_gbp`, `cost_gbp`, `gain_gbp`, `matched_against`
     (one of `"same-day"`, `"bed-and-breakfast"`, `"s104"`),
     `acquisition_dates: list[date]`. Tests must cover at least:
     pure-pool disposal, same-day match, 30-day match, mixed
     scenario where a disposal is partially matched across all
     three buckets.

   - `sa106.py` — aggregate foreign income within a tax year. Group
     by `(withholding_country, ISIN, "Dividend"|"Interest")`. For
     each group emit gross income (GBP), WHT (GBP), and net (GBP),
     with the underlying source documents listed.

   - `sa108.py` — aggregate capital gains within a tax year. Group
     disposals by ISIN, run `section_104` matching, partition output
     by `reporting-status` from the commodity metadata:
     - `reporting` and `uk-domestic` → CGT (SA108).
     - `non-reporting` → offshore income gains (SA106 box 41).
     - `unknown` → emit but flag with a `WARN_UNCLASSIFIED` marker
       so the user sees what's missing.

   - `currency.py` — GBP conversion helpers using
     `Transaction.gbp_rate` for trade-dated conversions and a
     supplied source (the same `GbpRateSource` from prompt 1) for
     dividends / WHT where the advice date is the conversion date.

2. `src/banking_pipeline/cli.py` — new subcommand
   `banking-pipeline tax-report`:
   - `--year YYYY-YY` (required) — the UK tax year, e.g.
     `--year 2025-26`.
   - `--source <dir>` (default `data/`) — root to walk for
     `*.transactions.jsonl` files.
   - `--out <dir>` (default `reports/uk-tax/<year>/`) — where to
     write the CSVs.
   - `--commodities <path>` (default from settings) — commodity
     metadata TOML.
   - `--rate-source` flag mirroring the ingest-side options for
     advices that weren't enriched at extraction time (defensive —
     in practice prompt 1's enrichment covers this).

   Outputs (CSVs are all GBP):
   - `<out>/sa106-dividends.csv` — columns: `country`, `isin`,
     `commodity_name`, `gross_gbp`, `wht_gbp`, `net_gbp`,
     `document_count`.
   - `<out>/sa106-interest.csv` — same shape.
   - `<out>/sa106-offshore-income-gains.csv` — non-reporting fund
     disposals.
   - `<out>/sa108-disposals.csv` — columns: `disposal_date`, `isin`,
     `commodity_name`, `reporting_status`, `quantity`,
     `proceeds_gbp`, `cost_gbp`, `gain_gbp`, `match_type`,
     `acquisition_dates`.
   - `<out>/summary.txt` — human-readable totals and any warnings
     (`WARN_UNCLASSIFIED`, missing GBP rates, unreconciled lots).
   - `<out>/tax-adjustments.beancount` — **optional**, emitted only
     when `--emit-beancount` is passed. A derived beancount file the
     user can `include` from `main.beancount` for Fava visibility:
     one entry per matched disposal posting realised gain/loss to
     `Income:CapitalGains:Foreign:<ISIN>` and a balancing posting to
     a synthetic `Equity:UKTax:Adjustments:<year>` account. The file
     header explains it is generated and must not be hand-edited.
     Treated like `data/portfolio.beancount`: regenerated on every
     `tax-report` run.

3a. **Drift check.** Before computing, walk the user's existing
    ingest output and confirm every transaction in the sidecar JSONL
    has a corresponding entry in the rendered `.beancount` (same
    transaction number / source document). Warn loudly on
    discrepancies — this is the cross-check the metadata-only
    design relies on to catch the case where someone hand-edits a
    posting and forgets to refresh the sidecar.

3. `src/banking_pipeline/config.py` — add
   `tax_reports_dir: Path = Path("reports/uk-tax")`.

4. Tests in `tests/tax/uk/`:
   - `test_tax_year.py` — boundary parsing.
   - `test_section_104.py` — at least six scenarios covering each
     matching rule and combinations. Use synthetic
     `MatchedDisposal`-input transactions, not Pictet fixtures.
   - `test_sa106.py` — aggregation across multiple advices, currency
     grouping, missing-WHT-country guard.
   - `test_sa108.py` — partition by reporting status, unclassified
     warning.
   - `test_drift_check.py` — sidecar entry with no matching ledger
     row produces a `WARN_DRIFT` warning; ledger row with no
     matching sidecar entry also warns.
   - `test_tax_adjustments_beancount.py` — `--emit-beancount`
     produces a parseable file with the expected
     `Income:CapitalGains:Foreign:*` and
     `Equity:UKTax:Adjustments:<year>` postings balanced per
     disposal. The omission of the flag suppresses the file.
   - `test_tax_report_cli.py` — end-to-end against a tiny fixture
     ledger covering one acquisition, one same-day partial disposal,
     one s104 disposal, one dividend with WHT, one coupon, and one
     non-reporting fund disposal. Snapshot the CSVs.

5. README — new "UK tax reporting" section pointing at
   `banking-pipeline tax-report`, the CSV layouts, the optional
   `--emit-beancount` flag and how to wire its output into
   `main.beancount` via `include`, and the limitations (no
   `bed-and-breakfast` matching across tax-year boundaries beyond 30
   days; no automatic excess reportable income handling — call
   those out as known follow-ups).

Constraints: still no `import beancount`. All inputs are JSONL
sidecars + the commodity TOML. The CLI must complete without an
Anthropic API key (no LLM paths in this code path). Decimal
arithmetic everywhere — never `float`.

Validate: `uv run ruff check .`, `uv run mypy src`,
`uv run pytest tests/tax/uk/`, then run
`uv run banking-pipeline tax-report --year 2025-26` against the
local rebuild output and sanity-check the CSVs by eye.

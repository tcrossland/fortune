# UK tax reporting — follow-up prompts (Pictet-report parity)

Five self-contained prompts closing the gaps found by comparing the
generated 2024/25 `tax-report` output against Pictet's official UK Tax
Report for the same year. They build on the seven prompts in
`uk-tax-prompts.md` (all implemented) and the tooling around them
(`scripts/fetch_hmrc_rates.py`, `scripts/fetch_reporting_funds.py`,
`scripts/scaffold_commodities.py`, `scripts/fill_commodity_names.py`).

Status and order (each is independently shippable except where noted):

- Prompt 8 — Capture dividend withholding tax that's currently missed —
  small, self-contained.
- Prompt 9 — CGT rate-change split + gains/losses separation —
  mechanical, presentation-layer.
- Prompt 10 — Opening positions / pre-ledger cost basis — foundational
  for gain accuracy; no dependency.
- Prompt 11 — Deep discounted securities taxed as income — needs a
  commodity-metadata flag; no dependency.
- Prompt 12 — Excess reportable income (ERI) + equalisation — the
  largest, and the dominant source of divergence; affects both income
  and CGT base cost. Best done last.

What the comparison showed (2024/25, account 123456), generated vs
Pictet:

- Overseas interest: **none produced** vs **47,937.64** gross (mostly
  ERI from accumulating bond/credit funds).
- Overseas dividends: 10,816.79 gross / **0.00** WHT vs 19,187.90 gross
  / **100.49** WHT.
- Capital gains: single net **54,879.33** vs gains 81,015.30 (split
  34,020.86 before / 46,994.44 on-or-after 30 Oct 2024), losses
  −45,420.23 → net **35,595.07**.
- Offshore income gains: −372.04 vs **+1,694.29**.
- Deep discounted securities: not modelled vs losses −2,527.53 taxed to
  income.

The recurring causes: missing ERI/equalisation (understates income,
overstates gains via too-low base cost), a missed WHT line, no
rate-change split, no DDS rule, and a section 104 pool that only spans
the ledger (from 2021-07).

Shared constraints for every prompt below: Python 3.14, `uv run` for
everything, **no `import beancount`**, `mypy --strict` and
`ruff check .` must pass, and tax-critical inputs (WHT, ERI, opening
costs, reporting status) must come from real source documents — never
fabricated. Validate with `uv run ruff check .`, `uv run mypy src`,
`uv run pytest`.

---

## Prompt 8 — Capture dividend withholding tax that's currently missed

The WHT split landed in prompt 4, but the 2024/25 comparison shows a
real dividend whose withholding tax wasn't captured: the Novo Nordisk
('B') distribution was recorded as `gross_income ≈ net`, `withholding_tax
= 0`, `withholding_country` from the ISIN prefix — whereas Pictet reports
gross 372.19, **WHT 100.49** (27% Danish), net 271.70 (GBP). Across the
whole report our WHT total is 0.00; Pictet's is 100.49. So the
withholding-tax line on at least the direct-equity dividend advice isn't
matching `find_withholding_tax` in
`src/banking_pipeline/templates/pictet/_common.py`.

Background: `find_withholding_tax` greps the CASH EFFECT block for
`PictetLabels.withholding_tax` (`"Withholding tax"` EN /
`"Retención fiscal"` ES). The Danish-equity dividend advice almost
certainly prints the tax under a different label or in a different block
(e.g. a `Tax`/`Withholding`/per-country line, or in the SECURITY EVENT
block rather than CASH EFFECT), or the gross is only in the security-event
block while CASH EFFECT shows the net.

Changes:

1. Inspect the real advice. Run
   `uv run banking-pipeline extract-text <novo-dividend>.pdf` (a
   WHT-bearing equity dividend) and read the exact field labels around
   the gross / tax / net lines. Capture an **anonymised** copy as a
   fixture under `tests/fixtures/en/pictet/` (a new `dividend_notice`
   tag, e.g. `dividend_notice.equity_wht.txt`).

2. Extend the parser in `_common.py` (`find_withholding_tax` and/or the
   `withholding_tax` label on `PictetLabels`) to match the advice's
   actual format — additional label synonyms, or reading gross from the
   security-event block when CASH EFFECT only carries the net. Keep the
   existing fixtures matching (don't regress `dividend_notice.us_wht` /
   `distribucion.us_wht`).

3. Confirm the model invariant in `models.py`
   (`gross_income - withholding_tax == amount`) holds for the new
   fixture, and add the golden `.beancount` (3-leg WHT split) plus a
   `tests/test_withholding_tax.py` case.

4. Sanity-check end to end: a `tax-report` run over a sidecar containing
   the fixture shows the WHT in `sa106-dividends.csv`.

Note: `withholding_country` already prefers the curated
`data/commodities.toml` domicile over the ISIN prefix (see
`HybridExtractor._enrich_withholding_country`), so once the WHT amount
parses, the country attribution is already handled — Denmark in this
case, if the commodity's `domicile = "DK"`.

---

## Prompt 9 — CGT rate-change split and gains/losses separation

HMRC requires 2024/25 disposals to be split either side of the
30 October 2024 rate change, and a CGT computation reports total gains
and total allowable losses separately (the annual exempt amount and
loss-offset rules act on those, not on a single net). Our `tax-report`
emits one combined `total gain/loss` figure with no date split; Pictet
reports `34,020.86` (before 30 Oct) + `46,994.44` (on or after) gains
and `−45,420.23` losses.

Changes:

1. `src/banking_pipeline/config.py` — add a per-tax-year CGT
   rate-change boundary, e.g.
   `cgt_rate_change_dates: dict[str, date]` defaulting to
   `{"2024-25": date(2024, 10, 30)}`, so future boundaries are
   data-only. (A tax year with no boundary → no split.)

2. `src/banking_pipeline/tax/uk/sa108.py` — keep `Sa108Row` as-is, but
   give `compute_sa108` (or a small helper) the means to bucket rows by
   the boundary date. Add a `period` discriminator
   (`"pre"` / `"post"` / `""`) to `Sa108Row`, set from the boundary for
   the year.

3. `src/banking_pipeline/cli.py`:
   - `sa108-disposals.csv` gains a `period` column.
   - `summary.txt` reports, mirroring Pictet:
     `total gains (before <date>)`, `total gains (on/after <date>)`,
     `total allowable losses`, and the net. "Gains" sums positive
     `gain_gbp`; "losses" sums negative ones — separately, not netted.

4. Tests in `tests/tax/uk/` — a fixture ledger with disposals straddling
   the boundary, asserting the per-bucket gain totals and the
   gains-vs-losses split.

Out of scope: actual tax-rate arithmetic and the annual exempt amount —
this is presentation/segmentation only, matching what Pictet tabulates.

---

## Prompt 10 — Opening positions / pre-ledger cost basis

The section 104 pool is built only from transactions in the ledger
(earliest data 2021-07). Any holding acquired before that, or
transferred in from another custodian, has no acquisition in the pool,
so its allowable cost is understated (often zero) and the gain
overstated. Pictet uses client-supplied historical cost for exactly
these cases. This is a major contributor to our CGT net (54,879) sitting
above Pictet's (35,595).

Background: `tax/uk/section_104.py` takes `Acquisition` /`Disposal`
lists; `tax/uk/sa108.py::compute_sa108` builds them from sidecar
`Transaction`s per ISIN. We need to seed the pool with opening lots.

Changes:

1. New user-maintained `data/opening-positions.toml` (gitignored, with a
   committed `data/opening-positions.example.toml`), one entry per
   pre-ledger lot:

   ```toml
   [[lot]]
   isin = "LU0128316170"
   acquired = 2019-05-01
   quantity = 5068.383
   cost_gbp = 120000.00
   ```

   Mirror the `commodities_metadata` loader: a pydantic model + a
   `load_opening_positions(path) -> dict[str, list[OpeningLot]]` keyed by
   ISIN (a security can have several opening lots). Accept the same
   ISIN / 11-char internal-ref codes as `normalise_commodity_code`.

2. `config.py` — `opening_positions_path: Path | None`, defaulting to
   `data/opening-positions.toml` when present.

3. `tax/uk/sa108.py` — prepend the opening lots (as `Acquisition`s, GBP
   already supplied so no rate lookup) to each ISIN's acquisitions
   before matching. They sort ahead of ledger buys by date.

4. Surface a clear warning when a disposal can't be fully matched
   (pool quantity goes negative) — that means a missing opening
   position. Add the count/ISINs to `summary.txt` and to the existing
   `missing_rate_isins`-style reporting on `Sa108Report`.

5. CLI: `tax-report --opening-positions <path>` override; thread it
   through.

6. Tests: an opening lot seeds the pool and changes the gain; a disposal
   with no acquisition (ledger or opening) is flagged, not silently
   priced at zero cost.

---

## Prompt 11 — Deep discounted securities taxed as income

Gains on deeply discounted securities (DDS — broadly, bonds issued or
acquired at a discount above the de-minimis) are taxed as **income**,
not CGT, and DDS losses are generally not allowable. Pictet reports
`deep discounted gains taxed to income` and
`deep discounted securities losses` separately. We currently treat every
bond as an ordinary CGT disposal in `sa108-disposals.csv`.

Changes:

1. `src/banking_pipeline/commodities_metadata.py` — add a way to mark a
   security as DDS. Cleanest: a boolean `deeply_discounted: bool = False`
   on `CommodityMetadata` (TOML `deeply_discounted = true`). Update
   `data/commodities.example.toml` and the scaffold/notes.

2. `tax/uk/sa108.py` — when a disposed ISIN is flagged
   `deeply_discounted`, route its matched disposals out of the CGT rows
   into a separate collection on `Sa108Report` (e.g.
   `dds_disposals: list[Sa108Row]`). The gain is income; a loss is
   reported but flagged as (generally) not allowable.

3. `src/banking_pipeline/cli.py` — emit `sa106-deep-discounted.csv`
   (date, isin, name, quantity, proceeds_gbp, cost_gbp, gain_gbp) and a
   `summary.txt` section: `deep discounted gains taxed to income` and
   `deep discounted securities losses`, mirroring Pictet.

4. Tests: a DDS-flagged disposal appears on the DDS CSV and **not** on
   `sa108-disposals.csv`; an un-flagged bond still routes to CGT.

Scope note: don't attempt to *detect* DDS from price/coupon — it depends
on issue terms HMRC publishes. Treat it as user-asserted metadata, like
reporting status.

---

## Prompt 12 — Excess reportable income (ERI) and equalisation

This is the dominant gap. UK reporting funds that accumulate rather than
distribute report **excess reportable income (ERI)** — income deemed
arising to holders at the fund's reporting-period end. ERI is taxable
(as dividend or interest, per the fund's nature) and, crucially, is
**added to the CGT base cost** on a later disposal (you've already been
taxed on it). **Equalisation** adjusts both figures for units acquired
mid-period (the income-equalisation portion of the first distribution/ERI
is a return of capital, reducing base cost rather than being income).

In the comparison, ERI/equalisation explains most of Pictet's 47,937.64
interest and a large part of the 19,187.90 dividends, **and** explains
why our CGT gains are ~19k too high (we never uplift base cost for ERI).
The pipeline models neither today.

This is a substantial feature and depends on a data source funds publish
(ERI is not on the Pictet trade advices). Treat it like the HMRC
rates / commodities tables: a user-maintained input.

Changes:

1. New user-maintained `data/eri.toml` (gitignored, committed
   `.example.toml`), one entry per fund reporting period:

   ```toml
   [[eri]]
   isin = "LU0767911984"
   period_end = 2024-06-30          # fund's reporting-period end
   fund_distribution_date = 2024-12-30   # the deemed-income date (+6 months)
   income_type = "interest"         # interest | dividend
   eri_per_unit = 0.4521            # in the fund's currency
   equalisation_per_unit = 0.0      # per unit, for units bought in-period
   currency = "EUR"
   ```

   Pydantic model + `load_eri(path) -> dict[str, list[EriEntry]]` keyed
   by ISIN, validated like `commodities_metadata`.

2. New `src/banking_pipeline/tax/uk/eri.py`:
   - For a tax year, for each held fund with ERI entries whose
     `fund_distribution_date` falls in the year: compute the holder's
     ERI = `units_held_at_period_end × eri_per_unit`, converted to GBP
     at the distribution date (reuse `currency.to_gbp` + the
     `GbpRateSource`). Determining `units_held_at_period_end` requires
     replaying the ledger holdings to that date — add a small per-ISIN
     position-as-of helper (or reuse the section 104 running balance).
   - Split results by `income_type` into dividend vs interest ERI.
   - Compute the **base-cost uplift** per ISIN = cumulative ERI (net of
     equalisation) on units still held, to feed CGT.

3. CGT integration (`tax/uk/sa108.py` / `section_104.py`): add the ERI
   base-cost uplift to the section 104 pool cost (and reduce by
   equalisation). Simplest: treat each ERI event as a zero-quantity,
   positive-cost adjustment to the pool at its distribution date, and
   equalisation as a negative-cost adjustment. Document the model
   clearly — it's the load-bearing correctness piece.

4. CLI / output: extend the income side. ERI dividend → folds into
   `sa106-dividends.csv` (or a clearly-marked `sa106-eri.csv`); ERI
   interest → the deferred `sa106-interest.csv` (build it now, since ERI
   is the main interest source — see the prompt-7 deferral). `summary.txt`
   gains overseas-interest and ERI lines mirroring Pictet's
   "Excess Reported Income (Interest/Dividend)" and equalisation totals.

5. `config.py` — `eri_path: Path | None` defaulting to `data/eri.toml`.

6. Tests in `tests/tax/uk/test_eri.py`: ERI income computed for a holding
   held across a period end; equalisation reduces a mid-period buyer's
   income and base cost; the base-cost uplift reduces a later disposal's
   gain; income split dividend vs interest by `income_type`.

Validation, in addition to the shared checks: reconstruct the 2024/25
figures against Pictet for one ERI-bearing fund end to end and confirm
the income and the disposal gain both move toward Pictet's numbers.

Known follow-ups left after this set: actual CGT/income **rate**
arithmetic and the annual exempt amount; private-equity transactions
(Pictet section 5); and consolidation across multiple Pictet accounts.

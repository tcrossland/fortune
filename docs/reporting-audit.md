# Reporting support — audit (2026-06-17)

A point-in-time assessment of the two "reporting" subsystems: the **UK
tax reporting-status pipeline** (reporting/non-reporting → SA108/SA106
routing) and the **analytical report commands** (concentration,
net-worth, allocation, portfolio-allocation, income, trial-balance,
reconcile). It records what is solid and what is missing/weak, prioritised
for remediation.

Snapshot, not living guidance — file:line citations reflect the tree at
the date above and may drift. When a remediation item is adopted, move it
to [backlog.md](backlog.md) (or fix it and note it in the CHANGELOG); this
file is the rationale for *why* those items exist.

**Headline risk:** a disposal whose ISIN has `reporting_status = "unknown"`
(or no metadata at all) is silently excluded from **every** computed tax
figure and is **not** caught by `--strict`. The tax return looks clean
while understating. This is P0.

> **Update (2026-06-17): P0 resolved.** `--strict` now fails on
> unclassified *and* unmatched (zero-cost) disposals across `tax-report` /
> `tax-forecast` / `fig-advice` (the A2 gate; A1/A3 are now catchable). See
> the CHANGELOG. The remaining P1/P2 items below stand.

---

## Part A — UK tax reporting-status pipeline

### Solid

- **Commodity-code validation** (`commodities_metadata.py:86`) — checksum-
  validates 12-char ISINs, accepts only 11-char Pictet structured-product
  refs, rejects 12-char checksum failures (typos) at load. Tested.
- **Duplicate-ISIN load guard** (`commodities_metadata.py:193`) — raises
  rather than silently keeping the last. Tested.
- **Single ISA tax-exempt choke point** (`cli/tax.py:131`) — `is_tax_exempt`
  filtered once before every `compute_*`/`match_history`; no per-report
  drift. Matches the documented invariant.
- **WHT arithmetic invariant at construction** (`models.py:736`) —
  `gross_income − withholding_tax == amount` (±0.01) enforced by a
  pydantic validator, so a template bug fails at construction, not in a
  figure. Tested.
- **Accrued-income-scheme split** (`sa108.py:105`) — `abs(accrued_interest)`
  removed from both cost and proceeds (interest is income, not capital).
  Tested.
- **ERI measurement-vs-distribution-date subtlety** (`eri.py:101`,
  `tax_year.py:56`) — units measured at period end (distribution − 6
  months), income/FX dated at distribution, base-cost uplift applied to
  the pool at the distribution date. The hardest part of ERI; tested.
- **Section-104 event interleaving** (`section_104.py:170`) — acq→adj→disp
  ordering on a shared date; ERI uplift ignored on an empty pool. Tested.
- **`to_gbp_all` is all-or-nothing per row** (`tax/uk/currency.py:66`) —
  returns `None` if any of gross/wht/net fails, so SA106 never emits a
  half-converted row; the gap is recorded.
- **Deeply-discounted / non-reporting routing** (`sa108.py:200`,
  `cli/tax.py:246`) — DDS routed out of CGT; non-reporting routed to
  offshore-income-gains and excluded from SA108. Tested.
- **FIG loss-disallowance threaded through the loss chain**
  (`cgt_allowance.py:226`, `fig_advice.py:92`) — the chain is rebuilt per
  claim subset so a forfeited foreign loss's knock-on to later years is
  reflected. The non-obvious correctness point; tested.

### Missing / weak (prioritised)

| # | Sev | Finding |
|---|-----|---------|
| A1 | **HIGH** | **`unknown`-status disposal lands on no tax figure.** `CGT_STATUSES = {"reporting","uk-domestic"}` (`cgt_allowance.py:41`), so an `unknown` disposal is excluded from SA108 (`cli/tax.py:190`), from offshore-income-gains (route is `== "non-reporting"`, `:246`), from the loss chain (`cgt_allowance.py:201`) and from the forecast (`:866`). It surfaces **only** as a `WARN_UNCLASSIFIED` text line (`cli/tax.py:590`). A real gain on an unclassified holding vanishes from the return. |
| A2 | **HIGH** | **`--strict` covers rate gaps only.** The strict raise is inside `if gaps:` where `gaps = sa108/sa106/eri.missing_rates` (`cli/tax.py:819`). It does **not** fire on `unclassified` (A1) or `unmatched_isins` (zero-cost disposals, A3). A CI gate on `--strict` passes while the return is materially wrong. Same gap in `tax-forecast` (`:1174`) and `fig-advice` (`:1502`). |
| A3 | **HIGH** | **Zero-cost fallback on an empty pool.** A disposal with no prior acquisition (incomplete history / missing `opening-positions.toml`) is matched at **zero cost** → 100% gain (`section_104.py:195`), flagged only via `unmatched_isins` → `summary.txt` (`sa108.py:189`). With A2, a missing opening position inflates a gain and passes `--strict`. Detection is tested; enforcement isn't. |
| A4 | **HIGH** | **`fetch_reporting_funds.py` has no failure handling and rewrites in place.** `urlopen` has no try/except, `archive.read("content.xml")` hard-codes the ODS member, then `METADATA.write_text` overwrites `commodities.toml` (`scripts/fetch_reporting_funds.py:48,63,93`). Only a `tomllib.loads` syntax-check guards the write — no atomic write, no backup. A partial download that still parses, or a gov.uk restructure, can silently truncate the file. **Zero tests.** |
| A5 | MED | **ISIN token regex over-matches.** `_ISIN_TOKEN` scans the whole `content.xml`; any checksum-valid 12-char token upgrades a holding (`fetch_reporting_funds.py:43,66`). A false-positive upgrade mis-routes a non-reporting disposal (income) to CGT. |
| A6 | MED | **ERI dividend/interest split ignores `distributions_as_interest`.** The split is the per-entry TOML `income_type` (`eri.py:85`), while SA106 *distributions* derive it from the commodity flag (`sa106.py:130`). A bond fund tagged `distributions_as_interest=true` but missing `income_type="interest"` on its ERI rows splits inconsistently for the same security. No cross-check, no test. |
| A7 | MED | **SA106 `GB`-prefix drop can lose foreign income.** Country is `(withholding_country or isin[:2]).upper()`; a `GB` result drops the row as UK income (`sa106.py:110`). A GB-listed depositary receipt over a foreign asset (the case `uk_situs` exists for) is silently dropped from SA106 with no warning — unlike disposals, dropped income gets no flag. |
| A8 | LOW | **`fetch_reporting_funds.py` line-coupled TOML rewrite** (`:44`) assumes `isin` precedes `reporting_status` per block on simple lines; a reordered block or inline table mis-associates or misses the upgrade silently. No test. |
| A9 | LOW | **`infer_issuer` first-substring-match is collision-prone** (`commodities_metadata.py:46`) — bare 3-char fragments (`UBS`) and ordered overlaps (`JPM `/`JPMF`). Report-only (not tax), so bounded; happy-path tested only. |
| A10 | LOW | **Test gaps for the silent-failure modes** — no test asserts an `unknown` disposal is excluded from the chain/forecast (only the summary string is checked), none for the fetch script (A4/A5/A8), none for A6/A7. The highest-risk behaviours are the untested ones. |

---

## Part B — Analytical report commands

### Solid

- **Shared valuation engine** (`valuation.py:178`) — `value_holdings` is
  consumed by concentration/net-worth/allocation/portfolio-allocation as
  peers; gross-long/net-cash/cash-netting/rate-gap semantics can't drift.
- **Leverage / cash-netting** (`valuation.py:247`) — gross-long sums only
  positive security values; net cash is signed (negative Lombard); cash
  netted across portfolios by currency, so a leveraged book never reads
  >100%.
- **GBP rate-gap plumbing degrades safely** (`tax/uk/currency.py:38`,
  `report_format.py:40`, `fx/gbp_rates.py:95`) — `to_gbp` returns `None`
  not a wrong figure; every report records a `RateGap` and renders the
  same warning naming the exact HMRC CSV row; a missing CSV degrades to
  `NullSource`. Well-tested.
- **Missing-binary handling** (`bean_query.py:61`, `cli/reports.py:467`,
  `cli/rebuild.py:306`) — `bean-query`/`bean-check` absence is a warning +
  exit 0 (opt-out), not a crash. Uniform across trial-balance/reconcile.
- **Reconcile delegates the drift verdict to bean-check**
  (`reconcile.py:20`) — failures matched back to the asserting source
  line; tolerance honoured by construction. Strongest test coverage.
- **Same-date dedup** (`net_worth.py:101`, `allocation.py:122`) — raws
  keyed by `(portfolio, date)` then commodity, so two same-"as at"
  statements can't double-count.
- **CSV/Markdown share one computed model** — both render from the same
  dataclass; `money`/`gbp` differ only in symbol/separators.
- **Income economic filtering** (`income.py:110`) — positive-amount guard
  drops overdraft interest (an expense); narration filter drops ISA
  deposits (contributions). Bond-fund reclassification mirrors SA106.

### Missing / weak (prioritised)

| # | Sev | Finding |
|---|-----|---------|
| B1 | **HIGH** | **Valuation-source divergence: trial-balance vs the statement reports.** trial-balance values the *ledger's current positions* (`bean-query value()`, latest marks, as-of today; `trial_balance.py:41`); concentration/net-worth/allocation/portfolio-allocation value the *latest statement snapshot* at the statement date (`valuation.py:250`). The two families answer "what is my net worth?" with different methods, dates, and inclusion rules, and **nothing reconciles or documents the gap**. A user comparing totals sees unexplained discrepancies. |
| B2 | **HIGH** | **`net-worth` has no `--strict` flag** (`cli/reports.py:115`) — unlike every other valued report. A net-worth point understated by a holding that fails to convert warns in the `.md` (`net_worth.py:184`) but the CLI never exits non-zero, so a CI gate can't catch it. |
| B3 | MED | **trial-balance not wired into `[post.reports]`** (`cli/rebuild.py:697`, `batch_config.py:170`) — the one ledger-faithful report is the only one `rebuild` won't auto-refresh. (Reconcile has its own `[post.reconcile]`.) |
| B4 | MED | **`unclassified` flagging inconsistent.** Concentration (`concentration.py:188`) and portfolio-allocation (`portfolio_allocation.py:229`) render an "unclassified holdings" section; **allocation and net-worth drop it** (`allocation.py:177`, `net_worth.py:145`), so an allocation chart lumps un-classified holdings into `unknown` with no "add to commodities.toml" pointer. |
| B5 | MED | **`missing_prices` flagging inconsistent.** `NetWorthTimeline` carries it (`net_worth.py:67`) but `render_markdown` never renders an unvaluable-holdings section (`net_worth.py:155`); allocation does (`allocation.py:232`). A net-worth point understated by a no-mark holding is invisible in the `.md`. |
| B6 | MED | **Forward-fill carries a wound-down portfolio indefinitely** (`net_worth.py:14`, `valuation.py:151`) — an empty valuation parses to no holdings, so it never refreshes the as-of fill; a closed account's last non-empty snapshot lingers until a later data-bearing statement supersedes it. Affects allocation identically but its docstring doesn't mention it. Net worth can be overstated by a stale snapshot. |
| B7 | MED | **trial-balance flags a whole multi-leg account on the first failing leg** (`trial_balance.py:108`) — an account with valued cash + one unmarked ISIN is dropped from the GBP total *entirely*, understating `assets_gbp` by the valued legs too; the warning names the account but not that its good legs were excluded. |
| B8 | MED | **Thin tests at the formatting / CLI-wiring layer** — `value_holdings` is exercised only transitively; `report_format.py` (the `pct` zero-total `—`, negative-leverage % edge cases) has **no** dedicated tests; no `CliRunner` test writes real `.md`/`.csv` for concentration/allocation/net-worth/income/trial-balance, so the `--strict` exit paths are untested. |
| B9 | LOW | **Property folding absent from trial-balance / income** (`valuation.py:54`) — arguably correct (property is off-ledger / generates no income), but means `portfolio-allocation` net worth ≠ `trial-balance` assets, compounding B1; undocumented in the trial-balance docstring. |
| B10 | LOW | **`pct`/`_weight` duplicated** — `report_format.py:32` `pct` exists, yet concentration/allocation/portfolio-allocation each re-implement a CSV `_weight` with the same zero-total guard (a future change touches four places). |
| B11 | LOW | **trial-balance `RateGap` carries the currency as the ISIN** (`trial_balance.py:124`) — renders `"JPY 2026-06 (JPY)"`; the `(isin)` slot is noise, inconsistent with other reports. |

---

## Cross-cutting themes

1. **Silent understatement is the dominant risk class.** Both subsystems
   prefer to exclude-and-warn over emitting a wrong number — correct in
   spirit, but the *warnings are text-only and `--strict` is blind to most
   of them* (A1/A2/A3 on tax; B2/B4/B5 on reports). The gate exists; it
   just doesn't cover the worst modes.
2. **Flagging is the most inconsistent surface.** `--strict`, `unclassified`
   and `missing_prices` each have a *different subset* of reports that
   surface them — despite the shared `report_format` helpers existing
   precisely to make this uniform.
3. **Two answers to "what is my net worth?"** The ledger-based
   (trial-balance) and statement-based (the four valuation reports)
   families don't reconcile and nothing documents why (B1/B9).
4. **Coverage is strong at the logic layer, thin at the edges** — the
   formatting layer, the CLI wiring, and every silent-failure path are the
   least-tested (A10, B8).

## Remediation roadmap

**P0 — tax correctness (silent understatement of the return) — ✅ DONE (2026-06-17)**
- A1 + A2 + A3: `unclassified` disposals and `unmatched_isins` (zero-cost)
  disposals now fail `--strict` across `tax-report` / `tax-forecast` /
  `fig-advice` (shared `_understatement_blockers` helper, three call
  sites). The CI gate is no longer blind to the two worst ways a return
  can be wrong.

**P1 — robustness & consistency**
- A4/A5/A8: harden `fetch_reporting_funds.py` — atomic write + backup,
  network/format guards, tighter ISIN acceptance, and a test.
- B1: document the trial-balance-vs-statement valuation split (in the
  trial-balance docstring + README), or add a reconciliation note; decide
  whether they should ever agree.
- B2: add `--strict` to `net-worth`.
- A6/A7: cross-check ERI `income_type` against `distributions_as_interest`;
  warn on the SA106 `GB`-prefix drop when `uk_situs` says foreign.

**P2 — polish & coverage**
- B3: wire trial-balance into `[post.reports]` (a `trial_balance` toggle).
- B4/B5: render `unclassified` + `missing_prices` uniformly in allocation
  and net-worth.
- B6: surface the stale-forward-fill caveat in the rendered report, not
  just the docstring.
- B7/B11: partial-value multi-leg trial-balance accounts; fix the cosmetic
  `RateGap` slot.
- A9/A10/B8/B10: adversarial `infer_issuer` test, the silent-failure-mode
  tests, `report_format` tests, and de-duplicate the `_weight` formula.

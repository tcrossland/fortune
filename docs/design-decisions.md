# Design decisions

The *why* behind the non-obvious choices, gathered so the rationale
outlives the implementation briefs it was first written into. For *how*
the code is laid out see [architecture.md](architecture.md); for the hard
constraints that bind every change see [CLAUDE.md](../CLAUDE.md); for *how
to use* the tool see [README.md](../README.md).

## GBP as posting metadata, not embedded `{cost}`

The ledger stays in the native trade currency (EUR/USD/…). GBP figures
ride along as posting **metadata** (`gbp-rate`, `trade-date`) and in the
structured `transactions.jsonl` sidecar, and **all** GBP arithmetic —
the section 104 pool, same-day and 30-day matching, gain/loss — happens
in the `tax-report` stage, never in beancount.

Why:

- **Beancount's booking methods don't implement UK matching rules.**
  Same-day and 30-day "bed and breakfast" matching sit above the section
  104 pool; whatever `AVERAGE`/`STRICT`/`FIFO` computed would be an
  approximation the tax stage has to override — two competing answers for
  one number. So UK matching lives in `tax/uk/section_104.py`, fed from
  the sidecar, and beancount is left to do cash-leg bookkeeping.
- **Reconciliation against source statements stays trivial.** Keeping the
  ledger in the statement's own currency means a `bean-check` balance
  assertion compares like with like, with no per-currency tolerance
  interaction from an embedded GBP cost.
- **Rates change; history shouldn't have to.** HMRC republishes monthly
  averages, and a user might switch monthly→daily mid-project. Metadata
  means re-running the tax report; embedded `{cost}` would mean
  regenerating ledger history.
- **Metadata applies symmetrically** to every posting (the GBP value of
  an FX-funded purchase's cash leg is worth carrying too), and keeps the
  writer surface small — builders append metadata lines rather than
  restructuring cost annotations, which preserves the "byte-identical
  when `gbp_rate is None`" back-compat contract.

Trade-off accepted: no Fava-visible realised gains directly on the
ledger. The figures live in the `tax-report` CSVs instead.

## Valuation reports forward-fill the GBP rate; tax uses the exact month

The same `HmrcMonthlyAverageSource` feeds two paths with **opposite**
requirements on a missing month:

- **UK tax** (`tax-report`) needs the *exact* trade-month HMRC average —
  section 104 cost basis is defined by it, and substituting a neighbouring
  month would be wrong. So `to_gbp(..., source=…)` on the tax path uses the
  raw source: an absent month yields `None`, and the transaction's
  `gbp_rate` is left unset rather than guessed.
- **Mark-to-market valuation reports** (concentration, net-worth,
  allocation, portfolio-allocation, mandate-returns — everything through
  `value_holdings`) want the *latest known* rate. A month-end statement is
  dated to the following day (a 30 June snapshot carries `on_date` 1 July),
  so it asks for a month HMRC hasn't published until that month closes.
  Dropping every non-GBP holding as a `RateGap` collapses the snapshot — a
  ~£-millions phantom drop on the newest row, purely a calendar artifact.

So `value_holdings` wraps its source in `ForwardFillRateSource`, which
walks back month-by-month (bounded to 12) to the most recent published
rate. This matches the balance sheet, which already values at the latest
rate on or before the as-of date.

Why the wrap lives in `value_holdings`, not in `get_rate`:

- **`get_rate` is shared with tax**, whose correctness depends on the
  exact-month lookup. Forward-filling there would silently corrupt CGT.
  The valuation path is the only one that should relax the lookup, so the
  relaxation is applied there and nowhere else.
- **The bound surfaces genuine holes.** A one-month leading-edge gap is
  expected and filled; a multi-month absence (a CSV the user forgot to
  update) walks past the 12-month cap and still reports a `RateGap` rather
  than valuing at a year-stale rate.
- **Wrapping a rateless source is a no-op** — `NullSource` stays `None`
  through the walk-back, so the `--strict` "understated snapshot" gate and
  the rate-gap warnings are preserved for truly unconvertible holdings.

Trade-off accepted: within the 12-month window a real (2–11 month) hole is
filled silently with a stale rate and isn't flagged — acceptable because
the only recurring gap is the single unpublished current month.

## `mandate-returns` counts distribution income as return

The mandate return is computed from statement holdings: a period's market gain
is `Σ qty_held × Δ(unit price)`, and the leftover `ΔValue − gain` is treated as
an inferred external flow (a deposit/withdrawal) and excluded from performance.
That's exactly right for a price move on accumulating funds — but a
*distributing* fund pays income out as cash, which lands in the portfolio value
without moving the unit price. Left alone that payout falls into the leftover
and is stripped from the return, understating TWR/MWR by the distributed yield.

So `distribution_income` reads the sidecars and adds each period's cash
distributions back into the gain (and removes them from the flow). Only fund
distributions count — `DIVIDEND_TYPES` rows carrying an ISIN (this includes a
bond fund's payout, which the writer books as a dividend doctype even when it's
economically interest) — because they carry the holding's portfolio account
number and land as cash. Bare current-account interest (no ISIN) stays out: a
separate, immaterial leak with no clean attribution.

The one trap: income is folded in **only for a portfolio with tracked
positions**. The P mandate's by-name holdings aren't resolved to ISINs in the
valuation path (`raw_from_statement` isn't given the name→ISIN index), so P's
snapshots carry no securities and its value base is a tiny residual-cash figure.
Dividing P's distributions by that base compounds to a nonsense three-figure
percentage. Gating on `prev.positions` leaves P at its prior (position-less)
behaviour while the aggregate and the K mandate — which do value their holdings
— get the correction. The whole-mandate effect is a sub-percentage-point uplift;
the book is mostly accumulating funds, so the residual understatement was small
but real. (Reuses the recognition logic behind `income.py`; that report
was never wrong — it records distributions as income correctly — it was only
the source of the identifier.)

## A recognised nil statement retires its portfolio from the timeline

The net-worth and allocation timelines combine portfolios statemented on
different cadences with an as-of forward-fill: each date sums every
portfolio's latest snapshot on or before it, so a portfolio between statements
keeps contributing its last value. Correct for a dormant account — wrong for a
**closed** one. An empty statement parses to no holdings, so it never creates a
snapshot, so the forward-fill carries the account's last non-empty value
forward indefinitely, overstating every later point. This went live when the
Vanguard ISA wound down.

The naive fix — emit a zero snapshot whenever a statement parses to zero
holdings — is unsafe, because an empty parse is **ambiguous**: a genuinely
closed account and a *parse failure on a still-funded account* look identical,
and zeroing the latter produces a phantom net-worth collapse (the very failure
the GBP forward-fill decision above was written to avoid). The two errors
aren't symmetric: carrying a stale value overstates by a bounded, known amount;
zeroing a live account understates by an alarming, wrong one.

So `drained_portfolio_snapshot` keys the retirement on the statement's **own
explicit nil total** — a Vanguard ISA regular statement whose *current-column*
`Account total` is £0.00 (`parse_isa_nil_statement`) — not on the absence of
parsed holdings. A still-funded account always prints a non-zero current total,
so a parser miss on a live statement can never be mistaken for a wind-down. On
that signal the builder emits a zero-value cash snapshot with the portfolio
string and date built exactly as the funded snapshot's, so the forward-fill
supersedes the last non-empty value and the account retires at its drain date.

Scope is the two **timeline** reports (net-worth, allocation) — the
latest-snapshot reports (concentration, portfolio-allocation) don't have the
lingering bug, since a drained account simply isn't in the current snapshot.
The residual caveat, now narrowed in both reports' output: a portfolio that
*stops statementing entirely* — with no closing nil statement — still lingers,
because there is no nil total to key on. (Audit item B6, previously closed as a
documented caveat, now behaviourally fixed.)

## Reconcile delegates its verdict to `bean-check`

`reconcile` compares statement-asserted balances against
ledger-computed ones, but it does **not** decide what counts as drift —
it runs `bean-check` once and treats an assertion as drifted iff
`bean-check` flagged it (matched back by `<file>:<line>`). Consequences:

- **No new dependency.** beancount v3 split `bean-query` into a separate
  uninstalled package; building on the already-wrapped `bean-check`
  avoids pulling in another GPL-family dep (and we still never `import
  beancount` — output is text, validation shells out).
- **Agrees with a real load by construction.** beancount's
  inferred-from-decimals tolerance is the verdict, so reconcile can't
  disagree with what `bean-check` itself would say. The originally
  planned half-the-smallest-unit tolerance replication was dropped for
  exactly this reason — it could only have introduced disagreement.

`reconcile` adds *over* `bean-check`: the full grid (not just the first
failure), drift magnitude/direction, the earliest date each account
diverged, and coverage gaps (statement months with no assertion).

## Completeness is checked by transaction, against the statement cash ledger

`reconcile` checks *balances*; `completeness` checks *transactions*. They
catch different gaps. A balance assertion only fires monthly and only
trips if an error lands on the wrong side of a checkpoint — two
equal-and-opposite mistakes, or an error after the last checkpoint, slip
through. The Pictet current-account statement, by contrast, is the
authoritative line-by-line list of every cash movement for its period, so
diffing it against the ingested sidecars localises the *exact* missing
advice. Three choices make that diff trustworthy rather than noisy:

- **Reads the sidecars, not the ledger** — same substrate invariant as the
  tax pipeline; the ledger is a rendering, the `*.transactions.jsonl` are
  the data.
- **Sign from the running balance, not the column.** `pdftotext`/pypdfium2
  don't preserve the DEBIT/CREDIT columns reliably, and pypdfium2 (the
  pipeline's loader) also repeats the page header and drops the page-break
  balance. So each movement's sign is recovered from the printed
  running-balance delta, which doubles as a self-check: a row that doesn't
  reconcile to ±its magnitude raises rather than emitting a
  plausible-but-wrong line. The balance is tracked per currency so a
  repeated same-currency page header can't wipe the chain.
- **Knows what legitimately isn't there.** Securities settlements
  (`switch_*`, `liquidacion_recepcion_de_valores`) post to a `Switch`
  sub-account or an `Equity:…:Transfers` in-specie leg, never the EUR/USD
  current account, so they're excluded — not flagged as drift. The
  FX/transfer counter-leg (one sidecar row, two statement lines) is
  expanded so both legs match. Validated against 2021–2023: zero
  unexplained findings.

## The Lombard loan is negative cash, not a liability

The Pictet Lombard facility is modelled as a **negative cash balance** on
the relevant `Assets:Pic:<portfolio>:<CCY>` sub-account — there is no
`Liabilities:` account for it. This looks unorthodox (a loan is a
liability), but it's the source-faithful choice and the alternative would
break more than it fixes.

Why:

- **The statement reports it as negative cash, and nothing else.** Pictet
  prints one negative balance per currency sub-account; the only other
  loan-related line is the `C/A Limit` (the *facility size*, not a drawn
  amount — the balance parser skips it). So the single source-of-truth
  number is the negative cash. A `bean-check` balance assertion compares
  like with like and ties out by construction. Splitting it into a
  separate liability account would mean fabricating a cash-vs-loan split
  the statement never makes — and there's no balance to assert the
  liability account against.
- **There's no reclassification boundary.** A revolving facility has no
  discrete drawdown event: the cash line just goes negative as you spend.
  Nothing distinguishes a "loan" from a transient overdraft, so any
  liability model needs a threshold or manual tagging the data can't
  support. Cash is commingled per currency, so a payment can't be
  attributed to "loan-funded" against a pooled balance either.
- **The liability *view* belongs in the reports, not the ledger.**
  `net_worth` / `concentration` / `allocation` already treat negative
  cash as the loan and report it separately from gross long (net worth =
  gross long + signed net cash). You get the balance-sheet liability
  presentation without corrupting the source-faithful ledger — see the
  interactive-balance-sheet plan, which derives its Liabilities bucket
  from the sign of cash rather than from an accounting tree.

This is the standard beancount treatment of a margin/overdraft facility.
A *distinct fixed-term loan* (its own account, its own statement line,
a scheduled principal) would be different — that has a balance to assert,
so it would warrant a real `Liabilities:` account.

## The self-to-self counter-leg is `Equity:Transfers`, not `Assets`

A Pictet self-to-self payment (Pictet ↔ Revolut) is **one** `Transaction`
carrying both legs; the writer books the external-account leg to
`Equity:Transfers:<bank>:<ccy>` (resolved from `beneficiary_bank_map`), not
to `Assets:<bank>:<ccy>`. The destination account's *own* day-to-day
activity is never imported.

Why:

- **An `Assets:` leg would be a phantom balance.** With only the
  Pictet-facing legs recorded, `Assets:Revolut:*` is not a cash position —
  it's "net moved between Pictet and Revolut". It stood at large positives
  (Pictet→Revolut funding dominates the history) that the balance sheet —
  which sums `Assets`/`Liabilities` postings — counted at face value,
  overstating net worth by that amount. Booking the leg to Equity (a
  *perimeter crossing*, not a holding) excludes it by construction: the
  balance-sheet query is `Assets|Liabilities` only, so no report-side filter
  is needed. Money sent out of the perimeter drops tracked net worth; money
  received into it raises net worth — both truthful given the destination is
  untracked.
- **`Transfers`, not `Drawings`.** The flow is bidirectional (Revolut→Pictet
  seeded the portfolio), so the one-way "withdrawal" reading is wrong for
  the inbound legs. `Equity:Transfers` is direction-neutral and matches the
  existing `Equity:Opening-Balances` idiom.
- **Not `Equity:Uncategorized`.** This is a *named* account; the
  no-`Uncategorized` invariant targets the elastic placeholder the
  self-to-self path was built to eliminate, not all Equity.
- **Tax substrate untouched.** Only the *rendered* account string changed;
  `Transaction.counter_account` still carries the bank segment, so the JSONL
  sidecars, `tax-report`, and `reconcile`/`completeness` (which read the
  sidecars, not this leg) are unaffected.

The escape hatch if the external account is later tracked for real: the
dormant Revolut **CSV importer** writes `Assets:Revolut:Personal:*`. If those
imports land, the inbound Pictet transfer would want to net against the real
balance there rather than sit in `Equity:Transfers` — reconciling the two
naming schemes is a separate task, deliberately out of scope here.

## Strict-mode dispatch on an empty template result

When a registered template returns `[]`, `HybridExtractor` does **not**
fall through to the regex/LLM path. It logs a WARN and returns `[]`
(or raises under `--strict`). This is deliberate: falling through
historically papered over template regressions with
`Equity:Uncategorized`-balanced placeholder entries that landed silently
in the ledger. Surfacing the empty result instead means the next
`bean-check` notices the imbalance — a loud failure beats a silent wrong
number. Doctypes that *always* emit nothing are listed in
`NO_OUTPUT_DOCTYPES` and short-circuit cleanly.

A doctype that **normally** emits but is legitimately empty on some inputs
(a nil-activity `vanguard_regular_statement` with no `Activity` section)
doesn't fit a doctype-level set — putting it in `NO_OUTPUT_DOCTYPES` would
blind strict mode to real regressions on the statements that *do* carry
activity. So templates may implement an optional `is_expected_empty(doc)`
hook (duck-typed; see `templates.Template`), consulted only when `extract`
returned `[]`, to declare a *specific document* a legitimate empty. It's
deliberately conservative — keyed on a structurally-empty input (no
`Activity` section), not on "extraction found nothing" — so a statement
whose rows drifted still surfaces as a regression.

## Switch legs pair on order date, not amount-netting

A Pictet fund switch is two advices — `SWITCH_SALIDA` (sell) and
`SWITCH_ENTRADA` (buy) — that should share one beancount `^<link>`.
`switch_pairing` reconciles them. The obvious key is the
`Assets:<prefix>:<portfolio>:Switch:<ccy>` clearing leg: the salida posts
proceeds *in*, the entrada draws cost *out*, so the pair "should" net to
zero. It doesn't, for **FX switches**: when the entrada buys a fund priced
in another currency, its clearing amount is an *independent* FX conversion
of the underlying buy, not the same cash the salida produced — the two
legs land ~0.33 apart, not within a cent. An amount-netting tolerance loose
enough to absorb that drift is also loose enough to mis-pair two distinct
same-day switches of similar size.

So the **primary key is the shared order date** (Pictet's
`Fecha de la orden`, captured into `Transaction.order_date`): both legs of
one switch always carry the same order date, even when their clearing
amounts don't tie. The matcher buckets by `(account, clearing currency,
booking date)` and pairs legs that share an order date, with no amount gate
for the unambiguous 1:1 and 1:many cases — the four shared facts are
conclusive. Amount-netting survives only as a conservative *fallback* for
legs that carry no order date, and it refuses to guess on a tie (two
indistinguishable switches are left unpaired and warned, never mis-linked).
This is why `order_date` is a model field even though the writer never
renders it: it exists to make FX-switch pairing deterministic.

## UK residence and the FIG regime

The tax stage assumes UK arising-basis residence across the whole
history unless `uk_residence_start_date` is set. Two corrections then
apply, both config-driven and both leaving the section 104 pool
untouched — acquisitions feed it whenever they happened; only the
taxable *output* is residence-filtered:

- **Pre-residence (split-year).** Income and gains arising before the
  arrival date drop out (the non-resident / overseas part of a split
  year); whole tax years before arrival are skipped.
- **4-year FIG claim** (`fig_claim_years`, from 2025-26). For an eligible
  year, foreign income and non-UK gains are relieved to nil but the
  personal allowance and CGT annual exempt amount are forfeited — *and*
  that year's foreign losses are disallowed (which is why the claim
  decision is multi-year; see `fig-advice`). Foreign-vs-UK situs is
  `CommodityMetadata.resolved_uk_situs` (the optional `uk_situs` flag,
  else derived from domicile / `uk-domestic` status).

**Documented simplifications** (not modelled — verify against HMRC
guidance; none of this is tax advice):

- The 10-prior-non-resident-years FIG eligibility test isn't checked —
  configuring an arrival date *asserts* eligibility.
- ERI income is attributed to the whole arrival year, not split at the
  arrival date.
- Temporary-non-residence clawback is not modelled.
- Former-remittance-basis transitional rebasing / TRF is not modelled.
- `tax-forecast` / `fig-advice` use year-to-date actuals (no run-rate
  projection) and England/Wales/NI rates for a single taxpayer.

## Tax reports file by their effective date, not the content fiscal date

A Spanish tax report's archive filename date is the **effective date** carried
in the source download filename (`-<YYYYMMDD>` = Pictet's Publication/Effective
date), taken in preference to the as-of date scraped from the document body.
The content scraper (`archive._pictet_tax_as_of`) is only a fallback for
dateless legacy names, and a filename/content disagreement is logged
(`archive.tax_report_date_mismatch`).

The content's fiscal-reference date can go **stale**: Pictet issued a run of
Oct–Nov 2023 unrealised reports that were re-valued live each day but kept a
frozen `Al 10.09.2023` label. Dating by content collapsed 31 distinct daily
valuations onto one canonical name (`Unrealised PL 20230910.pdf`) — a silent
loss. The effective date in the filename never drifted, and the two coincide
on every normal report, so the rule is a no-op except where it corrects a
stale label. Realised reports (fixed data) are unaffected either way; it's
unrealised valuations where it earns its keep.

This deliberately re-couples filing to the source *filename* — which the
archive pass otherwise ignores by design, deriving everything from content —
because the filename's date is the more reliable signal. Full rationale +
audit in [archive/pictet-effective-date-filing.md](archive/pictet-effective-date-filing.md).

## Cost basis is a pluggable per-jurisdiction lens

The `holdings` report shows unrealised P&L, which needs a cost basis — and the
right cost basis depends on *which* tax jurisdiction you're viewing through.
UK residence gives **section 104** (averaged pooling, GBP); a possible future
move to Spain would give **FIFO in EUR** (and Spain does not recognise the ISA
wrapper). The two are computed by opposite means: UK section 104 is *computed*
from the sidecars by machinery we already have; the Spanish figures are best
*parsed* from Pictet's own EUR IRPF reports rather than re-implemented.

So cost basis is a `BasisLens` seam (`basis_lens.py`) — a neutral protocol
returning per-ISIN `HoldingBasis` — with the UK implementation in
`tax/uk/basis.py` (`UkSection104Lens`, wrapping `match_history` and reading its
residual pool). The seam imports no tax code, so each jurisdiction depends on
the seam without the seam depending on any jurisdiction; the report renders
whichever lens it's handed. `--basis uk` ships; `--basis es` is a reserved,
not-yet-implemented slot (it blocks on the Pictet-P&L parser). This keeps the
report shippable now while admitting the second jurisdiction as pure addition.

The UK lens deliberately **excludes ISA** trades (tax-exempt → no section 104
basis, mirroring the tax choke point): ISA holdings still appear from the
statement side with a blank cost, which is also what the ticker-vs-ISIN key
mismatch would produce. Cost basis reads the JSONL sidecars, never the ledger,
and is never fed back to the tax pipeline — it is a management view, not a
return figure.

ERI base-cost uplift is folded into the pool via
`cumulative_base_cost_adjustments` (eri.py), which runs `compute_eri` for every
tax year the `eri` table spans and merges the adjustments. This matters because
`compute_eri` scopes to a single year but the pool is cumulative: a *current*
cost basis needs every year's uplift. Plan + staged history:
[archive/holdings-cost-basis-report.md](archive/holdings-cost-basis-report.md).

## The holdings drift cross-check classifies timing vs gap, by settlement date

The `holdings` report cross-checks each statement quantity against the section
104 pool quantity. Any disagreement used to read as "a missing trade
confirmation or an ingest gap" — but at every month-end that fires a batch of
false positives. A Pictet month-end valuation is struck on **settled**
positions, so a trade executed at the end of the month but settling a few days
later (T+2/3, into the next month) is *not* on the mark, while the section 104
pool — keyed by trade date — has already moved. The statement lags the pool by
one settlement cycle, and the drift is a **timing** lead that clears when the
next statement lands, not a gap.

So the report classifies each drift: it sums the net signed quantity (buys +,
sells −) of ingested trades whose **settlement date** falls after the
statement date, and if that movement equals `pool − statement` the drift is
*timing*; otherwise it is a *gap* to investigate. A genuinely missing trade is
never in the sidecars, so it can't appear in the movement and stays a gap — the
classifier can only *downgrade* a drift that the ledger already fully explains.

The cutoff is **settlement date, not trade date**, for two reasons: the mark is
settlement-basis, and the statement's own label date can run a day ahead of its
true valuation (the effective-date filing dates a month-end report to the 1st),
so a late-month sale dated on the label date would be wrongly judged already-on
the mark. Settlement date sidesteps both — the explaining trades settle in the
following month regardless. The held-not-on-statement list is classified the
same way (a post-statement acquisition is *timing*; a stale statement or
un-ingested disposal is a *gap*). Because market value comes from the pre-trade
statement quantity while cost comes from the post-trade pool, a timing row's
unrealised P&L momentarily mixes bases — flagged as provisional; it reconciles
at the next statement.

## The tax pipeline feeds cumulative ERI base-cost uplift to the pool

`compute_eri(…, year)` scopes to a single tax year by design (income is
declared for one year), but the section 104 pool is **cumulative**: a disposal
in year Y whose units accrued ERI in an *earlier* year needs that earlier
base-cost uplift in its allowable cost. `_compute_tax_year` (`cli/tax.py`)
originally passed only the current year's `eri_result.base_cost_adjustments` to
`compute_sa108` and `match_history`, so a disposal that post-dated its ERI year
under-counted its cost and **overstated the CGT gain** (too much tax; the
loss-carry-forward chain consumed the same mis-costed rows). This was live for
2025-26 — the real `eri.toml` carried ERI for 2024-25 but most of those funds
were disposed in 2025-26 / 2026-27.

The fix: keep the year-scoped `compute_eri` for the **income** rows (SA106 only
declares the current year's ERI), but feed the **cumulative** adjustments
(`cumulative_base_cost_adjustments`, the same helper the holdings lens uses) to
the pool. It's safe to pass the whole-history set to a year-Y SA108 because
`match_disposals` interleaves adjustments chronologically, so a future-dated
uplift lands *after* this year's disposals and can't affect them. The cumulative
ERI rate-gaps (a prior-year ERI entry with no GBP rate now leaves the current
pool's uplift incomplete) fold into the report's `rate_gaps` /
`--strict` understatement channel — superseding the single-year set, which they
contain. Plan: [archive/eri-cumulative-basis-fix.md](archive/eri-cumulative-basis-fix.md).

**Corollary — non-resident-year ERI does not uplift the UK base cost.** The
uplift exists because the ERI was *already taxed* as income, so taxing it again
on disposal would be double taxation. ERI deemed to arise while the holder is
non-UK-resident (deemed date before `uk_residence_start_date`) is **not**
UK-taxable, so that predicate fails — it must not enter `eri.toml`. This is a
`eri.toml` data discipline, not code: when transcribing a Pictet "UK Tax Report"
that straddles the arrival year, include only the entries whose deemed-income
date is on/after arrival. (Applied when back-filling FY23-24 ERI: the 30 Jun 2023
entries were excluded, the 30 Sep 2023 / 31 Mar 2024 entries kept — arrival
2023-07-14.) Equalisation, a return of capital, is netted off the uplift
regardless of residence.

## Licence hygiene

This project is MIT, and every runtime dependency is MIT / BSD / Apache-2.0
except `python-stdnum` (LGPL-2.1+, fine for the dynamic linking we do). The
README's "Libraries and licenses" table is the authoritative per-dependency
list; this is the reasoning behind the two notable *exclusions*.

**Why not PyMuPDF?** `PyMuPDF` (`pymupdf`) is the most popular MuPDF binding,
but it is **AGPL-3.0** — viral copyleft — unless you buy a commercial licence
from Artifex. For a permissive project that is a non-starter. `pypdfium2`
(Google's PDFium bindings, Apache-2.0 / BSD-3-Clause) is the drop-in
replacement: similar speed, maintained. When PDFium chokes on a file,
`pdfplumber` (MIT, on `pdfminer.six`) is the second backend.

**Why not `import beancount`?** `beancount` itself is **GPL-2.0**. The
pipeline avoids linking against it and emits beancount **plain text**
directly; to validate that output it shells out to the `bean-check` CLI as a
separate process — a normal program invocation, not library linking, so it
doesn't bind this codebase to the GPL. This is why the tax pipeline computes
everything from the JSONL sidecars rather than loading the ledger through
beancount's Python API.

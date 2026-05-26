# Design decisions

The *why* behind the non-obvious choices, gathered so the rationale
outlives the implementation briefs it was first written into. For *how*
the code is laid out see [CLAUDE.md](../CLAUDE.md); for *how to use* the
tool see [README.md](../README.md).

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

## Strict-mode dispatch on an empty template result

When a registered template returns `[]`, `HybridExtractor` does **not**
fall through to the regex/LLM path. It logs a WARN and returns `[]`
(or raises under `--strict`). This is deliberate: falling through
historically papered over template regressions with
`Equity:Uncategorized`-balanced placeholder entries that landed silently
in the ledger. Surfacing the empty result instead means the next
`bean-check` notices the imbalance — a loud failure beats a silent wrong
number. Doctypes that legitimately emit nothing are listed in
`NO_OUTPUT_DOCTYPES` and short-circuit cleanly.

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

## Licence hygiene

Output is plain beancount text; the pipeline never `import`s `beancount`
(GPL-2.0) and validation shells out to the `bean-check` binary. No
AGPL/GPL runtime dependency is added (notably **not** PyMuPDF). The
README's "Libraries and licenses" section is the authoritative list.

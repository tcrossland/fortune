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

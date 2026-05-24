# Plan: statement-balance reconciliation

## Goal

A `banking-pipeline reconcile` command that compares the
**statement-asserted** balances (what Pictet says each account/holding
held at month-end) against the **ledger-computed** balances (what the
ingested transactions actually sum to), and reports the full grid of
agreements, drifts, and coverage gaps.

This is the highest-leverage correctness check for a "trust the
output" archive: the moment a document is missed or misclassified, a
month-end balance silently diverges, and today the only signal is a
`bean-check` failure that aborts on the first bad assertion.

## What this adds over `bean-check`

`data/balances.beancount` already emits `balance` directives and
`bean-check` enforces them. `reconcile` is additive, not a
replacement:

1. **Full picture, not first-failure.** Reports every account/date
   pair with expected, actual, and signed difference — not just the
   assertions that tripped.
2. **Drift magnitude and direction.** "Account X is short 1,200 GBP at
   2025-07-01" is actionable; an opaque assertion error is not.
3. **Earliest-drift identification.** Pinpoints the *first* period an
   account diverged, which localises the missed/misclassified document
   to one statement month.
4. **Coverage gaps.** Flags months with **no** statement ingested at
   all — something `bean-check` structurally cannot do, since a
   missing assertion is simply a missing checkpoint, not an error.

## Status: implemented

Built and tested. **The engine changed during implementation** — see
the note below. Modules: `reconcile.py`, a `reconcile` CLI command, a
`reconciliation_dir` config knob, and `tests/test_reconcile.py` (14
tests, all passing; full suite green at 547).

## Engine choice — *revised*

The plan assumed a **`bean-query`** binary alongside `bean-check`. It
isn't there: this project runs **beancount v3.2.2**, which split
`bean-query` out into a separate `beanquery` package that isn't
installed (the venv ships only `bean-check`, `bean-doctor`,
`bean-example`, `bean-format`). Rather than add a new GPL-family
dependency, reconcile is built **on top of `bean-check`**, which is
already wrapped in [bean_check.py](../src/banking_pipeline/bean_check.py)
and already evaluates every assertion in one pass:

> `bean-check` prints one
> `Balance failed for '<account>': expected X != accumulated Y` line per
> drifted assertion, citing the directive's `<file>:<line>`. reconcile
> runs `bean-check` once, parses those lines, and matches each failure
> back to its assertion **by line number** (with an account cross-check).

This is strictly better on two fronts: no new dependency, and the
drift verdict is `bean-check`'s own — so reconcile **agrees with a load
by construction**, with beancount's inferred-from-decimals tolerance
honoured without re-implementing it. We still never `import beancount`.

The **expected** side is parsed from the generated
`data/balances.beancount` — its line shape is our own
(`<date> balance <account> <qty> <commodity>`), so a small regex
parser is safe and keeps a single source of truth.

## Tolerance — *delegated*

No longer computed in reconcile. Because an assertion is "drift" iff
`bean-check` flagged it, beancount's own tolerance is the verdict. (The
originally-planned `tolerance_for` half-the-smallest-unit replication
was dropped — it would only have risked disagreeing with the real
load.)

## Data flow

```
data/balances.beancount ──► parse_assertions ──► [Assertion(…, line)]
                                            │
ledger ──► bean_check ──► parse_bean_check_failures ──► {line: Failure}
                                            │
                                            ▼
                         reconcile() — DRIFT iff line flagged
                                            │
              ┌─────────────────────────────┼─────────────────────────┐
              ▼                             ▼                          ▼
      drift report grid          earliest-drift per account    coverage gaps
              │                                                        │
              ▼                                                        ▼
  reports/reconciliation/<...>          summary.txt          exit code (0 / 1)
```

`bean-check` returns 0 even when assertions fail (it's not `--strict`),
so reconcile parses its output regardless of return code; only a
*missing* `bean-check` binary is fatal (can't reconcile without it).

### Coverage-gap detection

Per portfolio segment, collect the set of asserted month-ends.
Statements arrive monthly, so any missing month between the first and
last asserted date is a likely missed statement — flag it. (Cash-only
months with a zero position still produce an assertion, so a true gap
means no statement was ingested.)

## Files changed (as built)

| File | Change |
|------|--------|
| `src/banking_pipeline/reconcile.py` | **New.** Pure logic: `parse_assertions(text)` (records line numbers), `parse_bean_check_failures(text, balances_name)`, `reconcile(expected, failures) -> list[ReconRow]`, `find_coverage_gaps`, `earliest_drift`, `build_report`, `render_summary` / `render_csv`. Failures injected as text, so the core is testable without the binary. |
| `src/banking_pipeline/cli.py` | **New `reconcile` command.** `ledger` (default `main.beancount`), `--balances` (default `data/balances.beancount`), `--output` (default `reconciliation_dir`), `--strict`, `--verbose`. Orchestrates: parse assertions → run `bean_check` once → parse failures → `build_report` → write report → exit nonzero on drift. |
| `src/banking_pipeline/config.py` | Added `reconciliation_dir: Path = Path("reports/reconciliation")` next to `tax_reports_dir`. |
| `tests/test_reconcile.py` | **New.** Unit tests for parsing/diff/coverage/rendering + two `bean-check`-guarded CLI integration tests. |
| `CLAUDE.md` / `README.md` | **TODO** — document the command under the CLI surface; note it's additive to `bean-check`. |

> `bean_query.py` from the original plan was **not** built (no
> `bean-query` binary on beancount v3).

## Output shape

`reports/reconciliation/summary.txt` (human) + `drift.csv` (machine):

```
Reconciliation — main.beancount vs data/balances.beancount

DRIFT (2 of 184 assertions outside tolerance)
  date        account                          expected      actual      diff
  2025-07-01  Assets:Pic:K123456001:GBP        57909.10    56709.10  -1200.00
  2025-07-01  Assets:Pic:K123456001:LU2601...  2248.13866   0.00000   -2248...

EARLIEST DRIFT
  Assets:Pic:K123456001:GBP first diverged 2025-07-01 → check Jul 2025 docs

COVERAGE GAPS
  K123456001: no statement for 2025-03 (asserted 2025-01,02,04,05,...)

OK: 182 assertions within tolerance
```

Exit `0` when every assertion is within tolerance and no gaps;
nonzero otherwise (CI-friendly, same convention as `check`).
`--strict` could later escalate coverage gaps to failures while plain
runs treat them as warnings — start with: drift = fail, gaps =
warning.

## Testing (as built)

Pure logic is tested without the binary by feeding canned `bean-check`
text; two CLI integration tests build a temp ledger and are
`@pytest.mark.skipif`-guarded on `shutil.which("bean-check")`.

- **Unit (no binary):** `parse_assertions` (line numbers, comment
  skipping); `parse_bean_check_failures` (the v3 failure-line format,
  basename filter, non-failure-line rejection); `reconcile` (OK + drift
  + account-mismatch guard); `find_coverage_gaps` (gap, contiguous,
  year boundary); `earliest_drift`; CSV / report rendering.
- **Integration (guarded):** a temp ledger + `balances.beancount`
  asserting a drifted run exits 1 and writes the report, and a clean
  run exits 0.

## Open questions / follow-ups

1. **Doc the command** in `CLAUDE.md` and `README.md` — done (CLI
   surface + layout in CLAUDE.md, a "Reconciliation" subsection under
   Validation in README.md).
2. **`beancount` in runtime deps.** `pyproject.toml` lists
   `beancount>=3.2.2` under `dependencies` (no licence comment, unlike
   every other dep) — this contradicts CLAUDE.md's "never import
   beancount" rule and the bean_check shell-out rationale. Worth
   resolving independently of this feature.
3. **Rebuild integration.** Done — `[post.reconcile]` (off by default)
   runs *before* `check` (bean-check exits nonzero on drift, so going
   first guarantees the report is produced). Drift fails the rebuild;
   coverage gaps fail under `strict` / `rebuild --strict`.
4. **Multiple portfolios.** Currently reconciles every asserted
   account in one run (no `--portfolio` filter). Add one if needed.

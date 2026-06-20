# Reclass the Revolut (self-to-self) contra-leg as an Equity transfer

## Problem

Every `Assets:Revolut:<ccy>` posting in the ledger is the *contra-leg* of a
Pictet self-to-self payment (Pictet↔Revolut). The Pictet side is faithful,
but Revolut's own day-to-day activity is never imported, so the account is
not a cash balance — it is "net moved between Pictet and Revolut". It stands
at large phantom positives (~£336k GBP, ~€252k EUR; small −USD). The
balance sheet sums all `Assets:`/`Liabilities:` postings
(`balance_sheet.py:59-63`), so it counts these at face value and overstates
net worth by roughly that amount.

## Decision

Reclass the self-to-self counter-leg from an `Assets:` root to
`Equity:Transfers:<bank>:<ccy>`. A self-to-self transfer to an account this
ledger does not otherwise track is a *perimeter crossing*, not a holding —
booking it to Equity is the accounting-correct treatment and makes the
balance sheet (which queries Assets/Liabilities only) exclude it by
construction. No `balance_sheet.py` change is needed; the fix is upstream in
the rendered ledger.

Money sent Pictet→Revolut now drops tracked net worth (it left the
perimeter); money received Revolut→Pictet raises it (entered from outside).
Both are truthful given the day-to-day is untracked.

### Why this is invariant-safe
- **Not `Equity:Uncategorized`.** `Equity:Transfers:Revolut` is a named
  account; the no-`Uncategorized` invariant is about the elastic placeholder,
  not all Equity.
- **Still one Transaction, both legs.** The "one Transaction carries both
  legs" convention holds — only the rendered counter-account string changes.
- **Tax substrate untouched.** `Transaction.counter_account` keeps the value
  `"Revolut"`; only the writer's rendered path changes. The JSONL sidecars,
  the ISA choke point, and `gross_income − withholding_tax == amount` are all
  unaffected. Reconcile/completeness read the sidecars, not this account
  string.

## Scope of change

Root constant: introduce `SELF_TRANSFER_ROOT = "Equity:Transfers"` (in
`writer/format.py`), used by the builder. `counter_account` stays a bank
*segment* (`"Revolut"`); the builder owns the family.

1. **`writer/builders/payment.py`** — the two self-to-self legs (lines 105,
   159): `Assets:{counter_account}` → `{SELF_TRANSFER_ROOT}:{counter_account}`.
   Update the four-shape docstrings (lines 47, 63-65).
2. **`writer/format.py`** — add the `SELF_TRANSFER_ROOT` constant.
3. **`models.py`** — update the `counter_account` docstrings (~640, ~664):
   `Assets:<segment>:<ccy>` → `Equity:Transfers:<segment>:<ccy>`.
4. **`config.py`** — update the `beneficiary_bank_map` docstring (308-326):
   destination leg routes to `Equity:Transfers:Revolut:<ccy>`. Map value
   stays `"Revolut"`.
5. **`templates/pictet/pago_interna.py`, `templates/pictet/payment.py`** —
   docstring prose only (`Assets:<counter_account>:<ccy>` → new root). The
   asserts key on section presence, not the account string — no logic
   change. (`pago.py` needed no edit — its docstring carries no account
   path.)

### Goldens / tests
6. Regenerate goldens (re-render + review diff): `tests/fixtures/en/pictet/
   payment.beancount`, `payment.2026.beancount`, `tests/fixtures/es/pictet/
   pago_interna.beancount`.
7. **`tests/test_portfolio_split.py`** — the synthetic ledger's counterparty
   leg `Assets:Revolut:GBP` → `Equity:Transfers:Revolut:GBP`; update the
   `open …` assertion (line 125). `_account_key(...) is None` still holds
   (any non-bank-prefixed account returns None).

### Generated ledger
8. `rebuild` regenerates `data/portfolio.beancount` + per-year ledgers; the
   `open Assets:Revolut:*` directives flip to `open Equity:Transfers:Revolut:*`
   automatically (the aggregate is account-agnostic). Not hand-edited.

### Out of scope (flagged)
- The dormant Revolut **CSV importer** (`revolut/account_map.py`,
  `tests/test_revolut_csv.py`) writes `Assets:Revolut:Personal:*` — a
  different path for *real* imported activity, untouched here. Tension to
  note for later: if CSVs are ever imported, the inbound transfer would land
  in Equity:Transfers while real balances sit in Assets:Revolut:Personal;
  reconciling the two is a separate task.

### Docs
9. `docs/design-decisions.md` — record the rationale (perimeter-crossing →
   Equity; balance-sheet contamination). Touch `docs/architecture.md` /
   `CLAUDE.md` only if they assert the old `Assets:Revolut` scheme.

## Verification
- `uv run ruff check .`, `uv run mypy src`, `uv run pytest`
- Goldens diff reviewed (intended account-path change only)
- `uv run banking-pipeline rebuild` → bean-check clean (Equity opens
  balance), no new reconcile drift
- Balance sheet: `Assets:Revolut:*` gone; net worth drops by the phantom sum
  (direction sanity-checked)
- `code-reviewer` subagent, no Critical findings
- `python3 scripts/check_pii.py --all` clean

## Status
Implemented. Account chosen: `Equity:Transfers:Revolut` (direction-neutral —
the flow is bidirectional, so `Drawings` was rejected).

Done:
- `format.py`: `SELF_TRANSFER_ROOT` + `self_transfer_account()` helper.
- `payment.py`: both self-to-self legs route via the helper; docstrings updated.
- Docstring-only: `models.py`, `config.py`, `templates/pictet/{payment,pago_interna}.py`,
  `portfolio_aggregate.py`.
- Goldens regenerated: `en/pictet/payment{,.2026}.beancount`,
  `es/pictet/pago_interna.beancount`. `test_portfolio_split.py` synthetic
  ledger + assertions updated; `test_pictet_pago_interna_golden.py` docstring.
- `docs/design-decisions.md` rationale; `docs/architecture.md` config note.

Verified:
- ruff / mypy / pytest (927) clean.
- `rebuild --strict`: bean-check `(strict) ok`, balance coverage passed.
- Ledger: no `Assets:Revolut` remains; balances moved to
  `Equity:Transfers:Revolut:*` (same magnitudes).
- Balance-sheet dataset: 0 Revolut / 0 Equity refs — phantom excluded by
  construction.
- PII guard clean.

`code-reviewer` run — no Critical/Major (two Minor housekeeping items
fixed). Ready to merge; archive on commit.

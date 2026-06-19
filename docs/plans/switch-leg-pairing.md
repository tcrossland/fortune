# Plan: switch salida/entrada leg pairing

## Goal

Link the two halves of a Pictet *switch* — the `SWITCH_SALIDA` (exit a
fund) and `SWITCH_ENTRADA` (enter another) advices — so both beancount
entries carry the **same** `^<ref>` link and resolve as one logical
operation in `bean-query` / Fava.

Today each leg is its own PDF → its own `Transaction` → its own entry,
and the writer links each leg to *its own* `transaction_number`. So the
Oct-2022 rotation renders as `^812960461` (salida) and `^812960462`
(entrada) — two unrelated links for one economic event. After this
change both legs render `^812960461` (the salida's number, the canonical
anchor), with each leg's own number preserved in its `no:` metadata.

## Background

This is the "future pairing layer" already anticipated in the code:

- `Transaction.link_id` exists for exactly this purpose
  ([models.py:698](../../src/banking_pipeline/models.py)) and is `None`
  today.
- The switch builder already consumes it:
  `link = tx.link_id or tx.transaction_number`
  ([switch_trade.py:115](../../src/banking_pipeline/writer/builders/switch_trade.py)).
  **The writer needs no change** — set `link_id` on the entrada
  `Transaction` and the shared link renders for free.
- The trailer comment already documents the intended end-state:
  *"link = salida's txn, no: = entrada's own txn"*
  ([switch_trade.py:222](../../src/banking_pipeline/writer/builders/switch_trade.py)).
- The JSONL sidecar already serializes `link_id`, so setting it
  propagates to both the rendered ledger and the structured substrate
  with no schema change.

So the entire feature is: **populate `link_id` on entrada legs before
render.** Everything downstream is already wired.

Scope note: `SWITCH_TYPES = {SWITCH_SALIDA, SWITCH_ENTRADA}`
([switch_trade.py:27](../../src/banking_pipeline/writer/builders/switch_trade.py)).
The forward-FX `CAMBIO_DE_DIVISAS_*` advices route to a different
builder and have a different (open-then-settle, not same-day-netting)
relationship — out of scope here (see Non-goals).

## The pairing signal: the `Switch:<ccy>` clearing account

A switch has **no external cash effect** — the proceeds and the cost
both flow through an intermediate clearing leg
`Assets:<prefix>:<portfolio>:Switch:<ccy>`
([switch_trade.py:209](../../src/banking_pipeline/writer/builders/switch_trade.py)):

- **salida** posts proceeds **in** → `tx.amount > 0`
- **entrada** draws cost **out** → `tx.amount < 0`

…in the **same clearing currency** (`tx.currency`), on the **same
booking date**, **equal and opposite to the cent**. The clearing account
therefore nets to ~zero per currency over all history (verified: the
residuals are accumulated cent-rounding, ≤ a couple of units). This is
the deterministic-enough signal a matcher reconciles — no guessing.

Critically this holds for **FX switches** too: the Dec-2022 CHF→AXA pair
both clear in **CHF** (`+80264.89` / `-80264.88`) even though the
entrada *buys* in EUR. Match on the clearing leg (`tx.currency` +
`tx.amount`), never on the underlying `security_currency`.

## Design

### 1. A pure matcher — `switch_pairing.py`

New module exposing a single pure function over a list of transactions:

```python
def pair_switches(txns: list[Transaction]) -> list[PairingResult]
```

- **Input**: any transaction list (mixed doctypes); it filters to
  `SWITCH_TYPES` itself.
- **Output**: assignments (`entrada transaction_number → link_id`) plus
  a list of **unpaired** legs for the caller to warn on. It does not
  mutate; the caller applies `link_id`. Pure + side-effect-free so it's
  unit-testable and reusable by either placement (below).

Algorithm:

1. **Bucket** switch legs by `(account_number, currency, booking_date)`
   — the clearing-account identity plus the day.
2. Within a bucket, split into `salida` (`amount > 0`) and `entrada`
   (`amount < 0`) legs.
3. **Match to a zero-netting set.** The common case is 1:1
   (`|salida| == |entrada|` within tolerance). Generalise to subset-sum
   so **1:many / many:1** splits (one sell funding two buys) also close.
   Tolerance: `±0.01 × leg-count` (the observed cent rounding).
4. For each closed group, set every member's `link_id` to the
   **salida's** `transaction_number` (lowest, if several salidas).
5. A group is only accepted if it contains **≥1 salida and ≥1 entrada**
   and nets to ~0. Anything that can't be closed is reported **unpaired**
   and keeps its `transaction_number` fallback link (today's behaviour) —
   never mis-pair to force closure.

Tie-breakers when a bucket holds several candidates:

- exact amount first;
- **shared order date** (`Fecha de la orden`) — both legs of one switch
  carry the same order date (see Investigation below); a strong
  corroborator, though not currently captured in the model;
- prefer salida.ISIN ≠ entrada.ISIN (a switch changes holding);
- transaction-number proximity as a *weak* last resort only
  (`812960461→462` are adjacent, but `826450556→560` are 4 apart, so it
  cannot be primary);
- still ambiguous → leave unpaired + warn, rather than guess.

Determinism: sort inputs canonically so output is order-independent.

### 2. Placement — collect-then-render in `ingest`

`ingest` currently renders **per document** inside the PDF loop
([ingest.py:101-108](../../src/banking_pipeline/cli/ingest.py)): it
appends `beancount_writer.render(result)` before the next PDF is seen,
so `link_id` can't yet be known. Restructure into two phases:

1. **Collect** every `ExtractionResult` for the batch (already
   accumulates `all_txns`).
2. Run `pair_switches(all_txns)` and apply the returned `link_id`s to
   the in-memory `Transaction` objects.
3. **Render** all results, and write the sidecar, from the now-paired
   transactions.

This sets the link at its canonical source (the `Transaction`), so the
ledger `^link` and the sidecar `link_id` agree by construction. Because
the empirical data shows both legs share a **booking date**, they land
in the same ingest batch (one `[[sources]]` entry ≈ one year), so
in-batch pairing covers the real cases.

`rebuild` inherits this automatically (it drives `ingest` per source).

### 3. Reuse, not a second mechanism

Keep the matcher pure so a future **full-history** pass (cross-source /
settlement-lag pairs that straddle a year boundary) can call the same
`pair_switches` over all sidecars — analogous to how
`portfolio_aggregate` reads the whole history. Not built now; the
design just doesn't preclude it. (See Open questions.)

## Edge cases

- **Orphan leg** (counterpart PDF missing, or it crossed the
  source boundary): unpaired, keeps `^<own-number>`, surfaced as a
  warning. Under `ingest --strict` / `rebuild --strict`, escalate an
  in-batch orphan (a salida whose entrada is in the same batch but
  didn't net) to a hard error — a genuine extraction bug. A lone leg
  with no same-batch counterpart is a warning, not an error (it may pair
  in a full-history pass later).
- **Two independent same-day, same-currency, same-amount switches**:
  disambiguate by ISIN pairing; if irreducible, leave unpaired + warn.
- **Cent rounding**: tolerance is per-leg, so a 1:1 pair tolerates
  ±0.01 and a 3-leg split ±0.03.
- **Re-runs**: pairing is idempotent — `link_id` is recomputed from the
  batch each run, never read back from prior output.

## Writer / data model impact

- **Writer**: none. `switch_trade.render` already prefers `link_id`.
- **Model**: none. `link_id` already exists.
- **Sidecar**: none. `link_id` already serialized (the `…/v3` schema
  already carries it as `null`).

The change is concentrated in the new `switch_pairing.py` and the
`ingest` collect/render restructure.

## Testing

- **Matcher unit tests** (`tests/test_switch_pairing.py`, pure
  function): 1:1 same-currency; FX (CHF-clearing) pair; 1:many split;
  cent-rounding within tolerance; orphan → unpaired; two ambiguous
  same-day/ccy/amount switches → unpaired (no mis-pair); determinism
  under input reordering.
- **End-to-end render test**: ingest a salida+entrada fixture *pair*
  together and assert both entries render the **salida's** `^link` and
  each keeps its own `no:`. (The existing single-document switch goldens
  render one leg in isolation, so they fall back to `transaction_number`
  and stay **byte-stable** — no golden churn.)
- **Strict-mode test**: an in-batch salida whose entrada is present but
  doesn't net fails `--strict`; a lone leg only warns.

## CLI / config surface

- No new command. Pairing is an internal `ingest` step, on by default.
- Optional `[settings]`/flag `pair_switches = true` kill-switch if a
  user wants the old per-leg links — low priority; default on.
- Unpaired legs reported via the existing warning channel; gated to a
  hard failure only under `--strict` (in-batch orphans).

## Non-goals

- **`CAMBIO_DE_DIVISAS_*` forward-FX pairing.** Different doctypes,
  different builder, and an open→settle relationship over time rather
  than same-day netting. A separate plan if wanted.
- **Cross-source / settlement-lag pairing** (legs in different
  per-year files). The matcher is built to allow it later; the ingest
  placement does not deliver it now.
- **Changing the non-switch builders** to emit links — explicitly
  declined; the `no:` metadata remains the reference for those.

## Investigation: is there a deterministic order reference? (settled — no)

Checked both legs of two real pairs — `812960461`/`812960462`
(2022-10-12) and `826450556`/`826450560` (2022-12-02). Findings:

- **No shared order/operation number.** The advices print only
  `N° de transacción` (per-leg) and `N° de cuenta` (the account). There
  is **no** `N° de orden` / `N° de operación` field. The only ≥6-char
  token both legs share is the account number itself.
- **Order date *is* shared; order time is not.** Both legs carry the
  same `Fecha de la orden` date (e.g. `06.10.2022`, `28.11.2022`) but
  different times (~45s apart — the legs are placed as two separate
  orders moments apart). So order date corroborates a pair but can't
  uniquely key one (multiple switches could be ordered the same day).
- **Booking date is the shared date, not trade date.** The legs'
  `Fecha de transacción` (trade/settlement) **differ** (07.10 vs 11.10;
  29.11 vs 30.11 — settlement lag), while `Fecha de publicación`
  (booking) is identical (12.10; 02.12). This **confirms the matcher
  must bucket by `booking_date`**, which is exactly the ledger
  `entry_date` — not by trade date.

**Conclusion:** amount-netting on the `Switch:<ccy>` clearing account is
the correct primary key — there is no exact reference to lean on.
Optionally capture `Fecha de la orden` into the model as a new field to
use as the corroborating tie-breaker above; cheap, and it tightens
disambiguation when several same-day switches collide. Not required for
the common (1:1) case.

## Open questions

1. **Full-history pass now or later?** Same-day batching covers observed
   data; defer the cross-source pass until a real straddling pair
   appears (the warning will flag it).

## Status: planned

Not yet implemented. Single-document goldens are expected to stay
byte-stable; the only new output appears when both legs are ingested in
one batch.

"""Pair the two legs of a Pictet fund switch onto one shared link.

A *switch* rotates a holding: Pictet issues a ``SWITCH_SALIDA`` advice
(sell the old fund) and a ``SWITCH_ENTRADA`` advice (buy the new one),
each as its own PDF → its own :class:`~banking_pipeline.models.Transaction`.
On their own each leg renders with its *own* ``transaction_number`` as the
beancount ``^<link>``, so one economic event shows up as two unrelated
links. This module reconciles a batch of transactions and decides which
legs belong to the same switch, so the caller can stamp both with the
**salida's** number as a shared ``link_id`` — after which the writer
renders one link for the pair (``switch_trade.render`` already prefers
``link_id`` over ``transaction_number``).

The matcher is **pure**: it reads transactions and returns assignments +
diagnostics; it never mutates. The caller applies the ``link_id``s. That
keeps it unit-testable and lets a future full-history pass (cross-source
legs that straddle a year boundary) reuse it over all sidecars.

Pairing key
-----------
Both legs of one switch share four facts: the **portfolio account**, the
**clearing currency** (``Assets:<prefix>:<portfolio>:Switch:<ccy>`` — the
intermediate leg the proceeds flow through), the **booking date** (Pictet's
``Fecha contable`` / the ledger ``entry_date`` — *not* the trade date,
which differs by the settlement lag), and the **order date**
(``Fecha de la orden``). Order date is the load-bearing corroborator: an
FX switch's two clearing amounts do **not** net to the cent (the entrada's
clearing amount is an independent FX conversion of the underlying buy), so
amount-netting alone can't confirm an FX pair — but both legs always carry
the same order date. Amount-netting is kept only as a conservative fallback
for legs that carry no order date.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.builders.switch_trade import SWITCH_TYPES

# Per-leg cent-rounding slack for the amount-netting fallback: a candidate
# group of N legs may net within ``±0.01 × N``. Only ever applied to legs
# that carry no order date — the order-date path needs no amount gate.
_CENT_TOLERANCE = Decimal("0.01")

# Buckets larger than this are not searched by the netting fallback (it
# would be combinatorial). Such legs are left unpaired and surfaced as a
# warning rather than silently dropped — see ``pair_switches``.
_MAX_NETTING_LEGS = 12


@dataclass(frozen=True)
class SwitchPairing:
    """The outcome of :func:`pair_switches` over one batch.

    ``assignments`` maps each paired leg's ``transaction_number`` to the
    shared ``link_id`` (the salida's number). It includes the salida
    mapped to itself, so applying it makes the shared anchor explicit on
    both legs (and in both sidecar rows). ``unpaired`` lists switch legs
    that couldn't be closed into a pair, for the caller to warn on.
    ``in_batch_orphans`` is the subset of ``unpaired`` that has an
    opposite-side counterpart sharing the *strongest* key (same bucket +
    order date, or same bucket with neither carrying an order date) yet
    still didn't pair — a likely extraction bug the caller escalates to a
    hard error under ``--strict``.
    """

    assignments: dict[str, str]
    unpaired: list[Transaction] = field(default_factory=list)
    in_batch_orphans: list[Transaction] = field(default_factory=list)


def pair_switches(txns: list[Transaction]) -> SwitchPairing:
    """Reconcile switch legs in ``txns`` into shared-link assignments.

    Filters ``txns`` to ``SWITCH_TYPES`` itself, so callers can pass a
    mixed-doctype batch. Deterministic: output is independent of input
    order. Idempotent: it never reads ``link_id`` back, so re-running over
    the same batch yields the same assignments.
    """

    legs = [
        tx
        for tx in txns
        if tx.document_type in SWITCH_TYPES and tx.transaction_number
    ]

    buckets: dict[tuple[str | None, str, date], list[Transaction]] = defaultdict(
        list
    )
    for tx in legs:
        buckets[(tx.account_number, tx.currency, _entry_date(tx))].append(tx)

    assignments: dict[str, str] = {}
    unpaired: list[Transaction] = []
    in_batch_orphans: list[Transaction] = []

    for key in sorted(buckets, key=lambda k: (k[0] or "", k[1], k[2])):
        groups, leftovers = _match_bucket(buckets[key])
        for group in groups:
            # Every group carries ≥1 salida (Phase 1 requires both sides;
            # Phase 2 emits [salida, entrada] pairs), and all legs were
            # filtered to a non-null transaction_number above.
            salida_numbers = [
                g.transaction_number
                for g in group
                if _is_salida(g) and g.transaction_number is not None
            ]
            anchor = min(salida_numbers, key=_txn_sort_key)
            for g in group:
                if g.transaction_number is not None:
                    assignments[g.transaction_number] = anchor
        unpaired.extend(leftovers)
        in_batch_orphans.extend(_in_batch_orphans(leftovers))

    unpaired.sort(key=lambda t: _txn_sort_key(t.transaction_number))
    in_batch_orphans.sort(key=lambda t: _txn_sort_key(t.transaction_number))
    return SwitchPairing(
        assignments=assignments,
        unpaired=unpaired,
        in_batch_orphans=in_batch_orphans,
    )


def _match_bucket(
    legs: list[Transaction],
) -> tuple[list[list[Transaction]], list[Transaction]]:
    """Match one ``(account, currency, entry_date)`` bucket.

    Phase 1 — **order date** (primary). Sub-bucket the legs by order date.
    An order-date group with at least one salida and one entrada is one
    switch (the four shared facts are overwhelming), so pair the whole
    group: 1:1 and the 1:many / many:1 split (one sell funding two buys,
    all ordered the same day) both close here with no amount gate. The
    lone exception is a group with **≥2 salidas and ≥2 entradas** — two
    distinct switches ordered the same day, interleaved — which is
    ambiguous and deferred to Phase 2 to split by amount.

    Phase 2 — **amount netting** (fallback). For legs with no order date,
    plus the deferred ambiguous groups, greedily match opposite-side legs
    whose clearing amounts net to ~zero (cent tolerance). Conservative on
    purpose: it pairs only true cent-netting 1:1 matches, never guesses an
    FX pair (whose legs don't net) — those stay unpaired for Phase 1's
    order date to catch, or for a human to review.
    """

    groups: list[list[Transaction]] = []
    deferred: list[Transaction] = []

    by_order: dict[date, list[Transaction]] = defaultdict(list)
    for tx in legs:
        if tx.order_date is not None:
            by_order[tx.order_date].append(tx)
        else:
            deferred.append(tx)

    for od in sorted(by_order):
        grp = by_order[od]
        salidas = [t for t in grp if _is_salida(t)]
        entradas = [t for t in grp if not _is_salida(t)]
        if salidas and entradas and not (len(salidas) >= 2 and len(entradas) >= 2):
            groups.append(grp)
        else:
            # Single-sided (counterpart not in this batch) or the
            # ambiguous ≥2/≥2 case — let amount-netting try.
            deferred.extend(grp)

    netted, leftovers = _net_match(deferred)
    groups.extend(netted)
    return groups, leftovers


def _net_match(
    legs: list[Transaction],
) -> tuple[list[list[Transaction]], list[Transaction]]:
    """Conservative cent-netting 1:1 match over ``legs``.

    Returns closed ``[salida, entrada]`` pairs and the unmatched
    remainder. Each salida pairs with the still-free entrada whose amount
    most nearly cancels it (within ``±0.01 × 2``) **only when that choice
    is unambiguous**: if two entradas tie on the closest net, the sole
    tie-break is a different ISIN (a switch changes the holding), and if
    that still leaves more than one candidate the salida is left unpaired
    rather than guessed — the plan's "two ambiguous switches → leave
    unpaired". Buckets above ``_MAX_NETTING_LEGS`` are left wholly
    unmatched (combinatorial); the caller surfaces them as a warning.
    """

    salidas = sorted([t for t in legs if _is_salida(t)], key=_leg_sort_key)
    entradas = sorted([t for t in legs if not _is_salida(t)], key=_leg_sort_key)

    if len(legs) > _MAX_NETTING_LEGS:
        return [], list(legs)

    groups: list[list[Transaction]] = []
    used: set[int] = set()
    pair_tolerance = _CENT_TOLERANCE * 2
    for s in salidas:
        candidates = [
            e
            for e in entradas
            if id(e) not in used and abs(s.amount + e.amount) <= pair_tolerance
        ]
        if not candidates:
            continue
        best_net = min(abs(s.amount + e.amount) for e in candidates)
        top = [e for e in candidates if abs(s.amount + e.amount) == best_net]
        if len(top) > 1:
            different_isin = [e for e in top if e.isin != s.isin]
            top = different_isin
        if len(top) != 1:
            continue  # ambiguous — don't guess
        groups.append([s, top[0]])
        used.add(id(s))
        used.add(id(top[0]))

    leftovers = [t for t in legs if id(t) not in used]
    return groups, leftovers


def _in_batch_orphans(leftovers: list[Transaction]) -> list[Transaction]:
    """Leftovers that should have paired but didn't — a likely bug.

    Within one bucket's leftovers, group by order date (``None`` is its
    own group). A group holding *both* a salida and an entrada whose
    aggregate **doesn't** net to zero (within ``±0.01 × N``) shares the
    strongest pairing key the matcher has yet failed to close, with
    amounts that don't add up — a genuine extraction error: flag every
    member. A single-sided group is a lone leg (its counterpart is in
    another batch / year) and a both-sided group that *does* net to zero
    is a benign ambiguity (two indistinguishable switches); neither is an
    orphan — both are warning-only.
    """

    by_order: dict[date | None, list[Transaction]] = defaultdict(list)
    for tx in leftovers:
        by_order[tx.order_date].append(tx)

    orphans: list[Transaction] = []
    for grp in by_order.values():
        has_salida = any(_is_salida(t) for t in grp)
        has_entrada = any(not _is_salida(t) for t in grp)
        if not (has_salida and has_entrada):
            continue
        net = sum((t.amount for t in grp), Decimal(0))
        if abs(net) > _CENT_TOLERANCE * len(grp):
            orphans.extend(grp)
    return orphans


def _entry_date(tx: Transaction) -> date:
    """The ledger entry date — booking date when present, else trade date.

    Mirrors the writer's ``entry_date = tx.booking_date or tx.trade_date``
    so the matcher buckets on the same date the legs render under. Switch
    advices reliably print ``Fecha contable``, so both legs bucket on their
    (shared) booking date; the trade-date fallback only bites if extraction
    missed the booking date on a leg, which would silently split it from
    its sibling rather than flag an orphan (orphan detection is per-bucket).
    """

    return tx.booking_date or tx.trade_date


def _is_salida(tx: Transaction) -> bool:
    """True for the outgoing (sale-proceeds) leg.

    Keyed off the classified doctype, not the amount sign, so the matcher
    agrees with the writer (``switch_trade.render`` branches on
    ``doc_type == SWITCH_SALIDA``). For well-formed advices the two
    coincide — salida posts proceeds *in* (``amount > 0``), entrada draws
    cost *out* (``amount < 0``) — but a mis-signed advice should still be
    grouped by what it *is*, and the netting math reads the raw amounts
    regardless. ``txns`` were filtered to ``SWITCH_TYPES``, so a non-salida
    leg is necessarily an entrada.
    """

    return tx.document_type == DocumentType.SWITCH_SALIDA


def _txn_sort_key(number: str | None) -> tuple[int, int | str]:
    """Order transaction numbers numerically when they're all digits.

    Pictet numbers are ``\\d+``; sort them as integers so ``"812960461"``
    precedes ``"812960462"``. Fall back to string order for anything
    non-numeric (defensive — not seen in practice). ``None`` sorts last.
    """

    if number is None:
        return (2, "")
    if number.isdigit():
        return (0, int(number))
    return (1, number)


def _leg_sort_key(tx: Transaction) -> tuple[int, int | str]:
    return _txn_sort_key(tx.transaction_number)

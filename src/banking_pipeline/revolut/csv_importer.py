"""Parse Revolut Personal CSVs and pair EXCHANGE legs across files.

The CSV is exported per-pocket. An exchange between, say, GBP and EUR shows
up as two rows — one in each pocket's CSV — that share a ``Started Date``.
This module reads all the CSVs you pass in, then pairs those rows so the
emitted beancount transaction has both legs and balances cleanly.

Filtering rules:

* Rows with ``State != COMPLETED`` are dropped (REVERTED, FAILED, DECLINED,
  PENDING). Beancount entries should reflect settled cash movement only.
* Unmatched EXCHANGE legs still emit a transaction, but flagged ``!`` and
  with the unknown side posted to a placeholder account. Better to surface
  them than silently drop.
"""

from __future__ import annotations

import csv
import logging
import re
from collections.abc import Iterable, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from banking_pipeline.revolut import account_map
from banking_pipeline.revolut.models import Posting, RevolutRow, RevolutTxn

logger = logging.getLogger(__name__)

# Canonical column names. Revolut's headers have varied historically; we
# normalise on read so the rest of the importer can use stable names.
_COLUMN_ALIASES: dict[str, str] = {
    "type": "type",
    "product": "product",
    "started date": "started_date",
    "completed date": "completed_date",
    "description": "description",
    "amount": "amount",
    "fee": "fee",
    "currency": "currency",
    "state": "state",
    "balance": "balance",
}

# Description pattern for the source leg of an exchange: "Exchanged to USD".
# The destination leg matches "Exchanged to <SOURCE_CCY>".
_EXCHANGE_RE = re.compile(r"Exchanged to (?P<ccy>[A-Z]{3})\b")


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def parse_csv(path: Path) -> list[RevolutRow]:
    """Parse a single Revolut Personal CSV file into :class:`RevolutRow` objects.

    Strips and case-normalises headers so minor schema drift (capitalisation,
    extra whitespace) doesn't break ingestion.
    """

    rows: list[RevolutRow] = []
    with path.open(encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: empty CSV (no header row)")
        normalised = [_COLUMN_ALIASES.get(name.strip().lower()) for name in reader.fieldnames]
        missing = {"type", "started_date", "amount", "currency", "state"} - {
            n for n in normalised if n
        }
        if missing:
            raise ValueError(
                f"{path}: missing required columns {sorted(missing)}; "
                f"got {reader.fieldnames!r}"
            )
        for raw in reader:
            mapped: dict[str, str] = {}
            for i, name in enumerate(reader.fieldnames):
                col = normalised[i]
                if col is not None:
                    mapped[col] = raw[name]
            try:
                rows.append(_row_from_dict(mapped, source=path.name))
            except (ValueError, InvalidOperation) as exc:
                logger.warning("%s: skipping malformed row %r: %s", path, raw, exc)
    return rows


def _row_from_dict(d: dict[str, str], *, source: str) -> RevolutRow:
    """Build a :class:`RevolutRow` from a mapping of normalised column → str.

    Tolerant of empty ``Fee`` and ``Balance`` values, which Revolut
    occasionally omits on non-cash rows (e.g. internal corrections).
    """

    return RevolutRow(
        type=d["type"].strip(),
        product=d.get("product", "").strip(),
        started_date=_parse_dt(d["started_date"]),
        completed_date=_parse_dt_optional(d.get("completed_date", "")),
        description=d.get("description", "").strip(),
        amount=_parse_decimal(d["amount"]),
        fee=_parse_decimal(d.get("fee", "0") or "0"),
        currency=d["currency"].strip().upper(),
        state=d["state"].strip().upper(),
        balance=_parse_decimal_optional(d.get("balance", "")),
        source_file=source,
    )


def _parse_dt(value: str) -> datetime:
    return datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")


def _parse_dt_optional(value: str) -> datetime | None:
    value = value.strip()
    return _parse_dt(value) if value else None


def _parse_decimal(value: str) -> Decimal:
    return Decimal(value.strip())


def _parse_decimal_optional(value: str) -> Decimal | None:
    value = value.strip()
    return Decimal(value) if value else None


# --------------------------------------------------------------------------
# Top-level: parse all + pair exchanges + convert to RevolutTxn
# --------------------------------------------------------------------------


def import_csvs(paths: Sequence[Path]) -> list[RevolutTxn]:
    """Parse every CSV in ``paths`` and return a chronologically-sorted list
    of :class:`RevolutTxn`.

    EXCHANGE rows that share a ``Started Date`` and form a matching pair
    (one row in each currency) are collapsed into a single two-posting
    transaction. Unpaired EXCHANGE rows are emitted flagged.
    """

    all_rows: list[RevolutRow] = []
    for path in paths:
        all_rows.extend(parse_csv(path))

    completed = [r for r in all_rows if r.state == "COMPLETED"]
    exchange_rows = [r for r in completed if r.type == "EXCHANGE"]
    other_rows = [r for r in completed if r.type != "EXCHANGE"]

    txns: list[RevolutTxn] = [_to_simple_txn(r) for r in other_rows]
    txns.extend(_pair_exchanges(exchange_rows))

    # Stable sort: by date, then by source file + amount so output is
    # deterministic across runs even when timestamps tie.
    txns.sort(key=lambda t: (t.txn_date, t.narration, _txn_signed_total(t)))

    _attach_eod_balances(txns, completed)
    return txns


# --------------------------------------------------------------------------
# Simple (non-exchange) row → transaction
# --------------------------------------------------------------------------


def _to_simple_txn(row: RevolutRow) -> RevolutTxn:
    """Convert a single non-EXCHANGE row to a two-posting :class:`RevolutTxn`."""

    asset = account_map.asset_account(row.product, row.currency)
    counter, payee = _counter_for(row)

    # The asset posting carries the gross amount. If a fee was charged,
    # split the counterparty leg so the fee shows under ``Expenses:Fees:Revolut``.
    #
    # Revolut reports ``fee`` as a non-negative number that's already
    # **included** in the signed ``amount`` (e.g. ATM withdrawal of £100
    # with a £0.50 fee comes through as ``amount=-100.50, fee=0.50``).
    # The counterparty value is therefore ``-amount - fee`` regardless of
    # direction: outflows give a positive expense leg, inflows give a
    # negative income leg, and the standalone fee posting is always a
    # positive expense.
    postings: list[Posting] = [Posting(account=asset, amount=row.amount, currency=row.currency)]
    if row.fee != 0:
        postings.append(
            Posting(
                account=counter,
                amount=-row.amount - row.fee,
                currency=row.currency,
            )
        )
        postings.append(
            Posting(
                account=account_map.EXPENSES_FEES,
                amount=row.fee,
                currency=row.currency,
            )
        )
    else:
        postings.append(Posting(account=counter, amount=-row.amount, currency=row.currency))

    return RevolutTxn(
        txn_date=row.started_date.date(),
        payee=payee,
        narration=row.description or row.type.title(),
        postings=tuple(postings),
        metadata={"type": row.type, "product": row.product or "Current"},
    )


def _counter_for(row: RevolutRow) -> tuple[str, str | None]:
    """Pick the placeholder counter-account for a non-exchange row.

    Returns ``(account, payee)`` — payee may be ``None`` for entries where
    Revolut's description isn't a real merchant name (TOPUP, INTEREST etc.).
    """

    t = row.type.upper()
    if t == "FEE":
        return account_map.EXPENSES_FEES, None
    if t == "INTEREST":
        return account_map.INCOME_INTEREST, None
    if t in {"CASHBACK", "REWARD"}:
        return account_map.INCOME_CASHBACK, None
    if t == "TOPUP":
        return account_map.EQUITY_OPENING, None
    if row.amount < 0:
        # Outflow: card payment, transfer out, ATM, etc.
        return account_map.EXPENSES_FIXME, row.description or None
    # Inflow: refund, transfer in.
    return account_map.INCOME_FIXME, row.description or None


# --------------------------------------------------------------------------
# EXCHANGE pairing
# --------------------------------------------------------------------------


def _pair_exchanges(rows: Iterable[RevolutRow]) -> list[RevolutTxn]:
    """Pair source/destination EXCHANGE legs and return one txn per pair.

    Pairing key is ``Started Date``. Within a group, a row whose description
    is ``Exchanged to <X>`` and whose currency is ``<Y>`` matches a row whose
    description is ``Exchanged to <Y>`` and whose currency is ``<X>``.
    Unpaired rows are emitted as flagged single-leg transactions.
    """

    by_ts: dict[datetime, list[RevolutRow]] = {}
    for r in rows:
        by_ts.setdefault(r.started_date, []).append(r)

    txns: list[RevolutTxn] = []
    for ts, group in by_ts.items():
        used: set[int] = set()
        for i, src in enumerate(group):
            if i in used:
                continue
            m = _EXCHANGE_RE.match(src.description)
            if m is None:
                txns.append(_unmatched_exchange(src))
                used.add(i)
                continue
            dest_ccy = m.group("ccy")
            partner_idx = _find_partner(group, used, i, src.currency, dest_ccy)
            if partner_idx is None:
                txns.append(_unmatched_exchange(src))
                used.add(i)
                continue
            dest = group[partner_idx]
            used.update({i, partner_idx})
            # Always orient (source = outflow leg, dest = inflow leg).
            if src.amount > 0:
                src, dest = dest, src
            txns.append(_paired_exchange(src, dest, ts))
    return txns


def _find_partner(
    group: list[RevolutRow],
    used: set[int],
    self_idx: int,
    self_ccy: str,
    dest_ccy: str,
) -> int | None:
    """Return the index of the matching destination row, or ``None``."""

    for j, candidate in enumerate(group):
        if j == self_idx or j in used:
            continue
        if candidate.currency != dest_ccy:
            continue
        m = _EXCHANGE_RE.match(candidate.description)
        if m and m.group("ccy") == self_ccy:
            return j
    return None


def _paired_exchange(src: RevolutRow, dest: RevolutRow, ts: datetime) -> RevolutTxn:
    """Build a balanced exchange transaction from a matched leg pair."""

    src_acct = account_map.asset_account(src.product, src.currency)
    dest_acct = account_map.asset_account(dest.product, dest.currency)
    # Express the rate as a total cost (``@@``) on the source leg: the
    # absolute source amount yielded the absolute destination amount.
    cost = (abs(dest.amount), dest.currency)
    postings = (
        Posting(account=src_acct, amount=src.amount, currency=src.currency, cost=cost),
        Posting(account=dest_acct, amount=dest.amount, currency=dest.currency),
    )
    return RevolutTxn(
        txn_date=ts.date(),
        payee=None,
        narration=f"Exchange {src.currency} → {dest.currency}",
        postings=postings,
        metadata={"type": "EXCHANGE", "product": src.product or "Current"},
    )


def _unmatched_exchange(row: RevolutRow) -> RevolutTxn:
    """Emit a flagged single-leg exchange entry for diagnostic surfacing."""

    asset = account_map.asset_account(row.product, row.currency)
    placeholder = account_map.EXPENSES_FIXME if row.amount < 0 else account_map.INCOME_FIXME
    return RevolutTxn(
        txn_date=row.started_date.date(),
        payee=None,
        narration=row.description or "Exchange (unpaired)",
        postings=(
            Posting(account=asset, amount=row.amount, currency=row.currency),
            Posting(account=placeholder, amount=-row.amount, currency=row.currency),
        ),
        metadata={
            "type": "EXCHANGE",
            "product": row.product or "Current",
            "source_file": row.source_file,
            "warning": "unpaired-exchange-leg",
        },
        flagged=True,
    )


# --------------------------------------------------------------------------
# Balance assertions (one per (account, day) — the last balance wins).
# --------------------------------------------------------------------------


def _attach_eod_balances(txns: list[RevolutTxn], rows: Sequence[RevolutRow]) -> None:
    """Emit ``balance`` assertions on the latest txn per (account, day).

    Beancount balance assertions are checked at the **start** of the asserted
    day, so we attach them with ``date + 1 day`` semantics by storing the
    raw balance and letting the renderer adjust.
    """

    # Pick the last completed row of each (account, calendar day) to source
    # the balance from. Iterate in chronological order so the final assignment
    # wins.
    latest: dict[tuple[str, object], tuple[RevolutRow, str]] = {}
    for r in sorted(rows, key=lambda x: x.started_date):
        if r.balance is None:
            continue
        acct = account_map.asset_account(r.product, r.currency)
        latest[(acct, r.started_date.date())] = (r, r.currency)

    # Keyed lookup: (account, date) → (balance, ccy).
    eod = {key: (row.balance, ccy) for key, (row, ccy) in latest.items() if row.balance is not None}
    # Attach to the last txn touching that (account, date).
    seen: set[tuple[str, object]] = set()
    for t in reversed(txns):
        for p in t.postings:
            key = (p.account, t.txn_date)
            if key in eod and key not in seen:
                bal, ccy = eod[key]
                # Only attach if this txn doesn't already carry a balance for a
                # different account on the same day. Beancount allows multiple
                # balance directives so we collect them and the renderer emits
                # all of them.
                t.metadata.setdefault("_balances", "")
                existing = t.metadata["_balances"]
                t.metadata["_balances"] = (
                    f"{existing}\n{p.account}|{bal}|{ccy}".lstrip("\n")
                )
                seen.add(key)


def _txn_signed_total(t: RevolutTxn) -> Decimal:
    """Sum of the first posting's amount — used purely as a tie-break sort key."""

    if not t.postings:
        return Decimal(0)
    return t.postings[0].amount

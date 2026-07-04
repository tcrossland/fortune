"""Reconcile ingested transactions against the portal Transactions export.

The e-banking "Transactions" CSV lists every trade leg Pictet booked — across
both mandates, every trade type — keyed by ``Order nr.`` (the same id captured
in each sidecar's ``transaction_number``). ``completeness`` checks only the cash
subset (current-account movements); this checks the *whole* transaction feed, so
a securities trade the pipeline failed to ingest — which would corrupt the
section 104 pool and CGT — surfaces here.

ID-keyed (exact ``Order nr.`` match, no date tolerance):

* **MISSING** — an export order absent from the sidecars: a trade booked but not
  ingested.
* **UNMATCHED** — a sidecar ``transaction_number`` absent from the export: a
  phantom / duplicate ingest. Safe only because the export is comprehensive
  (it lists cash events too, not just trades) — see ``_NON_TRANSACTION_DOCTYPES``.
* **AMOUNT_MISMATCH** — a matched single-leg securities order whose export cash
  amount disagrees with the sidecar (a mis-extracted figure).

Forex-forward *open* legs are excluded — we book the forward at settlement, so
the open leg's order number never appears in the sidecars (expected, not a gap).
The export is Windows-1252, ``;``-delimited; its ``Account nr.`` is bare (no
``K-``/``P-`` mandate letter), resolved to the lettered sidecar portfolio by the
caller. An archive-only reconciliation input — never ingested, never fed to tax.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from pathlib import Path

_CSV_ENCODING = "cp1252"
_CSV_DELIMITER = ";"
_AMOUNT_TOLERANCE = Decimal("0.01")

# The one export leg we never ingest: an FX-forward open. We book the forward at
# settlement (the close leg), so its order number never appears in the sidecars.
_FX_FORWARD_OPEN = "Forex forward open"

# Ingested doctypes that legitimately never appear in the Transactions export —
# so a sidecar of this type is neither MISSING nor UNMATCHED, just out of scope.
# A limit-extension advice is a credit-facility event, not a transaction; it
# carries an order number but the Transactions report doesn't list it.
#
# This set is one because the portal export is **comprehensive** — it lists
# every cash event too (dividends, interest, fees, payments, deposits all appear
# by ``Order nr.``), so the UNMATCHED direction is safe: everything but a
# genuine non-transaction is expected to be present. That safety depends on the
# export staying comprehensive: a securities-only or cash-dropping export
# variant would make in-window dividends/interest/fees false-positive as
# UNMATCHED, and this set would need to grow (or the direction be scoped to
# securities doctypes) — revisit here if the export shape ever changes.
_NON_TRANSACTION_DOCTYPES = frozenset({"limit_extension"})

# Single-leg order types whose export cash amount is compared to the sidecar —
# the securities feed (the tax-critical rows). Payments / transfers / fees carry
# gross-vs-net conventions that don't map 1:1, so they stay presence-only.
_AMOUNT_CHECK_TYPES = frozenset({"Subscription", "Redemption", "Buy", "Sell"})


def _sanitise_portfolio(account_no: str) -> str:
    """``K-123456.001`` → ``K123456001`` (the sidecar segment form)."""

    return account_no.replace("-", "").replace(".", "")


def _to_decimal(token: str) -> Decimal | None:
    token = token.strip()
    if not token:
        return None
    try:
        return Decimal(token)
    except InvalidOperation:
        return None


def _iso_date(token: str) -> str | None:
    """``YYYY/MM/DD`` (the export's format) → ISO, or ``None``."""

    parts = token.strip().split("/")
    if len(parts) != 3:
        return None
    year, month, day = parts
    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def _in_window(iso_date: str, period: tuple[str, str]) -> bool:
    """Is ``iso_date`` within the export's ``[start, end]`` trade-date window?
    Unparseable dates count as in-window (fail open — surface, don't drop)."""

    try:
        d = date.fromisoformat(iso_date)
        return date.fromisoformat(period[0]) <= d <= date.fromisoformat(period[1])
    except ValueError:
        return True


@dataclass(frozen=True)
class ExportRow:
    """One leg from the portal Transactions export."""

    order_number: str
    portfolio: str  # sanitised, letterless (e.g. ``173837001``)
    trade_date: str | None  # ISO
    transaction_type: str
    currency: str
    cash_amount: Decimal | None  # Net amount in current account currency (signed)
    description: str


def parse_transactions_csv(path: Path) -> list[ExportRow]:
    """Parse a portal Transactions CSV export into one :class:`ExportRow` per
    leg. cp1252 / ``;`` / CRLF; rows without an ``Order nr.`` or ``Account nr.``
    are skipped."""

    with path.open(encoding=_CSV_ENCODING, newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=_CSV_DELIMITER))

    out: list[ExportRow] = []
    for row in rows:
        order = (row.get("Order nr.") or "").strip()
        account = (row.get("Account nr.") or "").strip()
        if not order or not account:
            continue
        out.append(
            ExportRow(
                order_number=order,
                portfolio=_sanitise_portfolio(account),
                trade_date=_iso_date(row.get("Trade date") or ""),
                transaction_type=(row.get("Transaction type") or "").strip(),
                currency=(row.get("Current account currency") or "").strip(),
                cash_amount=_to_decimal(
                    row.get("Net amount in current account currency") or ""
                ),
                description=(row.get("Description of transaction") or "").strip(),
            )
        )
    return out


def group_by_portfolio(
    rows: list[ExportRow],
) -> list[tuple[str, list[ExportRow], tuple[str, str] | None]]:
    """Split export rows into per-portfolio ``(portfolio, rows, period)`` groups
    (the export holds every mandate; the diff is per-portfolio). ``period`` is
    ``(min, max trade_date)`` across the group's dated rows."""

    by_pf: dict[str, list[ExportRow]] = {}
    for row in rows:
        by_pf.setdefault(row.portfolio, []).append(row)
    groups: list[tuple[str, list[ExportRow], tuple[str, str] | None]] = []
    for portfolio, pf_rows in by_pf.items():
        dates = sorted(r.trade_date for r in pf_rows if r.trade_date)
        period = (dates[0], dates[-1]) if dates else None
        groups.append((portfolio, pf_rows, period))
    return groups


class MatchStatus(StrEnum):
    MISSING_IN_LEDGER = "missing_in_ledger"
    UNMATCHED_IN_LEDGER = "unmatched_in_ledger"
    AMOUNT_MISMATCH = "amount_mismatch"


@dataclass(frozen=True)
class ReconcileFinding:
    status: MatchStatus
    order_number: str
    date: str
    currency: str
    export_amount: Decimal | None
    sidecar_amount: Decimal | None
    description: str


@dataclass(frozen=True)
class ReconcileReport:
    matched: int
    missing_in_ledger: list[ReconcileFinding]
    unmatched_in_ledger: list[ReconcileFinding]
    amount_mismatches: list[ReconcileFinding]
    excluded: int  # Forex-forward-open orders (never ingested)
    out_of_period: int  # sidecar ids outside the export's trade-date window


def reconcile(
    export_rows: list[ExportRow],
    sidecar_rows: Sequence[Mapping[str, object]],
    *,
    portfolio: str,
    period: tuple[str, str] | None = None,
) -> ReconcileReport:
    """Diff one portfolio's export rows against the sidecars by ``Order nr.``.

    ``portfolio`` is the lettered segment (e.g. ``K999999001``) the caller
    resolved the export's bare account to; sidecar rows are filtered to it.
    """

    # Group export legs by order number; an order that is *only* FX-forward-open
    # legs is excluded (we book the forward at settlement).
    orders: dict[str, list[ExportRow]] = {}
    for row in export_rows:
        orders.setdefault(row.order_number, []).append(row)
    ingestable: dict[str, list[ExportRow]] = {}
    excluded = 0
    for order, legs in orders.items():
        if all(leg.transaction_type == _FX_FORWARD_OPEN for leg in legs):
            excluded += 1
        else:
            ingestable[order] = legs

    # This portfolio's sidecar transactions, keyed by transaction_number.
    # Non-transaction doctypes (limit extensions) are excluded — they carry an
    # order number but never appear in the Transactions export.
    sidecar_ids: dict[str, Mapping[str, object]] = {}
    for sc_row in sidecar_rows:
        account = sc_row.get("account_number")
        txn = sc_row.get("transaction_number")
        if str(sc_row.get("document_type") or "") in _NON_TRANSACTION_DOCTYPES:
            continue
        if (
            isinstance(account, str)
            and isinstance(txn, str)
            and _sanitise_portfolio(account) == portfolio
        ):
            sidecar_ids[txn] = sc_row

    matched = 0
    missing: list[ReconcileFinding] = []
    amount_mismatches: list[ReconcileFinding] = []
    for order, legs in ingestable.items():
        head = legs[0]
        sc = sidecar_ids.get(order)
        if sc is None:
            missing.append(
                ReconcileFinding(
                    status=MatchStatus.MISSING_IN_LEDGER,
                    order_number=order,
                    date=head.trade_date or "",
                    currency=head.currency,
                    export_amount=head.cash_amount,
                    sidecar_amount=None,
                    description=head.description,
                )
            )
            continue
        matched += 1
        # Single-leg securities order: the export cash amount must equal the
        # sidecar amount to the cent.
        if len(legs) == 1 and head.transaction_type in _AMOUNT_CHECK_TYPES:
            sc_amount = _to_decimal(str(sc.get("amount") or ""))
            if (
                head.cash_amount is not None
                and sc_amount is not None
                and abs(head.cash_amount - sc_amount) > _AMOUNT_TOLERANCE
            ):
                amount_mismatches.append(
                    ReconcileFinding(
                        status=MatchStatus.AMOUNT_MISMATCH,
                        order_number=order,
                        date=head.trade_date or "",
                        currency=head.currency,
                        export_amount=head.cash_amount,
                        sidecar_amount=sc_amount,
                        description=head.description,
                    )
                )

    # Sidecar ids absent from the export entirely (not even as an excluded
    # order) — a phantom / duplicate ingest. Out-of-window ids are tallied, not
    # flagged: the export covers a trade-date window and an older trade isn't on
    # a shorter export.
    unmatched: list[ReconcileFinding] = []
    out_of_period = 0
    for txn, sc_row in sidecar_ids.items():
        if txn in orders:
            continue
        sdate = sc_row.get("trade_date") or sc_row.get("settlement_date")
        if period is not None and isinstance(sdate, str) and not _in_window(sdate, period):
            out_of_period += 1
            continue
        currency = sc_row.get("currency")
        unmatched.append(
            ReconcileFinding(
                status=MatchStatus.UNMATCHED_IN_LEDGER,
                order_number=txn,
                date=sdate if isinstance(sdate, str) else "",
                currency=currency if isinstance(currency, str) else "",
                export_amount=None,
                sidecar_amount=_to_decimal(str(sc_row.get("amount") or "")),
                description=str(sc_row.get("narration") or sc_row.get("title") or ""),
            )
        )

    return ReconcileReport(
        matched=matched,
        missing_in_ledger=missing,
        unmatched_in_ledger=unmatched,
        amount_mismatches=amount_mismatches,
        excluded=excluded,
        out_of_period=out_of_period,
    )


def render_summary(name: str, report: ReconcileReport) -> str:
    """Human-readable summary for one portfolio's diff — findings first."""

    def _line(f: ReconcileFinding, amount: Decimal | None) -> str:
        amt = f"{amount:>16}" if amount is not None else " " * 16
        return f"  {f.order_number} {f.currency} {f.date} {amt}  {f.description}"

    out: list[str] = [f"Transactions reconciliation — {name}", ""]
    if report.missing_in_ledger:
        out.append(
            f"MISSING IN LEDGER ({len(report.missing_in_ledger)}) "
            "— export trade with no ingested transaction:"
        )
        out += [_line(f, f.export_amount) for f in report.missing_in_ledger]
        out.append("")
    if report.unmatched_in_ledger:
        out.append(
            f"UNMATCHED IN LEDGER ({len(report.unmatched_in_ledger)}) "
            "— ingested transaction with no export row:"
        )
        out += [_line(f, f.sidecar_amount) for f in report.unmatched_in_ledger]
        out.append("")
    if report.amount_mismatches:
        out.append(
            f"AMOUNT MISMATCH ({len(report.amount_mismatches)}) "
            "— matched order, export ≠ sidecar amount:"
        )
        for f in report.amount_mismatches:
            out.append(
                f"  {f.order_number} {f.currency} {f.date}  "
                f"export {f.export_amount} vs sidecar {f.sidecar_amount}  "
                f"{f.description}"
            )
        out.append("")
    out.append(
        f"OK: matched {report.matched} order(s) "
        f"(excluded {report.excluded} FX-forward-open, "
        f"{report.out_of_period} out-of-window)"
    )
    out.append("")
    out.append(
        f"SUMMARY: {len(report.missing_in_ledger)} missing, "
        f"{len(report.unmatched_in_ledger)} unmatched, "
        f"{len(report.amount_mismatches)} amount-mismatch"
    )
    out.append("")
    return "\n".join(out)


def render_csv(name: str, report: ReconcileReport) -> str:
    """Machine-readable findings CSV — a row per finding."""

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "export",
            "status",
            "order_number",
            "currency",
            "date",
            "export_amount",
            "sidecar_amount",
            "description",
        ]
    )
    for f in (
        *report.missing_in_ledger,
        *report.unmatched_in_ledger,
        *report.amount_mismatches,
    ):
        writer.writerow(
            [
                name,
                f.status.value,
                f.order_number,
                f.currency,
                f.date,
                "" if f.export_amount is None else str(f.export_amount),
                "" if f.sidecar_amount is None else str(f.sidecar_amount),
                f.description,
            ]
        )
    return out.getvalue()

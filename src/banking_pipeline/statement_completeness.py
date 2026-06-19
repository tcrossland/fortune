"""Current-account statement parser (transaction-level completeness).

The Pictet *current-account statement* — the EUR/USD cash ledger printed
in the annual ``Financial-statement-*.pdf`` — is the authoritative,
complete list of every cash movement for a period. This module parses
that section into structured :class:`CashLine` rows so a later diff step
can flag any movement the pipeline never ingested (a missing or
misclassified advice).

It is deliberately separate from :mod:`banking_pipeline.balances_extract`
and :mod:`banking_pipeline.prices_extract`, which read only the
*portfolio-valuation* page. Nothing else reads the cash ledger.

Parsing strategy — sign from the running balance, not the column
====================================================================
``pdftotext -layout`` preserves the DEBIT / CREDIT columns by whitespace
alone, which is fragile across layouts. Instead we read the **two
trailing money tokens** on each movement row — the movement magnitude
and the printed running balance — and recover the *sign* from the
balance delta::

    signed_amount = balance - prev_balance      (within tolerance of ±magnitude)

This makes the debit/credit determination and the running-balance
**self-check** the same operation: if the delta doesn't reconcile to
±magnitude the row is mis-parsed and we raise. Page-break
``Balance carried forward`` lines re-sync ``prev_balance`` so the check
survives across pages.

The module is pure (text in, rows out) so the parse + self-check logic
is unit-testable without PDFs.

Diff against the sidecars
=========================
:func:`diff` reconciles the parsed :class:`CashLine` rows against the
ingested ``*.transactions.jsonl`` sidecars (the substrate, never the
ledger text). The sidecar's signed ``amount`` uses the same convention
as a :class:`CashLine` (negative = cash out), and its ``settlement_date``
is the statement's VALUE DATE, so the match key is
``(currency, amount, date≈)``. Two wrinkles, both verified against the
2021–2023 archive:

* **FX / internal transfers** book one sidecar row carrying *both* legs
  (``counter_currency`` / ``counter_amount``); the statement prints them
  as two lines (one per currency section). :func:`sidecar_cash_events`
  expands the row into both legs so each matches.
* **Securities settlements** (``switch_*``,
  ``liquidacion_recepcion_de_valores``) settle on a dedicated ``Switch``
  sub-account or an ``Equity:…:Transfers`` in-specie leg — never the
  EUR/USD current account — so they produce *no* statement line and are
  excluded from the diff rather than flagged as drift.

A statement line with no sidecar event is ``MISSING_IN_LEDGER`` (the
prime signal — a likely un-ingested advice); a sidecar cash event with no
statement line is ``UNMATCHED_IN_LEDGER`` (a possible misdated or
spurious booking).
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from banking_pipeline.prices_extract import _parse_statement_date

# Running-balance self-check tolerance: statements print to the cent.
_BALANCE_TOLERANCE = Decimal("0.01")

# A money token: apostrophe-grouped thousands, exactly two decimals,
# optional leading sign. The trailing ``(?!\d)`` stops the integer part
# of a ``dd.mm.yyyy`` date (``07.2021`` → ``07.20``) from matching, and a
# leading ``(?<![\d.])`` stops a partial match inside a longer number
# (e.g. an FX rate ``1.15221084``).
_MONEY_RE = re.compile(r"(?<![\d.'])(-?\d{1,3}(?:'\d{3})*\.\d{2})(?!\d)")

# ``dd.mm.yyyy`` booking / value date.
_DATE_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{4})")
_LEADING_DATE_RE = re.compile(r"^\s*(\d{2})\.(\d{2})\.(\d{4})\s+")

# Reporting-period header: ``From <date> to <date>`` / ``Del <date> al
# <date>``, where each date is ``<day> <Month> <year>`` in either locale.
_PERIOD_RE = re.compile(
    r"\b(?:From|Del)\s+(\d{1,2}\s+\w+\s+\d{4})\s+(?:to|al)\s+(\d{1,2}\s+\w+\s+\d{4})",
    re.I,
)

# Section header naming the currency of the cash ledger that follows.
# English-titled statements (the annual ``Financial-statement``) and the
# Spanish ``Estado de la cuenta corriente`` both appear in the archive.
_SECTION_RE = re.compile(
    r"(?:Current\s+account\s+statement"
    r"|(?:Estado|Extracto)\s+de\s+(?:la\s+)?cuenta\s+corriente)"
    r"\s+(?:in|en)\s+([A-Z]{3})\b",
    re.I,
)

# Portfolio account number — ``K-NNNNNN.NNN`` — anywhere in the header.
# Anchored to reject the sub-account forms (``K-NNNNNN.NNN.00.EUR``) by
# requiring a non-digit / end after the three-digit suffix.
_ACCOUNT_RE = re.compile(r"\b([A-Z]-\d{6}\.\d{3})(?![\d.])")

# Lines that carry a running balance but are not movements. Both
# locales: ``Balance carried forward`` / ``Saldo traspasado`` re-sync the
# running balance at section start and page breaks; ``Balance as at`` /
# ``Saldo al`` is the closing line.
_CARRIED_FORWARD_RE = re.compile(
    r"\bBalance\s+carried\s+forward\b|\bSaldo\s+traspasado\b", re.I
)
_BALANCE_AS_AT_RE = re.compile(r"\bBalance\s+as\s+at\b|\bSaldo\s+al\b", re.I)

# Footer / structural lines to skip outright (English + Spanish).
_SKIP_RE = re.compile(
    r"\bDeposits/withdrawals\b|\bEntradas/Salidas\b"
    r"|\bStatement\s+without\s+reversals\b|\bExtracto\s+sin\b"
    r"|\bBIC/SWIFT\b"
    r"|\bIBAN\b"
    r"|\bFrom\s+\d|\bDel\s+\d"
    r"|\bBOOK\.\s*DATE\b|\bFECHA\s+CON\b",
    re.I,
)


@dataclass(frozen=True)
class CashLine:
    """One cash movement from the current-account statement.

    ``amount`` is **signed**: positive = credit (cash in), negative =
    debit (cash out), recovered from the running-balance delta.
    ``value_date`` is ``None`` only on the rare row that omits it.
    """

    portfolio: str  # sanitised segment, e.g. ``K999999001``
    currency: str  # ISO 4217
    book_date: str  # ISO ``YYYY-MM-DD``
    value_date: str | None
    description: str
    amount: Decimal
    running_balance: Decimal


class StatementParseError(ValueError):
    """The running-balance self-check failed — the parse is unreliable."""


def _sanitise_portfolio(account_no: str) -> str:
    """``K-123456.001`` → ``K123456001`` (the writer's segment form)."""

    return account_no.replace("-", "").replace(".", "")


def _to_decimal(token: str) -> Decimal:
    return Decimal(token.replace("'", ""))


def _iso_date(day: str, month: str, year: str) -> str | None:
    """``dd``, ``mm``, ``yyyy`` → ISO, or ``None`` if not a real date.

    Returns ``None`` for the anonymised ``99.99.9999`` form so a
    digit-masked fixture degrades to no movements instead of crashing.
    """

    try:
        return date(int(year), int(month), int(day)).isoformat()
    except ValueError:
        return None


def parse_current_account(text: str) -> list[CashLine]:
    """Parse every current-account (cash) movement in a statement dump.

    Returns the movements in document order across the EUR and USD
    sections. Raises :class:`StatementParseError` if any row's printed
    running balance doesn't reconcile to the prior balance ± the movement
    magnitude (a mis-parse), so a silent column-misread can't produce
    plausible-but-wrong rows.

    Returns ``[]`` when no portfolio account number or no cash section is
    found (e.g. the anonymised fixture header, or a valuation-only dump).
    """

    account_match = _ACCOUNT_RE.search(text)
    if account_match is None:
        return []
    portfolio = _sanitise_portfolio(account_match.group(1))

    rows: list[CashLine] = []
    currency: str | None = None
    # Running balance per currency. Tracked per-currency, not as one
    # scalar, because the section header (``Current account statement in
    # EUR``) *repeats at every page top* under pypdfium2 — resetting on it
    # would wipe the balance chain mid-section. The chain only restarts
    # when the currency genuinely changes (EUR → USD), where that
    # section's dated opening ``Balance carried forward`` re-anchors it.
    prev_by_ccy: dict[str, Decimal] = {}

    for raw in text.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        section = _SECTION_RE.search(line)
        if section is not None:
            currency = section.group(1).upper()
            continue

        if currency is None:
            continue

        # Re-sync the running balance at the section opener and close.
        # Page-break ``Balance carried forward`` repeats print no number
        # under pypdfium2 — those simply leave the prior balance in place.
        if _CARRIED_FORWARD_RE.search(line) or _BALANCE_AS_AT_RE.search(line):
            balances = _MONEY_RE.findall(line)
            if balances:
                prev_by_ccy[currency] = _to_decimal(balances[-1])
            continue

        if _SKIP_RE.search(line):
            continue

        lead = _LEADING_DATE_RE.match(line)
        if lead is None:
            # Not a movement row (stray continuation / disclaimer prose).
            continue

        row = _parse_movement(
            line, lead, currency, portfolio, prev_by_ccy.get(currency)
        )
        if row is None:
            continue
        rows.append(row)
        prev_by_ccy[currency] = row.running_balance

    return rows


def _parse_movement(
    line: str,
    lead: re.Match[str],
    currency: str,
    portfolio: str,
    prev_balance: Decimal | None,
) -> CashLine | None:
    """Parse one movement row, signing the amount from the balance delta.

    Returns ``None`` for a date-led line that carries no money tokens
    (not a real movement). Raises if the self-check fails.
    """

    book_date = _iso_date(lead.group(1), lead.group(2), lead.group(3))
    if book_date is None:
        return None  # masked / invalid date — not a real movement
    rest = line[lead.end() :]

    # The value date (if any) splits description from the amount columns.
    value_match = _DATE_RE.search(rest)
    if value_match is not None:
        description = rest[: value_match.start()].strip()
        tail = rest[value_match.end() :]
        value_date: str | None = _iso_date(*value_match.groups())
    else:
        money = _MONEY_RE.search(rest)
        description = rest[: money.start()].strip() if money else rest.strip()
        tail = rest if money is None else rest[money.start() :]
        value_date = None

    tokens = _MONEY_RE.findall(tail)
    if len(tokens) < 2:
        # No ``<amount> <balance>`` pair — not a bookable movement.
        return None

    try:
        magnitude = _to_decimal(tokens[-2])
        balance = _to_decimal(tokens[-1])
    except InvalidOperation:  # pragma: no cover - regex guarantees shape
        return None

    if prev_balance is None:
        raise StatementParseError(
            f"movement before any balance anchor: {description!r} ({book_date})"
        )

    delta = balance - prev_balance
    if (delta - magnitude).copy_abs() <= _BALANCE_TOLERANCE:
        amount = magnitude  # credit / inflow
    elif (delta + magnitude).copy_abs() <= _BALANCE_TOLERANCE:
        amount = -magnitude  # debit / outflow
    else:
        raise StatementParseError(
            f"running balance does not reconcile at {description!r} "
            f"({book_date}): {prev_balance} → {balance} is not "
            f"±{magnitude}"
        )

    return CashLine(
        portfolio=portfolio,
        currency=currency,
        book_date=book_date,
        value_date=value_date,
        description=description,
        amount=amount,
        running_balance=balance,
    )


# Doctypes whose cash leg settles *outside* the EUR/USD current account —
# a dedicated ``Switch`` sub-account (fund-to-fund rotations) or an
# ``Equity:…:Transfers`` in-specie leg (free securities receipts). They
# never produce a current-account statement line, so a sidecar row of one
# of these types is not expected to match and is excluded from the diff.
# Verified against the 2021–2023 ledgers; anything *not* listed here
# defaults to "should appear in the cash ledger" so a genuinely missing or
# misdated cash booking surfaces loudly rather than being silently dropped.
_NON_CURRENT_ACCOUNT_DOCTYPES = frozenset({
    "switch_salida",
    "switch_entrada",
    "liquidacion_recepcion_de_valores",
    "liquidacion_aviso_previo_recepcion",
})

# A statement line's VALUE DATE and a sidecar's settlement date should
# agree, but advices occasionally stamp the booking date instead, so the
# match allows a few days' slack and prefers the closest date.
_DATE_TOLERANCE_DAYS = 5


class MatchStatus(StrEnum):
    """Verdict for one reconciled cash event."""

    MATCHED = "matched"
    MISSING_IN_LEDGER = "missing_in_ledger"  # statement line, no sidecar
    UNMATCHED_IN_LEDGER = "unmatched_in_ledger"  # sidecar event, no line


@dataclass(frozen=True)
class SidecarEvent:
    """One current-account cash movement expected from a sidecar row.

    A row yields one event normally and two for an internal/FX transfer
    (the second is the ``counter_*`` leg, ``is_counter_leg=True``).
    """

    currency: str
    settlement_date: str  # ISO; the statement VALUE DATE counterpart
    amount: Decimal  # signed, negative = cash out
    document_type: str
    narration: str
    source: str | None
    dedup_key: str | None
    is_counter_leg: bool = False


@dataclass(frozen=True)
class CompletenessRow:
    """One row of the completeness report."""

    status: MatchStatus
    currency: str
    date: str
    amount: Decimal
    description: str
    document_type: str | None = None
    source: str | None = None


@dataclass(frozen=True)
class CompletenessReport:
    """The diff outcome: matches, and the two kinds of finding."""

    matched: int
    missing_in_ledger: list[CompletenessRow]
    unmatched_in_ledger: list[CompletenessRow]
    excluded: int  # sidecar rows that settle outside the current account
    out_of_period: int  # sidecar events dated outside the statement window

    @property
    def has_findings(self) -> bool:
        return bool(self.missing_in_ledger or self.unmatched_in_ledger)


def _decimal_or_none(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def sidecar_cash_events(row: Mapping[str, object]) -> list[SidecarEvent]:
    """Expand one sidecar transaction into its current-account leg(s).

    Returns ``[]`` for a row that posts no current-account cash movement —
    a securities-settlement doctype, or a row missing the amount /
    currency / settlement date the match needs. Transfers yield two events
    (the cash leg and its ``counter_*`` leg).
    """

    doctype = str(row.get("document_type") or "")
    if doctype in _NON_CURRENT_ACCOUNT_DOCTYPES:
        return []

    amount = _decimal_or_none(row.get("amount"))
    currency = row.get("currency")
    settlement = row.get("settlement_date") or row.get("trade_date")
    if amount is None or not isinstance(currency, str) or not isinstance(settlement, str):
        return []

    narration = str(row.get("narration") or row.get("title") or "")
    source = row.get("source_path")
    dedup = row.get("dedup_key")
    source_s = source if isinstance(source, str) else None
    dedup_s = dedup if isinstance(dedup, str) else None

    events = [
        SidecarEvent(
            currency=currency,
            settlement_date=settlement,
            amount=amount,
            document_type=doctype,
            narration=narration,
            source=source_s,
            dedup_key=dedup_s,
        )
    ]

    counter_ccy = row.get("counter_currency")
    counter_amt = _decimal_or_none(row.get("counter_amount"))
    if isinstance(counter_ccy, str) and counter_amt is not None:
        events.append(
            SidecarEvent(
                currency=counter_ccy,
                settlement_date=settlement,
                amount=counter_amt,
                document_type=doctype,
                narration=narration,
                source=source_s,
                dedup_key=dedup_s,
                is_counter_leg=True,
            )
        )
    return events


def _days_apart(a: str, b: str) -> int | None:
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except ValueError:
        return None


def parse_statement_period(text: str) -> tuple[str, str] | None:
    """Parse the statement's reporting window from its ``From … to …`` /
    ``Del … al …`` header. Returns ``(start, end)`` ISO dates, or ``None``
    if absent / unparseable (e.g. the anonymised ``9 Enero 9999`` form).

    The diff uses this to bound which sidecar events are *expected* to
    appear: an annual statement covers one year, so a transaction settling
    outside that window belongs to a different statement, not a gap.
    """

    match = _PERIOD_RE.search(text)
    if match is None:
        return None
    start = _parse_statement_date(match.group(1))
    end = _parse_statement_date(match.group(2))
    if start is None or end is None:
        return None
    return (start.isoformat(), end.isoformat())


def diff(
    cash_lines: list[CashLine],
    sidecar_rows: Sequence[Mapping[str, object]],
    *,
    period: tuple[str, str] | None = None,
    portfolio: str | None = None,
    date_tolerance_days: int = _DATE_TOLERANCE_DAYS,
) -> CompletenessReport:
    """Reconcile parsed statement cash lines against sidecar transactions.

    Matches on ``(currency, amount)`` exactly with the date within
    ``date_tolerance_days`` (closest wins). Each sidecar event is consumed
    at most once, so duplicate same-day same-amount movements need a
    distinct statement line each. The result splits into matched, lines
    missing from the ledger, and sidecar events absent from the statement.

    ``period`` bounds the ``UNMATCHED_IN_LEDGER`` check to the statement's
    reporting window (``(start, end)`` ISO dates). An unmatched event whose
    settlement date falls outside the window is *not* a finding — it
    belongs to a neighbouring statement — and is tallied as
    ``out_of_period`` instead. A *matched* line is always a match,
    regardless of window.

    ``portfolio`` (a sanitised segment like ``K999999001``) restricts the
    sidecar rows to one account, so a statement is never diffed against
    another portfolio's transactions when the whole ``data/`` tree is
    loaded at once.

    When ``period`` is given, rows whose date falls outside
    ``[start − tolerance, end + tolerance]`` are skipped entirely before
    counting — so ``excluded`` / ``out_of_period`` reflect only this
    statement's window, not every year in ``data/``. This is safe for
    matching: a statement line is in-period, and only matches a sidecar
    settling within ``tolerance`` of an in-period date, which is inside the
    window.
    """

    events: list[SidecarEvent] = []
    excluded = 0
    for row in sidecar_rows:
        if portfolio is not None:
            account = row.get("account_number")
            # A non-portfolio ``account_number`` (e.g. the rare IBAN
            # fallback when the header lacks a ``K-NNNNNN.NNN``) drops the
            # row, so its statement line surfaces as MISSING rather than
            # silently matching — fail loud, don't mis-reconcile.
            if isinstance(account, str) and _sanitise_portfolio(account) != portfolio:
                continue
        if period is not None:
            row_date = row.get("settlement_date") or row.get("trade_date")
            if isinstance(row_date, str) and not _within_window(
                row_date, period, date_tolerance_days
            ):
                continue
        expanded = sidecar_cash_events(row)
        # Count only genuine securities-settlements as `excluded`; a row
        # that yielded no events because it's malformed (missing
        # amount/currency/date) isn't one, so re-test the doctype rather
        # than keying off the empty result.
        if not expanded and str(row.get("document_type") or "") in _NON_CURRENT_ACCOUNT_DOCTYPES:
            excluded += 1
        events.extend(expanded)

    used = [False] * len(events)
    matched = 0
    missing: list[CompletenessRow] = []

    for line in cash_lines:
        target_date = line.value_date or line.book_date
        best: tuple[int, int] | None = None  # (day distance, index)
        for i, ev in enumerate(events):
            if used[i] or ev.currency != line.currency or ev.amount != line.amount:
                continue
            dist = _days_apart(target_date, ev.settlement_date)
            if dist is None or dist > date_tolerance_days:
                continue
            if best is None or dist < best[0]:
                best = (dist, i)
        if best is None:
            missing.append(
                CompletenessRow(
                    status=MatchStatus.MISSING_IN_LEDGER,
                    currency=line.currency,
                    date=target_date,
                    amount=line.amount,
                    description=line.description,
                )
            )
        else:
            used[best[1]] = True
            matched += 1

    unmatched: list[CompletenessRow] = []
    out_of_period = 0
    for i, ev in enumerate(events):
        if used[i]:
            continue
        if period is not None and not _in_period(ev.settlement_date, period):
            out_of_period += 1
            continue
        unmatched.append(
            CompletenessRow(
                status=MatchStatus.UNMATCHED_IN_LEDGER,
                currency=ev.currency,
                date=ev.settlement_date,
                amount=ev.amount,
                description=ev.narration,
                document_type=ev.document_type,
                source=ev.source,
            )
        )

    return CompletenessReport(
        matched=matched,
        missing_in_ledger=missing,
        unmatched_in_ledger=unmatched,
        excluded=excluded,
        out_of_period=out_of_period,
    )


def _in_period(iso_date: str, period: tuple[str, str]) -> bool:
    """Is ``iso_date`` within the statement's ``[start, end]`` window?

    The cash ledger lists a movement by its value (settlement) date, so an
    event settling after ``end`` belongs to the *next* statement, not this
    one — a strict bound, no end slack. Unparseable dates count as
    in-period (fail open — better to surface a questionable event than to
    silently drop it).
    """

    try:
        d = date.fromisoformat(iso_date)
        start = date.fromisoformat(period[0])
        end = date.fromisoformat(period[1])
    except ValueError:
        return True
    return start <= d <= end


def _within_window(iso_date: str, period: tuple[str, str], tolerance_days: int) -> bool:
    """Is ``iso_date`` within ``[start − tolerance, end + tolerance]``?

    The relevance gate for the diff: rows outside this band can't match any
    in-period statement line (matches are within ``tolerance`` of an
    in-period date), so they're irrelevant to *this* statement and skipped
    before the diagnostic tallies. Unparseable dates count as in-window
    (fail open). Wider than :func:`_in_period` by the match tolerance.
    """

    try:
        d = date.fromisoformat(iso_date)
        start = date.fromisoformat(period[0])
        end = date.fromisoformat(period[1])
    except ValueError:
        return True
    return start - timedelta(days=tolerance_days) <= d <= end + timedelta(
        days=tolerance_days
    )


def render_summary(name: str, report: CompletenessReport) -> str:
    """Human-readable summary for one statement's diff.

    One statement per file (named by statement date) so successive runs
    don't clobber each other. Leads with the findings — the point of the
    report — and closes with a one-line verdict.
    """

    out: list[str] = [f"Statement completeness — {name}", ""]
    if report.missing_in_ledger:
        out.append(
            f"MISSING IN LEDGER ({len(report.missing_in_ledger)}) "
            "— statement line with no ingested advice:"
        )
        for r in report.missing_in_ledger:
            out.append(f"  {r.currency} {r.date} {r.amount:>16}  {r.description}")
    if report.unmatched_in_ledger:
        out.append(
            f"UNMATCHED IN LEDGER ({len(report.unmatched_in_ledger)}) "
            "— ingested cash event with no statement line:"
        )
        for r in report.unmatched_in_ledger:
            doctype = f"[{r.document_type}] " if r.document_type else ""
            out.append(
                f"  {r.currency} {r.date} {r.amount:>16}  {doctype}{r.description}"
            )
    out.append(
        f"OK: matched {report.matched} line(s) "
        f"(excluded {report.excluded} securities-settlement, "
        f"{report.out_of_period} out-of-period)"
    )
    out.append("")
    out.append(
        f"SUMMARY: {len(report.missing_in_ledger)} missing, "
        f"{len(report.unmatched_in_ledger)} unmatched"
    )
    out.append("")
    return "\n".join(out)


def render_csv(name: str, report: CompletenessReport) -> str:
    """Machine-readable findings CSV for one statement — a row per finding."""

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        ["statement", "status", "currency", "date", "amount", "document_type", "description"]
    )
    for r in (*report.missing_in_ledger, *report.unmatched_in_ledger):
        writer.writerow(
            [
                name,
                r.status.value,
                r.currency,
                r.date,
                str(r.amount),
                r.document_type or "",
                r.description,
            ]
        )
    return out.getvalue()

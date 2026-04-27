"""Shared parsing helpers for Pictet advice documents.

Pictet's PDF-to-text output is remarkably consistent across its single-event
advices (subscription, redemption, dividend, fee debit, interest payment, …):

  - Numbers use Swiss apostrophe thousands separators: ``99'999.99``.
  - Negative amounts have a leading ``-`` (no parentheses): ``-23'700.00``.
  - Dates are ``dd.mm.yyyy``.
  - Each field is a label-then-value pair on its own line, with the ISO
    currency code prefixed to amount values
    (``Gross amount EUR -119'890.47``).

These helpers extract those primitives. Per-doctype templates compose them
into ``Transaction`` objects rather than reinventing the regex each time.

Locale support
--------------
Pictet's Madrid branch issues advices in Spanish using the same overall
template skeleton with translated field labels (``Fecha de transacción``
instead of ``Trade date``, ``EFECTO CASH`` instead of ``CASH EFFECT``, etc.).
The :class:`PictetLabels` dataclass bundles the labels used by a single
locale; helpers default to :data:`EN_LABELS` so existing callers stay
unchanged, and ES templates pass :data:`ES_LABELS` explicitly.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from banking_pipeline.fields.validators import normalise_iban, normalise_isin
from banking_pipeline.models import RawDocument, Transaction

# dd.mm.yyyy — Pictet always pads to two digits for day/month.
_DATE_RE = re.compile(r"\b(\d{2})\.(\d{2})\.(\d{4})\b")

# Swiss-formatted number with optional sign and decimals. Used inside
# label-line regexes via string interpolation, so kept as a plain string
# rather than a compiled pattern.
_NUMBER = r"-?\d{1,3}(?:'\d{3})*(?:\.\d+)?"


# ---------------------------------------------------------------------------
# Locale labels
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PictetLabels:
    """Field labels used in a particular Pictet locale.

    Pictet's Luxembourg-issued advices come in English; the Madrid branch
    issues advices in Spanish using the same templates with translated
    field labels. This class holds the labels each locale uses, so the
    parsing helpers can stay locale-agnostic and per-doctype templates
    declare which locale they target.

    The ``portfolio_in_re`` and ``account_no_re`` fields hold pre-compiled
    regexes rather than plain strings because both need internal grouping
    that's awkward to express as ``re.escape``-able literals (the Pictet
    portfolio-ID format ``[A-Z]-\\d{6}\\.\\d{3}`` is a capture group, and
    the inter-word spacing on ``N° de cuenta`` etc. needs ``\\s+`` flex).
    """

    # Single-line ``label value`` field labels
    trade_date: str
    value_date: str
    booking_date: str
    # Operation-type accepts a tuple because Pictet renamed this field across
    # document generations: 2023+ advices say ``Tipo de operación`` while
    # 2021-era advices say ``Tipo de ejecución``. Helpers try each in order
    # and use the first one that matches; English advices have only one name
    # historically, so EN_LABELS still passes a single-element tuple.
    operation_type: tuple[str, ...]
    executed_quantity: str
    execution_price: str
    gross_amount: str
    net_amount: str
    # Per-leg fee line inside the CASH EFFECT block (``Costs USD -23.45`` /
    # ``Costes USD -23,45``). Only meaningful on advices that carry one;
    # ``find_amount_field`` returns ``None`` when the line is absent.
    costs: str
    # FX-only intermediate line: gross + costs in the security currency
    # (``Subtotal USD -73'665.87``). Absent on non-FX advices, where the
    # CASH EFFECT block jumps straight from costs to net amount.
    subtotal: str

    # Section markers
    cash_effect_marker: str  # the literal split string

    # Document patterns
    portfolio_in_re: re.Pattern[str]
    account_no_re: re.Pattern[str]
    # Header reference number. Pictet writes it as ``N° de transacción: NNN``
    # (ES) / ``Transaction no.: NNN`` (EN), always on the same header line as
    # the publication date — the regex is anchored to start-of-line so the
    # pipe-and-publication-date trailer doesn't get swept into the capture.
    transaction_number_re: re.Pattern[str]
    # FX rate line — Pictet writes ``Tipo de cambio (EUR/USD): 1.18585481``
    # (ES) / ``Exchange rate (EUR/USD): 1.18585481`` (EN). The numeric form
    # uses an ISO decimal point regardless of locale, so the regex captures
    # the value with a permissive ``[^:]*:\s*([\d.]+)`` tail.
    exchange_rate_re: re.Pattern[str]

    # Headline detection
    headline_verbs: tuple[str, ...] = field(default_factory=tuple)


EN_LABELS = PictetLabels(
    trade_date="Trade date",
    value_date="Value date",
    booking_date="Booking date",
    operation_type=("Operation type",),
    executed_quantity="Executed quantity",
    execution_price="Execution price",
    gross_amount="Gross amount",
    net_amount="Net amount",
    costs="Costs",
    subtotal="Subtotal",
    cash_effect_marker="CASH EFFECT",
    portfolio_in_re=re.compile(
        r"^\s*in\s+portfolio\s+([A-Z]-\d{6}\.\d{3})", re.M
    ),
    account_no_re=re.compile(
        r"^Account no\.\s*:\s*([A-Z]-\d{6}\.\d{3})", re.M
    ),
    transaction_number_re=re.compile(
        r"^Transaction\s+no\.\s*:\s*(\d+)", re.M
    ),
    exchange_rate_re=re.compile(
        r"\bExchange\s+rate\b[^:]*:\s*([\d.]+)", re.I
    ),
    headline_verbs=("Purchase", "Sale", "Buy", "Sell"),
)


ES_LABELS = PictetLabels(
    trade_date="Fecha de transacción",
    value_date="Fecha valor",
    booking_date="Fecha contable",
    # 2023+ advices use ``Tipo de operación``; 2021-era advices use ``Tipo de
    # ejecución`` for the same field. Both carry ``Compra`` / ``Venta`` as
    # values, so ``expected_operations`` checks still apply uniformly.
    operation_type=("Tipo de operación", "Tipo de ejecución"),
    executed_quantity="Cantidad ejecutada",
    execution_price="Precio de ejecución",
    gross_amount="Importe bruto",
    net_amount="Importe neto",
    costs="Costes",
    subtotal="Subtotal",
    cash_effect_marker="EFECTO CASH",
    portfolio_in_re=re.compile(
        r"^\s*en\s+la\s+cartera\s+([A-Z]-\d{6}\.\d{3})", re.M
    ),
    account_no_re=re.compile(
        r"^N°\s*de\s+cuenta\s*:\s*([A-Z]-\d{6}\.\d{3})", re.M
    ),
    transaction_number_re=re.compile(
        r"^N°\s*de\s+transacci[oó]n\s*:\s*(\d+)", re.M
    ),
    exchange_rate_re=re.compile(
        r"\bTipo\s+de\s+cambio\b[^:]*:\s*([\d.]+)", re.I
    ),
    # ``Suscripción`` / ``Reembolso`` titles aren't headline verbs — the
    # actual narration line uses ``Compra`` / ``Venta`` (e.g.
    # ``Compra 1'296.000 AB SICAV ... a USD 44.44``).
    headline_verbs=("Compra", "Venta"),
)


# ---------------------------------------------------------------------------
# Primitives — language-agnostic
# ---------------------------------------------------------------------------


def parse_pictet_date(value: str) -> date:
    """Parse a ``dd.mm.yyyy`` Pictet date string."""

    m = _DATE_RE.search(value)
    if not m:
        raise ValueError(f"Not a Pictet-format date: {value!r}")
    day, month, year = (int(g) for g in m.groups())
    return date(year, month, day)


def parse_pictet_amount(value: str) -> Decimal:
    """Parse a Swiss-formatted number (apostrophe thousands separator)."""

    cleaned = value.replace("'", "").strip()
    return Decimal(cleaned)


def find_field(text: str, label: str) -> str | None:
    """Return the value following ``label`` on the same line, or ``None``.

    Pictet writes fields as ``<label> <value>`` with the value running to end
    of line. Anchored to a line start so unrelated mentions of the label
    inside narration text don't false-match.
    """

    pattern = re.compile(rf"^{re.escape(label)}\s+(.+?)\s*$", re.M)
    m = pattern.search(text)
    return m.group(1) if m else None


def find_amount_field(text: str, label: str) -> tuple[str, Decimal] | None:
    """Match a ``<label> <CCY> <amount>`` line.

    Pictet writes amounts with the ISO currency code immediately preceding
    the value; we capture them as a pair so callers don't have to re-split.
    """

    pattern = re.compile(
        rf"^{re.escape(label)}\s+([A-Z]{{3}})\s+({_NUMBER})\s*$", re.M
    )
    m = pattern.search(text)
    if not m:
        return None
    return m.group(1), parse_pictet_amount(m.group(2))


def find_isin(text: str) -> str | None:
    """Extract the ISIN from the standard Pictet portfolio holding line.

    Two layouts seen in fixtures:

      ``ISIN/Internal ref.: LU1234567890 Telekurs ID/Internal ref.: ...``
      ``ISIN: LU1234567890 Telekurs ID: ...``
    """

    pattern = re.compile(
        r"\bISIN(?:/Internal\s+ref\.)?\s*:\s*([A-Z]{2}[A-Z0-9]{9}[A-Z0-9])"
    )
    m = pattern.search(text)
    return m.group(1) if m else None


def find_iban(text: str) -> str | None:
    """Extract the IBAN from a Pictet ``Current account ... IBAN?<IBAN>`` line.

    Pictet glues the ``?`` directly to the IBAN with no surrounding spaces in
    its template — a stable marker we can pin off without false-firing on
    third-party IBANs that appear elsewhere in the document.
    """

    pattern = re.compile(r"\bIBAN\?([A-Z]{2}\d[A-Z0-9]+)")
    m = pattern.search(text)
    return m.group(1) if m else None


def find_subject_line(text: str, prefix: str) -> str | None:
    """Match a ``<prefix> - <subject>`` narration line.

    Pictet labels security events by their type followed by the underlying
    instrument: ``Dividend - JPMF-INCOME FD ...``,
    ``Redemption - EUR PWM LG VOL BALANC (PICTET)25/26``. This helper
    returns the subject (everything after the dash) so callers can wrap it
    in a template-specific narration prefix.
    """

    pattern = re.compile(rf"^{re.escape(prefix)}\s+-\s+(.+?)\s*$", re.M)
    m = pattern.search(text)
    return m.group(1) if m else None


def find_balance_currency(text: str) -> str | None:
    """Extract the ISO currency code from an interest-scale's column header.

    Pictet writes interest-scale tables with the format
    ``PERIOD DAYS BALANCE (USD) RATE (%) INTEREST (USD)``; both column
    parentheticals carry the same currency, so we just match the first one.
    """

    pattern = re.compile(r"\bBALANCE\s*\(([A-Z]{3})\)")
    m = pattern.search(text)
    return m.group(1) if m else None


def find_total_amount(text: str) -> Decimal | None:
    """Match the ``Total <days> <amount>`` summary line on interest scales.

    The interest-scale table opens with a totals row that sums the
    per-bucket interest (e.g. ``Total 90 -3'510.65``). Currency lives in
    the column header, not on this line, so we return only the amount.
    """

    pattern = re.compile(rf"^Total\s+\d+\s+({_NUMBER})\s*$", re.M)
    m = pattern.search(text)
    return parse_pictet_amount(m.group(1)) if m else None


def find_period(text: str, label: str = "Period") -> tuple[date, date] | None:
    """Parse a ``<label> dd.mm.yyyy - dd.mm.yyyy`` line into a (start, end)
    pair. ``label`` defaults to ``Period`` (EN); pass ``"Período"`` for ES
    fee/invoice advices."""

    pattern = re.compile(
        rf"^{re.escape(label)}\s+(\d{{2}}\.\d{{2}}\.\d{{4}})\s*-\s*(\d{{2}}\.\d{{2}}\.\d{{4}})\s*$",
        re.M,
    )
    m = pattern.search(text)
    if not m:
        return None
    return parse_pictet_date(m.group(1)), parse_pictet_date(m.group(2))


def find_comment_line(text: str, label: str = "Comment") -> str | None:
    """The first non-blank line of Pictet's free-form ``Comment`` block.

    Pictet writes ``Comment`` (EN) / ``Comentario`` (ES) on its own line
    followed by one or more text lines. We return the first such line as a
    narration source for advices that have no headline verb.
    """

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip() == label:
            for follow in lines[i + 1 :]:
                stripped = follow.strip()
                if stripped:
                    return stripped
            return None
    return None


def resolve_isin(text: str) -> str | None:
    """Return the ISIN, preferring the validator's canonical form when the
    checksum passes and falling back to the raw value otherwise.

    Pictet uses the same ``ISIN/Internal ref.`` field for both real ISINs
    (e.g. ``LU2096759431``) and proprietary structured-product codes
    (e.g. ``ZZ00ABB5K50``) — the latter won't validate but still carries
    information the beancount writer wants to preserve.
    """

    raw = find_isin(text)
    if raw is None:
        return None
    return normalise_isin(raw) or raw


# ---------------------------------------------------------------------------
# Locale-aware helpers
# ---------------------------------------------------------------------------


def find_pictet_account(text: str, labels: PictetLabels = EN_LABELS) -> str | None:
    """Pictet's internal portfolio account ID, e.g. ``P-123456.789``.

    Useful as a fallback when the IBAN won't validate (notably on the
    anonymised test fixtures, where the IBAN's checksum is fake).
    """

    m = labels.account_no_re.search(text)
    return m.group(1) if m else None


def find_transaction_number(
    text: str, labels: PictetLabels = EN_LABELS
) -> str | None:
    """Pictet's per-document reference number, e.g. ``717848921``.

    Pictet writes this on the document header line:
    ``N° de transacción: 717848921 | Fecha de publicación: 10.09.2021`` (ES)
    ``Transaction no.: 1129889269 | Publication date: 21.10.2025`` (EN).
    Returned as a string (no integer parsing) to preserve any leading
    zeros on documents we haven't seen yet.
    """

    m = labels.transaction_number_re.search(text)
    return m.group(1) if m else None


def find_exchange_rate(
    text: str, labels: PictetLabels = EN_LABELS
) -> Decimal | None:
    """The FX conversion rate quoted inside the CASH EFFECT block, or
    ``None`` if the document doesn't carry one (non-FX advices).

    Pictet writes ``Tipo de cambio (EUR/USD): 1.18585481`` (ES) or
    ``Exchange rate (EUR/USD): 1.18585481`` (EN). The rate itself uses an
    ISO decimal point regardless of locale.
    """

    m = labels.exchange_rate_re.search(text)
    if m is None:
        return None
    return Decimal(m.group(1))


def find_fee_breakdown(
    text: str,
    *,
    costs_label: str = "Costes",
    total_label: str = "Total",
) -> list["FeeItem"]:
    """Walk the ``Costes`` / ``Costs`` block and extract per-line fee items.

    Pictet quarterly fee advices print a costs block with one line per
    fee component followed by a ``Total`` summary::

        Costes
        Honorarios de gestión EUR -8'467.20
        IVA extranjero EUR -1'778.11
        Total EUR -10'245.31

    The helper enters the block at the standalone ``costs_label`` line,
    yields one :class:`~banking_pipeline.models.FeeItem` per single-line
    ``<label> <CCY> <amount>`` row, and stops when it hits ``Total`` or
    a non-matching line (section change). Amounts are stored signed as
    printed (negative for cash-out); the writer flips signs at render.

    Limitation: multi-line label wrapping (where Pictet splits long fee
    names across two or three lines before the currency+amount line)
    isn't handled — affects the 2023 ES and the 2026 EN ``debit_of_fees``
    fixtures. Add a multi-line accumulator when those fixtures get their
    own goldens; the 2021 ES fixture this helper targets has all fee
    items on a single line.
    """

    from banking_pipeline.models import FeeItem  # avoid import cycle

    items: list[FeeItem] = []
    in_block = False
    inline_re = re.compile(
        rf"^(.+?)\s+([A-Z]{{3}})\s+({_NUMBER})\s*$"
    )

    for line in text.splitlines():
        stripped = line.strip()
        if not in_block:
            if stripped == costs_label:
                in_block = True
            continue
        if not stripped:
            # Blank line inside the block — skip without ending; Pictet
            # occasionally pads vertical space between fee items.
            continue
        m = inline_re.match(stripped)
        if not m:
            # Multi-line label or section change — stop at the first
            # non-matching line. The single-line-only contract is
            # documented above; revisit when fixtures with multi-line
            # labels need to round-trip through the writer.
            break
        label, ccy, amount_str = m.groups()
        if label.strip().lower() == total_label.lower():
            break
        items.append(
            FeeItem(
                description=label.strip(),
                amount=parse_pictet_amount(amount_str),
                currency=ccy,
            )
        )
    return items


def find_switch_fund_name(text: str, side: str) -> str | None:
    """Extract the fund name from a Pictet switch advice's portfolio block.

    Pictet switch documents identify each leg with a portfolio header and
    a fund-name line directly below it::

        SALIDA de la cartera K-123456.001
        MSIF-GLOBAL QUALITY FUND ZH EUR-ACC 1'177.000

    ``side`` selects which leg to read — ``"SALIDA"`` (outgoing) or
    ``"ENTRADA"`` (incoming). The helper handles both ``de la cartera``
    (used by salida) and ``en la cartera`` (used by entrada and most
    other ES advices) prepositions, returns the fund-name line with the
    trailing Swiss-formatted quantity stripped, and is used by the
    switch templates to compose the ``"<side> <fund>"`` narration —
    switch advices carry no ``Compra``/``Venta`` headline line, so the
    standard :func:`find_headline` returns ``None`` on them.
    """

    portfolio_re = re.compile(
        rf"^{re.escape(side)}\s+(?:de|en)\s+la\s+cartera\b", re.I
    )
    quantity_tail_re = re.compile(
        rf"\s+-?\d{{1,3}}(?:'\d{{3}})*(?:\.\d+)?\s*$"
    )
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if portfolio_re.match(line.strip()):
            if i + 1 >= len(lines):
                return None
            fund_line = lines[i + 1].strip()
            stripped = quantity_tail_re.sub("", fund_line)
            return stripped or None
    return None


def find_headline(text: str, labels: PictetLabels = EN_LABELS) -> str | None:
    """The one-line transaction summary Pictet prints near the top of every
    single-event advice (e.g. ``Purchase 469.00 ELEVA-... at EUR 255.63``,
    or in Spanish ``Compra 1'296.000 AB SICAV ... a USD 44.44``).

    Returns the most human-readable narration available; callers can
    truncate or post-process as their model requires.
    """

    if not labels.headline_verbs:
        return None
    pattern = re.compile(
        rf"^(?:{'|'.join(labels.headline_verbs)})\b.*\b[A-Z]{{3}}\b\s*-?\s*\d",
        re.I,
    )
    for line in text.splitlines():
        stripped = line.strip()
        if pattern.match(stripped):
            return stripped
    return None


def resolve_account_number(
    text: str, labels: PictetLabels = EN_LABELS
) -> str | None:
    """Pick the most useful account identifier present in the document.

    Order of preference:
        1. Pictet's internal portfolio ID (``P-123456.789`` etc.) — read
           from the document header's ``N° de cuenta`` / ``Account no.``
           line. Always present and stable across every Pictet advice
           type, regardless of whether the document carries an IBAN.
        2. Validated IBAN — kept only when ``stdnum`` accepts the
           checksum. Used as a fallback when the portfolio header is
           missing or doesn't match the expected format (rare).

    Returning the Pictet ID rather than the raw IBAN keeps downstream
    beancount postings keyed on the user's portfolio identity rather
    than on a bank-side IBAN whose validity (and stability across the
    bank's own renumbering) we shouldn't rely on. Earlier the order
    here was reversed — IBAN-first, portfolio-fallback — and the only
    reason existing tests passed was that anonymised fixtures' IBANs
    failed the mod-97 checksum, so the fallback branch always fired.
    On real PDFs with real valid IBANs that branch returned the IBAN,
    which contradicted both this docstring's intent and every per-template
    test's expected ``account_number`` value.
    """

    portfolio = find_pictet_account(text, labels)
    if portfolio is not None:
        return portfolio
    iban = find_iban(text)
    if iban is not None:
        validated = normalise_iban(iban)
        if validated:
            return validated
    return None


# ---------------------------------------------------------------------------
# Multi-leg ``CASH EFFECT`` parsing
#
# Pictet's spot, FX-forward, FX-forward-settle, and internal-transfer advices
# all carry **two** ``CASH EFFECT`` blocks per document — one per portfolio
# leg / currency. Each block is self-contained: portfolio account marker on
# its first line, then ``Gross amount`` / ``Costs`` / optional ``Sub-total``
# / optional in-block ``Exchange rate`` / ``Net amount`` / ``Current account``
# (which carries the IBAN). Splitting on the literal ``CASH EFFECT`` marker
# (or ``EFECTO CASH`` for Spanish-locale docs) is the cleanest way to walk
# them.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CashEffectLeg:
    """One ``CASH EFFECT`` block parsed into its essentials.

    The ``amount`` is the block's ``Net amount`` line (signed as printed,
    inclusive of any in-block ``Costs``). For internal-transfer documents
    the ``currency`` is the *destination* currency on the second leg —
    Pictet performs the FX inside the block and prints ``Net amount`` in
    the post-conversion currency.
    """

    portfolio_account: str  # e.g. "P-999999.999"
    currency: str  # ISO 4217 from the Net amount line
    amount: Decimal  # signed
    iban_raw: str | None  # captured before validation, may be None


_LEG_IBAN_RE = re.compile(r"\bIBAN\?([A-Z]{2}\d[A-Z0-9]+)")


def find_cash_effect_legs(
    text: str, labels: PictetLabels = EN_LABELS
) -> list[CashEffectLeg]:
    """Walk every CASH EFFECT block in the document and return one leg per
    block, in document order.

    Single-leg advices return one leg; multi-leg advices return two (or
    more, if Pictet ever ships a doc with more than that — none in the
    current fixture set). Returns an empty list when the document carries
    no CASH EFFECT markers for the given locale.

    Blocks that don't carry the expected portfolio-marker / Net-amount pair
    are skipped silently — that shape is the helper's contract; if a
    fixture violates it the missing leg surfaces as a transaction count
    mismatch in the calling template's test.
    """

    net_amount_re = re.compile(
        rf"^{re.escape(labels.net_amount)}\s+([A-Z]{{3}})\s+({_NUMBER})\s*$", re.M
    )

    legs: list[CashEffectLeg] = []
    segments = text.split(labels.cash_effect_marker)
    for segment in segments[1:]:  # segments[0] is the document header
        portfolio_match = labels.portfolio_in_re.search(segment)
        net_match = net_amount_re.search(segment)
        if portfolio_match is None or net_match is None:
            continue
        iban_match = _LEG_IBAN_RE.search(segment)
        legs.append(
            CashEffectLeg(
                portfolio_account=portfolio_match.group(1),
                currency=net_match.group(1),
                amount=parse_pictet_amount(net_match.group(2)),
                iban_raw=iban_match.group(1) if iban_match else None,
            )
        )
    return legs


def legs_to_transactions(
    legs: list[CashEffectLeg],
    *,
    doc: RawDocument,
    trade_date: date,
    settlement_date: date | None,
    narration: str,
) -> list[Transaction]:
    """Render a list of ``CashEffectLeg`` as ``Transaction`` objects with a
    shared narration, one Transaction per leg.

    Account number resolution mirrors :func:`resolve_account_number`: prefer
    the leg's own validated IBAN (each leg is a different per-currency
    sub-account), fall back to the parent portfolio identifier when the
    IBAN won't validate (anonymised fixtures, mostly).
    """

    truncated = narration[:140]
    transactions: list[Transaction] = []
    for leg in legs:
        validated = (
            normalise_iban(leg.iban_raw) if leg.iban_raw is not None else None
        )
        account_number = validated or leg.portfolio_account
        transactions.append(
            Transaction(
                trade_date=trade_date,
                settlement_date=settlement_date,
                narration=truncated,
                currency=leg.currency,
                amount=leg.amount,
                isin=None,
                quantity=None,
                price=None,
                account_number=account_number,
                source_path=doc.path,
            )
        )
    return transactions


def extract_simple_trade_advice(
    doc: RawDocument,
    *,
    expected_operations: tuple[str, ...] | None = None,
    fallback_narration: str = "Pictet trade",
    labels: PictetLabels = EN_LABELS,
    title: str | None = None,
) -> Transaction | None:
    """Parse a single-leg Pictet trade advice into one ``Transaction``.

    Covers the family of advices that share an identical field layout:
    fund subscriptions, fund redemptions, structured-product buys, ETF
    buys (Pictet emits all four under the same skeleton — only the
    Operation type label and the asset-type metadata differ). Works for
    both English and Spanish locales via the ``labels`` parameter; the
    Spanish-locale fund subscriptions / stock purchases additionally
    carry an FX leg inside the cash-effect block, which surfaces as a
    ``Net amount`` in the destination currency rather than the trade
    currency. The helper preserves whichever currency the document
    prints, so downstream code sees the actual cash-impact currency.

    Pictet's sign conventions are preserved as printed: cash-out legs carry
    a negative ``amount`` and positive ``quantity``; cash-in legs invert
    both. Downstream code does not need to look at ``Operation type`` to
    decide signs.

    ``expected_operations`` is defence-in-depth. If the document's
    ``Operation type`` line doesn't match the values the caller declared,
    the helper returns ``None`` rather than emitting a transaction with
    fields whose meanings depend on the operation type. This guards against
    classifier mis-routes — better an empty result that surfaces a warning
    than a buy booked as a sell.

    FX advice fields
    ----------------
    Pictet bills FX trades with the gross amount and per-leg costs in the
    security currency, an explicit ``Subtotal`` in the security currency,
    an FX rate, and the converted ``Net amount`` in the cash-account
    currency. The helper populates ``security_currency``, ``fees`` /
    ``fees_currency``, ``subtotal_security``, and ``exchange_rate`` when
    those lines are present so the writer can emit a proper beancount
    ``@@`` annotation; on non-FX trades those fields stay ``None``.

    ``title`` is the per-doctype document title (``"Suscripción"``,
    ``"Trade confirmation"``, etc.). Passed through to ``Transaction.title``
    for use as the first beancount narration string. Optional — when
    omitted the writer falls back to a single-string narration.
    """

    text = doc.text

    if expected_operations is not None:
        # Try each known label for the operation-type field in turn — see
        # ``PictetLabels.operation_type`` for why this is a tuple. First hit
        # wins; if none match, treat it the same as a missing field.
        op: str | None = None
        for label in labels.operation_type:
            op = find_field(text, label)
            if op is not None:
                break
        if op is None or op not in expected_operations:
            return None

    trade_date_raw = find_field(text, labels.trade_date)
    if not trade_date_raw:
        return None

    cash_effect = find_amount_field(text, labels.net_amount)
    if cash_effect is None:
        return None
    currency, amount = cash_effect

    value_date_raw = find_field(text, labels.value_date)
    booking_date_raw = find_field(text, labels.booking_date)
    quantity_raw = find_field(text, labels.executed_quantity)
    price_match = find_amount_field(text, labels.execution_price)

    # FX-bridge / fees fields. ``find_amount_field`` returns ``None`` when
    # the line is absent, so on non-FX advices these all stay ``None``
    # (except ``costs_match``, which fires whenever there's any per-leg
    # cost line at all — including ``Costs EUR 0.00`` non-FX advices).
    costs_match = find_amount_field(text, labels.costs)
    subtotal_match = find_amount_field(text, labels.subtotal)
    exchange_rate = find_exchange_rate(text, labels)
    # Per-line fee breakdown (``Corretaje y/o spread`` + ``Tasa bursátil``
    # for stock trades, ``Spread`` alone for fund subscriptions/redemptions).
    # Empty when the document carries an inline ``Costes <CCY> <amount>``
    # line but no standalone ``Costes`` block above it; the writer falls
    # back to a single aggregate fees leg in that case.
    fee_breakdown = find_fee_breakdown(
        text, costs_label=labels.costs, total_label="Total"
    )

    narration = (find_headline(text, labels) or fallback_narration)[:140]

    return Transaction(
        trade_date=parse_pictet_date(trade_date_raw),
        settlement_date=(
            parse_pictet_date(value_date_raw) if value_date_raw else None
        ),
        booking_date=(
            parse_pictet_date(booking_date_raw) if booking_date_raw else None
        ),
        narration=narration,
        title=title,
        currency=currency,
        amount=amount,
        isin=resolve_isin(text),
        quantity=parse_pictet_amount(quantity_raw) if quantity_raw else None,
        price=price_match[1] if price_match else None,
        # The price line carries the security's quotation currency
        # (``Execution price USD 113.2718``) — the asset's trade-execution
        # currency, distinct from ``currency`` which is the cash-account
        # currency. On non-FX trades the two are equal.
        security_currency=price_match[0] if price_match else None,
        fees=costs_match[1] if costs_match else None,
        fees_currency=costs_match[0] if costs_match else None,
        fee_breakdown=fee_breakdown,
        subtotal_security=subtotal_match[1] if subtotal_match else None,
        exchange_rate=exchange_rate,
        account_number=resolve_account_number(text, labels),
        transaction_number=find_transaction_number(text, labels),
        source_path=doc.path,
    )

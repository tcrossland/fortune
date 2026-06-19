"""Balance-assertion extractor for Pictet monthly statements.

Pictet's ``Portfolio valuation`` page lists every position's quantity
and every cash sub-account's balance as of the statement's ``As at``
date. Wiring those into beancount ``balance`` directives gives
``bean-check`` an enforceable inventory checkpoint at month-end —
the moment the running ledger drifts from Pictet's records (a missed
ingest, an extraction bug, a writer regression), the next load
fails with the source statement date in the error message.

Assertion date convention
-------------------------
Beancount evaluates ``balance`` directives at the *beginning* of the
asserted date. Pictet's ``As at <day> <Month> <year>`` is the *end*
of that day. We add one day to convert: an "as at 31 December 2025"
statement emits assertions dated 2026-01-01.

Output shape
------------
For each holding row::

    2026-01-01 balance Assets:Pic:K123456001:LU2601001147  2248.13866 LU2601001147

For each cash row::

    2026-01-01 balance Assets:Pic:K123456001:GBP  57909.10 GBP

The portfolio segment comes from the statement header's ``Account
no.:`` / ``N° de cuenta:`` line, sanitised via the same dash-and-
period strip the writer uses on trade advices (so ``K-123456.001``
→ ``K123456001``).

Locale handling
---------------
The ``As at`` anchor accepts both English (``As at 31 December 2025``)
and Spanish (``al 31 Enero 2026``) date strings; the parser returns
``[]`` on the fully-anonymised ``99 Enero 9999`` form. The Spanish
``ESTADO FINANCIERO`` (Madrid account) statement differs in two further
ways the parser handles: the portfolio number prints **bare** on its own
line (``K-NNNNNN.NNN``) rather than behind an ``Account no.`` /
``N° de cuenta`` label, and its cash row leads with the currency
(``<CCY> <bal> <name> <bal> <%>``) instead of the English
``<bal> <name> <CCY> <bal>``. Security rows (quantity-led + ``ISIN:``
next) are layout-agnostic and need no locale handling.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from banking_pipeline.prices_extract import _parse_statement_date
from banking_pipeline.vanguard_statement import parse_isa_valuation
from banking_pipeline.writer.format import portfolio_segment
from banking_pipeline.writer.profile import VANGUARD_PROFILE

_AS_AT_RE = re.compile(r"\b(?:As\s+at|al)\s+(\d{1,2}\s+\w+\s+\d{4})", re.I)
_ACCOUNT_NO_RE = re.compile(
    r"^(?:Account\s+no\.|N°\s*de\s+cuenta)\s*:\s*([A-Z]-\d{6}\.\d{3})",
    re.M,
)
# Fallback for the Spanish statement, which prints the portfolio number on
# its own line (``K-NNNNNN.NNN``) rather than behind an ``Account no.:``
# label. Anchored to a whole line so the sub-account forms in the
# current-account table (``K-NNNNNN.NNN.00.EUR``) can't match.
_ACCOUNT_BARE_RE = re.compile(r"^([A-Z]-\d{6}\.\d{3})\s*$", re.M)
_ISIN_LINE_RE = re.compile(
    r"\bISIN(?:/Internal\s+ref\.)?\s*:\s*"
    r"([A-Z]{2}[A-Z0-9]{8}(?:[A-Z0-9]{2}|\s[A-Z0-9]))"
)
# Quantity-led row preceding an ISIN block: ``<quantity> <description>``.
# Matched permissively — the ISIN-line scan a few lines later confirms
# this was an actual security row.
_QUANTITY_ROW_RE = re.compile(
    r"^([-\d'.]+)\s+(\S.*)$"
)
# Cash-currency row: ``<balance> <Currency Name> <CCY> <balance>``,
# where the two balance amounts repeat. The currency name is one or
# more capitalised words. The character class spans accented Latin
# letters (``À-ÿ``) as well as ASCII because the Spanish statement
# localises the names (``Dólar USA``, ``Yen Japón``) — an ASCII-only
# class silently failed to match those rows and dropped that currency's
# cash from the valuation entirely.
_CASH_ROW_RE = re.compile(
    r"^([-\d'.]+)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]+?)\s+([A-Z]{3})\s+([-\d'.]+)\s*$"
)
# Spanish cash row: ``<CCY> <balance> <Currency Name> <balance> <%>`` — the
# currency leads, the two balances repeat, and a trailing weight column the
# English layout doesn't have. Requires a digit in each balance so a
# dash-only zero row (``USD - Dólar USA - -``) can't match.
_ES_CASH_ROW_RE = re.compile(
    r"^([A-Z]{3})\s+(-?[\d'][\d'.]*)\s+([A-Za-zÀ-ÿ][A-Za-zÀ-ÿ\s]+?)\s+"
    r"(-?[\d'][\d'.]*)\s+[-\d'.]+\s*$"
)


def _sanitise_portfolio(account_no: str) -> str:
    """Match the writer's ``_portfolio_segment``: strip the dash and
    period from ``K-123456.001`` to get a beancount-compatible
    segment ``K123456001``."""

    return account_no.replace("-", "").replace(".", "")


def _normalise_amount(s: str) -> str:
    return s.replace("'", "")


def extract_balances_from_statement(
    text: str,
) -> list[tuple[str, str, str, str]]:
    """Parse a statement text dump for per-holding / per-currency balances.

    Returns ``(date, account, quantity, commodity)`` 4-tuples ready to
    format as ``<date> balance <account> <quantity> <commodity>``
    directives. The date is one day after the statement's valuation
    anchor (beancount's beginning-of-day evaluation convention).

    Dispatches by bank: the Pictet monthly-statement parser and the
    Vanguard ISA regular-statement parser each run and no-op on the
    other issuer's text (Pictet keys on its ``As at`` / ``K-NNNNNN.NNN``
    markers; Vanguard on its ``Your ISA investments at`` table), so the
    union is safe to return without sniffing the issuer first.

    Returns ``[]`` when neither parser recognises the document (e.g. the
    fixture's anonymised ``99 Enero 9999`` form, or a drained Vanguard
    statement with no valuation table).
    """

    return _pictet_balances(text) + _vanguard_balances(text)


def _vanguard_balances(text: str) -> list[tuple[str, str, str, str]]:
    """Vanguard ISA balance assertions from the valuation snapshot.

    Emits a cash-balance assertion (when the statement prints a ``Cash
    account`` row) and one assertion per **non-zero** holding. Wound-down
    positions (movement-pair rows netting to zero) are skipped — asserting
    a 0-unit balance is noise, and the ticker accounts are never closed
    (the writer only closes ISIN-shaped commodities), so there's nothing
    to confirm. Accounts mirror the writer's ``Assets:Vgd:ISA:<acct>:…``
    layout exactly so the assertions line up with the ingested postings.
    """

    valuation = parse_isa_valuation(text)
    if valuation is None:
        return []

    assertion_date = (valuation.statement_date + timedelta(days=1)).isoformat()
    prefix = VANGUARD_PROFILE.account_prefix
    portfolio = portfolio_segment(valuation.account_number)
    rows: list[tuple[str, str, str, str]] = []

    if valuation.cash_balance is not None:
        rows.append(
            (
                assertion_date,
                f"Assets:{prefix}:{portfolio}:GBP",
                str(valuation.cash_balance),
                "GBP",
            )
        )
    for holding in valuation.holdings:
        if holding.quantity == 0:
            continue
        rows.append(
            (
                assertion_date,
                f"Assets:{prefix}:{portfolio}:{holding.ticker}",
                str(holding.quantity),
                holding.ticker,
            )
        )
    return rows


def _pictet_balances(text: str) -> list[tuple[str, str, str, str]]:
    """Per-holding / per-currency balances from a Pictet monthly statement.

    Returns ``[]`` when the statement's date or account header can't be
    parsed (e.g. the fixture's anonymised ``99 Enero 9999`` /
    ``K-999999.999`` forms, or a non-Pictet document).
    """

    date_match = _AS_AT_RE.search(text)
    if date_match is None:
        return []
    statement_date = _parse_statement_date(date_match.group(1))
    if statement_date is None:
        return []
    assertion_date = (statement_date + timedelta(days=1)).isoformat()

    acct_match = _ACCOUNT_NO_RE.search(text) or _ACCOUNT_BARE_RE.search(text)
    if acct_match is None:
        return []
    portfolio = _sanitise_portfolio(acct_match.group(1))

    rows: list[tuple[str, str, str, str]] = []
    seen_isins: set[str] = set()
    seen_currencies: set[str] = set()
    lines = text.splitlines()

    for i, line in enumerate(lines):
        stripped = line.strip()

        # --- Cash row ---
        # EN ``<bal> <Currency-Name> <CCY> <bal>`` or ES ``<CCY> <bal>
        # <Currency-Name> <bal> <%>`` — both repeat the balance (symmetry
        # guard against layout drift). Checked *before* the security row
        # because an EN cash row also begins with a number and would
        # otherwise be caught by the quantity-led pattern.
        cash: tuple[str, str, str] | None = None  # (ccy, bal1, bal2)
        m_cash = _CASH_ROW_RE.match(stripped)
        if m_cash is not None:
            bal1, _name, ccy, bal2 = m_cash.groups()
            cash = (ccy, bal1, bal2)
        else:
            m_es_cash = _ES_CASH_ROW_RE.match(stripped)
            if m_es_cash is not None:
                ccy, bal1, _name, bal2 = m_es_cash.groups()
                cash = (ccy, bal1, bal2)
        if cash is not None:
            ccy, bal1, bal2 = cash
            n1, n2 = _normalise_amount(bal1), _normalise_amount(bal2)
            # The two balance columns should agree — a symmetry guard
            # against layout drift catching a non-cash row. But the Spanish
            # ``ESTADO FINANCIERO`` rounds the portfolio-currency *display*
            # column to whole units (``31'673``) while the balance column
            # keeps cents (``31'673.01``), so an exact-equality guard
            # silently dropped that currency's cash whenever the display
            # column rounded. Compare numerically with a half-unit
            # tolerance instead (the max round-to-nearest-integer error),
            # which still rejects a genuinely mismatched cross-currency row.
            if abs(Decimal(n1) - Decimal(n2)) <= Decimal("0.5"):
                if ccy not in seen_currencies:
                    seen_currencies.add(ccy)
                    # Keep the more precise column (the one carrying cents)
                    # so the assertion matches the ledger to the cent.
                    balance = n2 if "." in n2 else n1
                    # Skip zero-balance currencies. A statement lists a
                    # residual ``0.00`` line for currencies the account
                    # briefly held; the ledger never opens that
                    # sub-account, so asserting ``0`` against it trips
                    # bean-check's "Invalid reference to inactive
                    # account". A zero assertion is low-value anyway.
                    if Decimal(balance) != 0:
                        rows.append(
                            (
                                assertion_date,
                                f"Assets:Pic:{portfolio}:{ccy}",
                                balance,
                                ccy,
                            )
                        )
                continue

        # --- Security row ---
        # A quantity-led row whose next line carries an ``ISIN:``
        # marker is a per-position balance entry. We scan up to ~3
        # lines forward to tolerate the occasional blank between the
        # quantity row and the ISIN line.
        m_qty = _QUANTITY_ROW_RE.match(stripped)
        if m_qty is not None:
            # The newer statement layout concatenates the quantity row and
            # the ISIN marker onto a single line, joined by a stray control
            # character (``1'743.00 Eleva… <0xFFFE> ISIN: LU…``). The
            # forward scan below only looks at *following* lines, so check
            # this line first — otherwise the holding is silently dropped.
            m_isin_same = _ISIN_LINE_RE.search(stripped)
            if m_isin_same is not None:
                isin = m_isin_same.group(1).replace(" ", "")
                if isin not in seen_isins:
                    seen_isins.add(isin)
                    rows.append(
                        (
                            assertion_date,
                            f"Assets:Pic:{portfolio}:{isin}",
                            _normalise_amount(m_qty.group(1)),
                            isin,
                        )
                    )
                continue
            for j in range(i + 1, min(i + 4, len(lines))):
                # A later quantity-led row means a *new* holding has
                # started before we found an ISIN — so the ISIN that
                # follows belongs to it, not to this row. Stop scanning
                # so a non-holding numeric line that merely precedes a
                # real holding (e.g. a ``2'400'000.00 C/A Limit Gbp …``
                # lombard credit-limit row on the P portfolio's
                # valuation page) can't annex the next holding's ISIN
                # and assert a bogus quantity against it.
                if _QUANTITY_ROW_RE.match(lines[j].strip()):
                    break
                m_isin = _ISIN_LINE_RE.search(lines[j])
                if m_isin is None:
                    continue
                isin = m_isin.group(1).replace(" ", "")
                if isin in seen_isins:
                    break
                seen_isins.add(isin)
                quantity = _normalise_amount(m_qty.group(1))
                rows.append(
                    (
                        assertion_date,
                        f"Assets:Pic:{portfolio}:{isin}",
                        quantity,
                        isin,
                    )
                )
                break

    return rows


# --- Coverage guard -------------------------------------------------------
# A deliberately permissive *re-detector* run alongside the production
# parser to catch rows it silently drops. The two cash bugs this guards
# against — a whole-unit-rounded display column and an accented currency
# name (``Dólar USA``) — both made a real cash row fail the strict parser
# and vanish from the valuation, understating net cash with no error. The
# guard re-finds cash rows and ISIN markers with looser patterns (Unicode
# name class, balance-equality tolerance) and reports any the real parser
# missed, so a future tightening of the parser surfaces as a coverage gap
# rather than a silent shortfall. It is FX-free (a pure presence check, no
# valuation), so unlike a value reconciliation it can't be fooled by the
# HMRC-monthly-vs-statement-spot FX drift that legitimately moves a
# multi-currency book's GBP total by ~10%.

# Permissive cash-row re-detectors: the name is any run of non-digits
# (so accents pass), and the row must end right after the second balance
# (``$``-anchored) so a security price row — which carries further
# columns — can't match.
_LOOSE_EN_CASH_RE = re.compile(r"^([-\d'.]+)\s+\D+?\s+([A-Z]{3})\s+([-\d'.]+)\s*$")
_LOOSE_ES_CASH_RE = re.compile(
    r"^([A-Z]{3})\s+(-?[\d'][\d'.]*)\s+\D+?\s+(-?[\d'][\d'.]*)\s+[-\d'.]+\s*$"
)


@dataclass(frozen=True)
class CoverageGap:
    """A holding or cash row present in the statement text that the
    production parser did not extract."""

    kind: str  # "cash" | "security"
    detail: str  # currency code (cash) or ISIN (security)


def statement_coverage_gaps(text: str) -> list[CoverageGap]:
    """Re-scan a statement and report rows the production parser dropped.

    Returns ``[]`` for a document the parser doesn't recognise at all
    (nothing extracted → nothing to reconcile against), so it never
    fires on the Vanguard path or an anonymised fixture. Otherwise it
    compares the set of non-zero cash currencies and ISINs visible in
    the text against what :func:`extract_balances_from_statement`
    captured, and returns one gap per missed row.
    """

    extracted = extract_balances_from_statement(text)
    if not extracted:
        return []
    def _is_ccy(code: str) -> bool:
        return len(code) == 3 and code.isalpha()

    extracted_cash = {c for _d, _a, _q, c in extracted if _is_ccy(c)}
    extracted_isins = {c for _d, _a, _q, c in extracted if not _is_ccy(c)}

    gaps: list[CoverageGap] = []

    seen_cash: set[str] = set()
    for line in text.splitlines():
        stripped = line.strip()
        detected: tuple[str, str, str] | None = None  # (ccy, bal1, bal2)
        m_en = _LOOSE_EN_CASH_RE.match(stripped)
        if m_en is not None:
            bal1, ccy, bal2 = m_en.groups()
            detected = (ccy, bal1, bal2)
        else:
            m_es = _LOOSE_ES_CASH_RE.match(stripped)
            if m_es is not None:
                ccy, bal1, bal2 = m_es.groups()
                detected = (ccy, bal1, bal2)
        if detected is None:
            continue
        ccy, bal1, bal2 = detected
        if ccy in seen_cash:
            continue
        a, b = Decimal(_normalise_amount(bal1)), Decimal(_normalise_amount(bal2))
        # The doubled balances should agree (a whole-unit-rounded display
        # column differs by < 1); a line where they diverge isn't a cash
        # row, so it doesn't count as one the parser ought to have caught.
        if abs(a - b) > max(Decimal("0.5"), abs(a) / 100):
            continue
        seen_cash.add(ccy)
        if a != 0 and ccy not in extracted_cash:
            gaps.append(CoverageGap("cash", ccy))

    seen_isins: set[str] = set()
    for m in _ISIN_LINE_RE.finditer(text):
        isin = m.group(1).replace(" ", "")
        if isin in seen_isins:
            continue
        seen_isins.add(isin)
        if isin not in extracted_isins:
            gaps.append(CoverageGap("security", isin))

    return gaps


def coverage_report(
    statement_files: Iterable[Path],
) -> list[tuple[Path, list[CoverageGap]]]:
    """Run :func:`statement_coverage_gaps` over each statement file and
    return one ``(path, gaps)`` entry per file that has at least one gap.

    The reconciliation guard behind ``balances --strict``: a holding or
    cash row visible in a statement that the parser failed to extract
    would otherwise vanish from the valuation with no error (the two cash
    bugs and the concatenated-ISIN bug all did exactly that). An
    unreadable file is reported as a gap rather than crashing the run.
    """

    out: list[tuple[Path, list[CoverageGap]]] = []
    for path in statement_files:
        try:
            if path.suffix.lower() == ".txt":
                text = path.read_text(encoding="utf-8")
            else:
                from banking_pipeline.extractors import load_pdf

                text = load_pdf(path).text
        except Exception as exc:  # noqa: BLE001 — report, don't abort the batch
            out.append((path, [CoverageGap("unreadable", str(exc))]))
            continue
        gaps = statement_coverage_gaps(text)
        if gaps:
            out.append((path, gaps))
    return out


def merge_balances(
    *balance_lists: Iterable[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Merge multiple balance-tuple lists into a deduplicated, sorted
    sequence. Last-occurrence-wins on duplicate ``(date, account)``
    pairs."""

    seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for balance_list in balance_lists:
        for row in balance_list:
            seen[(row[0], row[1])] = row
    return sorted(seen.values())


_DEFAULT_HEADER = (
    ";; Balance assertions extracted from Pictet monthly statements.\n"
    ";;\n"
    ";; One ``<date> balance <account> <quantity> <commodity>``\n"
    ";; directive per holding and per cash sub-account, sourced from\n"
    ";; the ``Portfolio valuation`` page on each statement. Assertion\n"
    ";; dates are one day after the statement's ``As at`` anchor —\n"
    ";; beancount evaluates balance directives at the beginning of\n"
    ";; the asserted date, so end-of-2025 maps to 2026-01-01.\n"
    ";;\n"
    ";; Regenerate via ``banking-pipeline balances data/`` after\n"
    ";; adding new statements. ``portfolio.beancount`` should\n"
    ";; ``include`` this file so ``bean-check`` runs the assertions\n"
    ";; on every load.\n"
)


def render(
    rows: Iterable[tuple[str, str, str, str]],
    header: str = _DEFAULT_HEADER,
) -> str:
    """Format the extracted balance tuples as a beancount file body."""

    lines = [header.rstrip("\n"), ""]
    for assertion_date, account, quantity, commodity in rows:
        # A statement that rounds cash to whole units (the Spanish locale
        # prints ``EUR 10'080``) can't be asserted to the cent against the
        # precise ledger under the tight ``EUR:0.005`` default tolerance, so
        # a whole-number *fiat* balance carries an explicit ``~ 0.5``
        # rounding tolerance. Security quantities (ISIN / ticker commodities)
        # and cent-precise cash assert exactly. The padding between account
        # and quantity is cosmetic — beancount ignores the spacing.
        tol = (
            " ~ 0.5"
            if len(commodity) == 3 and commodity.isalpha() and "." not in quantity
            else ""
        )
        lines.append(
            f"{assertion_date} balance {account}  {quantity}{tol} {commodity}"
        )
    lines.append("")
    return "\n".join(lines)


def generate(
    data_dir: Path,
    statement_files: Iterable[Path],
    output: Path | None = None,
) -> tuple[Path, int]:
    """Parse the supplied statement files and write a beancount
    balance-assertions file under ``data_dir``. Returns
    ``(output_path, row_count)``.

    The output file is overwritten on every run; aggregating across
    multiple invocations means caller passes the full statement set
    each time. Statements that don't parse (anonymised dates etc.)
    contribute zero rows silently.
    """

    if output is None:
        output = data_dir / "balances.beancount"

    rows: list[tuple[str, str, str, str]] = []
    for path in statement_files:
        if path.suffix.lower() == ".txt":
            text = path.read_text(encoding="utf-8")
        else:
            from banking_pipeline.extractors import load_pdf
            text = load_pdf(path).text
        rows.extend(extract_balances_from_statement(text))

    rows = merge_balances(rows)
    output.write_text(render(rows), encoding="utf-8")
    return output, len(rows)

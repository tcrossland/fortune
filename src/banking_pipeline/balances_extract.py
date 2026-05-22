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
Same as :mod:`prices_extract` — the ``As at`` anchor accepts both
English (``As at 31 December 2025``) and Spanish (``al 31 Enero
2026``) date strings; the parser returns ``[]`` on the
fully-anonymised ``99 Enero 9999`` form. The header label is also
locale-specific (``Account no.`` / ``N° de cuenta``).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import timedelta
from pathlib import Path

from banking_pipeline.prices_extract import _parse_statement_date

_AS_AT_RE = re.compile(r"\b(?:As\s+at|al)\s+(\d{1,2}\s+\w+\s+\d{4})", re.I)
_ACCOUNT_NO_RE = re.compile(
    r"^(?:Account\s+no\.|N°\s*de\s+cuenta)\s*:\s*([A-Z]-\d{6}\.\d{3})",
    re.M,
)
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
# more capitalised words (``Pound United Kingdom``, ``Euro``, ``Yen
# Japan``, ``Dollar USA``, ``Franc Switzerland``, etc.).
_CASH_ROW_RE = re.compile(
    r"^([-\d'.]+)\s+([A-Z][A-Za-z\s]+?)\s+([A-Z]{3})\s+([-\d'.]+)\s*$"
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
    """Parse a monthly-statement text dump for per-holding /
    per-currency balances.

    Returns ``(date, account, quantity, commodity)`` 4-tuples ready
    to format as ``<date> balance <account> <quantity> <commodity>``
    directives. The date is one day after the statement's ``As at``
    anchor (beancount's beginning-of-day evaluation convention).

    Returns ``[]`` when the statement's date or account header can't
    be parsed (e.g. the fixture's anonymised ``99 Enero 9999`` /
    ``K-999999.999`` forms).
    """

    date_match = _AS_AT_RE.search(text)
    if date_match is None:
        return []
    statement_date = _parse_statement_date(date_match.group(1))
    if statement_date is None:
        return []
    assertion_date = (statement_date + timedelta(days=1)).isoformat()

    acct_match = _ACCOUNT_NO_RE.search(text)
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
        # ``<bal> <Currency-Name> <CCY> <bal>`` — both balance amounts
        # repeat across the line. Checked *before* the security row
        # because cash rows also begin with a number and would
        # otherwise be caught by the quantity-led pattern. The
        # symmetry check (bal1 == bal2) guards against layout drift.
        m_cash = _CASH_ROW_RE.match(stripped)
        if m_cash is not None:
            bal1, _name, ccy, bal2 = m_cash.groups()
            if _normalise_amount(bal1) == _normalise_amount(bal2):
                if ccy not in seen_currencies:
                    seen_currencies.add(ccy)
                    rows.append(
                        (
                            assertion_date,
                            f"Assets:Pic:{portfolio}:{ccy}",
                            _normalise_amount(bal1),
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
            for j in range(i + 1, min(i + 4, len(lines))):
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
        # Leave a tab-like padding between account and quantity so
        # the output is human-readable; the exact spacing isn't
        # significant to beancount's parser.
        lines.append(
            f"{assertion_date} balance {account}  {quantity} {commodity}"
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

"""Price-directive extractor.

Reads beancount files (the per-year ingest output) and emits
``<date> price <commodity> <amount> <currency>`` directives for every
security trade — one per ``(date, commodity, currency)`` tuple. The
price comes from each entry's existing inventory annotations:

  - Buys carry ``{<price> <ccy>}`` cost-basis braces on the asset
    leg (``Assets:Pic:K123456001:DK0062498333  200 DK0062498333 {460.17475 DKK}``).
    The price inside the braces is the per-unit acquisition price.
  - Sells carry ``{} @ <price> <ccy>`` market-price annotations on
    the asset leg (``Assets:Pic:K123456001:IE00B579F325  -119 IE00B579F325 {} @ 178.6699 USD``).
    The price after ``@`` is the per-unit sale price.

Both shapes give the same data — per-unit price in the security
currency at the trade date — so the extractor matches either form
and feeds beancount's price database. Downstream this is what lets
Fava roll up the security holdings into the operating-currency
(GBP) net-worth view.

When the same ``(date, ISIN)`` pair appears multiple times (rare but
possible: two trades same day at different prices), the last
occurrence wins. Deduplication is otherwise trivial — most ISINs
trade infrequently enough that one price per trade-date per ISIN
covers the dataset.

The helper deliberately ignores the ``Income:<...>:Realized`` /
``:Unrealized`` legs and the cash legs; it only reads the asset leg
carrying the inventory annotation. That keeps the extractor immune
to changes in fee/realised-gain account naming as the writer
evolves — only the cost-basis brace shape needs to stay stable.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date
from pathlib import Path

# English and Spanish month names, lowercased for case-insensitive
# lookup. Both locales' Pictet statements anchor the valuation date as
# ``<day> <Month> <year>``; the locale-specific month name is the only
# difference between them.
_MONTHS = {
    # English
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    # Spanish
    "enero": 1, "febrero": 2, "marzo": 3, "abril": 4,
    "mayo": 5, "junio": 6, "julio": 7, "agosto": 8,
    "septiembre": 9, "octubre": 10, "noviembre": 11, "diciembre": 12,
    # Plus a couple of common abbreviated/accent variants Pictet has
    # printed historically.
    "setiembre": 9,
}


def _parse_statement_date(s: str) -> date | None:
    """Parse a ``<day> <month_name> <year>`` string in either English
    or Spanish. Returns ``None`` if the string can't be matched
    (e.g., the fixture's anonymised ``99 Enero 9999`` form)."""

    m = re.match(r"^\s*(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóú]+)\s+(\d{4})\s*$", s)
    if m is None:
        return None
    day = int(m.group(1))
    month_name = m.group(2).lower()
    year = int(m.group(3))
    month = _MONTHS.get(month_name)
    if month is None:
        return None
    try:
        return date(year, month, day)
    except ValueError:
        return None

# Posting line carrying inventory annotation. Captures:
#   1. commodity (the ISIN-shaped token after the quantity)
#   2. price (inside ``{}`` for buys, or after ``@`` for sells)
#   3. price currency (3-letter code immediately after the price)
#
# Two annotation shapes are accepted on the same line:
#   - ``{<price> <ccy>}`` — buys (literal cost basis)
#   - ``{} @ <price> <ccy>`` — sells (reduce-from-inventory + market price)
#
# Anchored to a posting line (leading whitespace + ``Assets:`` family);
# the commodity is asserted ISIN-shaped to skip cash-leg postings
# (which carry a 3-letter currency rather than a 12-char ISIN).
_PRICED_POSTING_RE = re.compile(
    r"^\s+Assets:[^\s]+\s+-?[\d.']+\s+"
    r"([A-Z]{2}[A-Z0-9]{8}[A-Z0-9]{0,2})"
    r"\s+(?:"
    r"\{(?:\s*([\d.']+)\s+([A-Z]{3})\s*)?\}(?:\s*@\s*([\d.']+)\s+([A-Z]{3}))?"
    r")"
)
_TXN_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+\*")


def _normalise_amount(s: str) -> str:
    """Strip Swiss apostrophe thousand-separators (Pictet's input
    format; the writer doesn't emit them but the helper stays
    permissive in case a future ingest pass does)."""
    return s.replace("'", "")


def extract_prices(files: Iterable[Path]) -> list[tuple[str, str, str, str]]:
    """Walk the supplied beancount files and return a list of
    ``(date, commodity, price, currency)`` 4-tuples — one per unique
    (date, commodity) pair seen, with the last occurrence winning.

    Files are read in order so a deterministic ``last-wins`` rule
    means the user's per-year files passed last (most recent year)
    take precedence on accidental same-date duplicates.
    """

    seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for path in files:
        current_date: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            m_date = _TXN_DATE_RE.match(line)
            if m_date:
                current_date = m_date.group(1)
                continue
            if current_date is None:
                continue
            m_post = _PRICED_POSTING_RE.match(line)
            if not m_post:
                continue
            commodity = m_post.group(1)
            # Price/currency from either the buy form (groups 2/3) or
            # the sell form (groups 4/5). One of the two shapes must
            # have populated; if both are empty the posting has an
            # empty ``{}`` with no ``@`` — skip it (no price datum).
            price = m_post.group(2) or m_post.group(4)
            currency = m_post.group(3) or m_post.group(5)
            if price is None or currency is None:
                continue
            seen[(current_date, commodity)] = (
                current_date,
                commodity,
                _normalise_amount(price),
                currency,
            )
    return sorted(seen.values())


_DEFAULT_HEADER = (
    ";; Price directives extracted from per-trade inventory annotations.\n"
    ";;\n"
    ";; One ``<date> price <commodity> <price> <ccy>`` directive per\n"
    ";; unique (date, commodity) pair, sourced from the cost-basis\n"
    ";; braces on buys (``{<price> <ccy>}``) and the market-price\n"
    ";; annotation on sells (``@ <price> <ccy>``). When the same\n"
    ";; (date, commodity) appears more than once the file order wins\n"
    ";; (last write).\n"
    ";;\n"
    ";; Regenerate via ``banking-pipeline prices data/`` after\n"
    ";; re-running ingest. ``portfolio.beancount`` should\n"
    ";; ``include`` this file so Fava and bean-report can value\n"
    ";; holdings in the operating currency (GBP).\n"
)


def render(rows: Iterable[tuple[str, str, str, str]], header: str = _DEFAULT_HEADER) -> str:
    """Format the extracted price tuples as a beancount file body."""

    lines = [header.rstrip("\n"), ""]
    for date, commodity, price, currency in rows:
        lines.append(f"{date} price {commodity}  {price} {currency}")
    lines.append("")
    return "\n".join(lines)


def generate(
    data_dir: Path,
    output: Path | None = None,
    *,
    statement_files: Iterable[Path] = (),
) -> tuple[Path, int]:
    """Scan ``data_dir`` for ``*.beancount`` per-year files (skipping
    aggregate files like ``portfolio.beancount`` to avoid double-
    counting on aggregates that ``include`` the per-year files), then
    write the prices file. Returns ``(output_path, row_count)``.

    ``statement_files`` is an optional iterable of paths to
    Pictet monthly-statement PDFs (or pre-extracted ``.txt`` text
    dumps) that the helper parses for per-ISIN market prices and
    merges into the trade-derived prices. Statement-derived prices
    win on ``(date, ISIN)`` collisions because the statement's
    valuation is the authoritative quote for that date.
    """

    if output is None:
        output = data_dir / "prices.beancount"

    files = [
        f
        for f in sorted(data_dir.glob("*.beancount"))
        if f.resolve() != output.resolve()
        and not _is_aggregate(f)
    ]
    trade_rows = extract_prices(files)

    # Pull in statement-derived prices. The PDF extractor import is
    # deferred until a PDF actually shows up — pypdfium2 is heavy to
    # load and the trade-only path doesn't need it; ``.txt`` test
    # fixtures don't need it either.
    statement_rows: list[tuple[str, str, str, str]] = []
    for path in statement_files:
        if path.suffix.lower() == ".txt":
            text = path.read_text(encoding="utf-8")
        else:
            from banking_pipeline.extractors import load_pdf  # type: ignore[attr-defined]
            text = load_pdf(path).text
        statement_rows.extend(extract_prices_from_statement(text))

    # Statement rows passed last so they win on (date, ISIN) collisions.
    rows = merge_prices(trade_rows, statement_rows)
    output.write_text(render(rows), encoding="utf-8")
    return output, len(rows)


def _is_aggregate(path: Path) -> bool:
    """Heuristic — aggregate files start with the ``Portfolio aggregate``
    header comment and contain ``include`` directives. Per-year ingest
    output starts with a ``; source:`` comment instead.

    The check looks at the first non-empty line. Cheap and reliable
    on the project's current file layout; refine if a future file
    breaks the heuristic.
    """

    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            return stripped.startswith(";; Portfolio aggregate")
    except OSError:
        return False
    return False


# ---------------------------------------------------------------------------
# Monthly-statement parser
# ---------------------------------------------------------------------------
#
# Pictet's portfolio-valuation pages list one row per holding with the
# market price as of a single statement-wide ``As at <date>`` /
# ``Valoración de la cartera al <date>`` anchor. Layout per holding::
#
#     <quantity> <description>
#     ISIN: <isin>
#     <sec_ccy> <market_price> [<symbol>)] <sec_ccy> <gross_unit_cost>
#
# Two real-world quirks to handle:
#
#   - The line break between ``description`` and ``ISIN`` is sometimes
#     replaced by the ``⁾``-style control char so a single line
#     reads ``<quantity> <description>￾ISIN: <isin>`` — the regex
#     looks for the ``ISIN`` marker anywhere on the line, not at the
#     start.
#   - Pictet's structured-product internal refs (``ZZ00AB97OD 0``) carry
#     the same space-before-final-char artifact the trade advices use;
#     stripped via the existing :func:`find_isin`-style normalisation.
#   - The price line may carry a footnote symbol such as ``b)`` between
#     the market price and the gross unit cost; the regex makes that
#     optional.

_AS_AT_RE = re.compile(
    r"\b(?:As\s+at|al)\s+(\d{1,2}\s+\w+\s+\d{4})", re.I
)
# ``ISIN: <code>`` anywhere on the line. Captures both the standard
# 12-char form and the Pictet 11-char internal-ref form (with optional
# space before final char).
_ISIN_LINE_RE = re.compile(
    r"\bISIN(?:/Internal\s+ref\.)?\s*:\s*"
    r"([A-Z]{2}[A-Z0-9]{8}(?:[A-Z0-9]{2}|\s[A-Z0-9]))"
)
# Price line: ``<ccy> <number> [<symbol>)] <ccy> <unit_cost>``. The
# first ``<ccy> <number>`` pair is the market price; the second is
# the cost basis (which we ignore here — that's already encoded on
# every trade advice).
_PRICE_LINE_RE = re.compile(
    r"^([A-Z]{3})\s+([\d'.]+)(?:\s+\w+\))?\s+[A-Z]{3}\s+[\d'.]+"
)


def extract_prices_from_statement(text: str) -> list[tuple[str, str, str, str]]:
    """Parse a monthly-statement text dump for per-ISIN market prices.

    Returns a list of ``(date, commodity, price, currency)`` 4-tuples,
    one per ISIN found. The date is the statement's ``As at`` /
    ``al <date>`` anchor; if that can't be parsed, returns ``[]``.

    Handles both English and Spanish locales (``Financial Statement``
    and ``ESTADO FINANCIERO``) since they share the same row layout
    and only differ in the locale of the date string and the column
    headers — neither of which affect the parser.
    """

    date_match = _AS_AT_RE.search(text)
    if date_match is None:
        return []
    parsed_date = _parse_statement_date(date_match.group(1))
    if parsed_date is None:
        return []
    date_str = parsed_date.isoformat()

    rows: list[tuple[str, str, str, str]] = []
    seen_on_this_statement: set[str] = set()
    lines = text.splitlines()
    for i, line in enumerate(lines):
        m_isin = _ISIN_LINE_RE.search(line)
        if m_isin is None:
            continue
        isin = m_isin.group(1).replace(" ", "")
        if isin in seen_on_this_statement:
            # The same ISIN can repeat across pages of the statement
            # (Pictet prints subtotals + per-holding rows on each
            # page); take the first occurrence and ignore subsequent
            # ones.
            continue
        # Walk forward from the ISIN line looking for a ``<ccy>
        # <price> ...`` line. Usually the very next line; rarely
        # there's a blank or a continuation line in between, so scan
        # up to ~3 lines forward before giving up.
        for j in range(i + 1, min(i + 4, len(lines))):
            m_price = _PRICE_LINE_RE.match(lines[j].strip())
            if m_price is None:
                continue
            currency, price = m_price.group(1), m_price.group(2)
            rows.append((date_str, isin, price.replace("'", ""), currency))
            seen_on_this_statement.add(isin)
            break

    return rows


def merge_prices(
    *price_lists: Iterable[tuple[str, str, str, str]],
) -> list[tuple[str, str, str, str]]:
    """Merge multiple price-tuple lists into a deduplicated, sorted
    sequence. Last-occurrence-wins on duplicate ``(date, commodity)``
    pairs — pass statement-derived prices last when both sources
    cover the same date so the statement's authoritative valuation
    overrides any same-day trade-derived price."""

    seen: dict[tuple[str, str], tuple[str, str, str, str]] = {}
    for price_list in price_lists:
        for row in price_list:
            seen[(row[0], row[1])] = row
    return sorted(seen.values())

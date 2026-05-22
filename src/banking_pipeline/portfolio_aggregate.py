"""Portfolio-aggregate generator.

Walks a directory of per-year ``*.beancount`` ingest output and writes
a single ``portfolio.beancount`` file that:

  - declares the user's operating currency (or currencies) via
    ``option "operating_currency" "<ccy>"``,
  - emits an ``open`` directive for every account referenced by a
    posting that isn't already opened inline by the writer (the writer
    emits an inline open for first-time security buys; redeclaring
    those centrally would double-open and beancount errors on that),
  - then ``include``s the per-year files in lexicographic order.

The earliest posting that touches each account becomes the open's
date. Constraint commodity is filled in when the account's last path
segment is unambiguous — an ISO 4217 currency or an ISIN — and left
off otherwise (the elastic ``Realized`` / ``Unrealized`` / ``Other``
sub-accounts post in arbitrary currencies).

Mirrors the same scan rules as :func:`render_open_directives` in the
beancount writer, but operates on already-rendered files rather than
on in-memory ``ExtractionResult`` instances. This split exists because
``render_open_directives`` is a per-batch helper used by ``ingest``,
whereas the aggregate is the cross-year roll-up the user opens in
Fava / ``bean-check`` directly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path

from banking_pipeline.commodities_metadata import CommodityMetadata

# A posting line's account is the indented token at the start of the line.
# Account segments are letters, digits, and hyphens — beancount's grammar.
_POSTING_RE = re.compile(
    r"^\s+((?:Assets|Liabilities|Income|Expenses|Equity)(?::[A-Za-z0-9-]+)+)"
)
# Open directive at the top-level: ``<date> open <account> [<commodity>]``.
_OPEN_RE = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+open\s+"
    r"((?:Assets|Liabilities|Income|Expenses|Equity)(?::[A-Za-z0-9-]+)+)"
)
# Transaction header — anchors the "current date" we attribute postings to.
_TXN_DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\s+\*")

_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
# ISIN-shaped: 2 letters then 9–10 alphanumerics. The 11-char form covers
# Pictet's structured-product internal refs; 12-char covers real ISINs.
_ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{8}[A-Z0-9]{0,2}$")

# Posting line carrying inventory annotation — captures the ISIN (the
# commodity after the quantity) and the currency on either the
# cost-basis brace (``{<price> <ccy>}`` for buys) or the market-price
# annotation (``@ <price> <ccy>`` for sells). The ISIN's quotation
# currency is what feeds Income:<...>:<ISIN>:<...> account opens.
_PRICED_ASSET_RE = re.compile(
    r"^\s+Assets:[^\s]+\s+-?[\d.']+\s+"
    r"([A-Z]{2}[A-Z0-9]{8}[A-Z0-9]{0,2})"
    r"\s+(?:"
    r"\{(?:\s*[\d.']+\s+([A-Z]{3})\s*)?\}(?:\s*@\s*[\d.']+\s+([A-Z]{3}))?"
    r")"
)


_DEFAULT_HEADER = (
    ";; Portfolio aggregate.\n"
    ";; Generated central account opens + per-year ingest includes.\n"
    ";;\n"
    ";; Open directives are dated to the earliest posting that touches\n"
    ";; each account. Accounts already opened inline by the writer (one\n"
    ";; per first-time security buy) are not redeclared here — the\n"
    ";; inline open in the per-year file is authoritative.\n"
    ";;\n"
    ";; Constraint currency / commodity is filled in when the last\n"
    ";; path segment is an ISO 4217 currency (``…:EUR``) or an ISIN\n"
    ";; (``…:LU2096759431``). Sub-accounts whose currency varies per\n"
    ";; posting (Realized/Unrealized/Dividend, Other, Unknown) open\n"
    ";; without a constraint.\n"
)


def _constraint(
    account: str,
    isin_currencies: dict[str, str] | None = None,
) -> str | None:
    """Beancount commodity constraint for ``open <account> <ccy>``, or
    ``None`` when the account's last segment doesn't unambiguously imply
    one.

    Three resolution paths, in order:

      - **Last segment is an ISO 4217 currency** (``…:EUR``) — return
        the currency. Used for cash sub-accounts and per-currency
        Fees / Interest accounts.
      - **Last segment is an ISIN** (``…:LU2096759431``) — return the
        ISIN as the commodity constraint. Used for security-asset
        accounts (the security's commodity is itself).
      - **Account contains an ISIN segment somewhere in the middle**
        (``Income:Pic:K123456001:LU2096759431:Realized``) — look up
        the ISIN's quotation currency in ``isin_currencies`` and
        return that. The currency comes from the security's
        cost-basis brace on its trade entries (USD-quoted ETFs,
        EUR-quoted funds, etc.). Without a hit in the map, returns
        ``None``.

    Mirrors the writer's per-trade logic so reading the central open
    from this file matches what the inline opens would have emitted.
    """

    parts = account.split(":")
    last = parts[-1]
    if _CURRENCY_RE.fullmatch(last):
        return last
    if _ISIN_RE.fullmatch(last) and 11 <= len(last) <= 12:
        return last
    # Look for an ISIN segment elsewhere in the path (typically the
    # third segment in ``Income:Pic:<portfolio>:<ISIN>:<suffix>``).
    if isin_currencies:
        for segment in parts[1:-1]:
            if _ISIN_RE.fullmatch(segment) and 11 <= len(segment) <= 12:
                ccy = isin_currencies.get(segment)
                if ccy is not None:
                    return ccy
    return None


def _extract_isin_currencies(files: Sequence[Path]) -> dict[str, str]:
    """Walk the per-year files and return a ``{isin: currency}`` map
    derived from each security's first-seen trade-execution
    currency. Used by ``_constraint`` to assign currency constraints
    to per-ISIN Income accounts (Realized / Unrealized / Dividend /
    Interest / bare-ISIN). When the same ISIN appears in trades
    denominated in different currencies (rare; would mean Pictet
    re-priced it) the first occurrence in file order wins.
    """

    isin_to_ccy: dict[str, str] = {}
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _PRICED_ASSET_RE.match(line)
            if m is None:
                continue
            isin = m.group(1)
            currency = m.group(2) or m.group(3)
            if currency is None:
                continue
            isin_to_ccy.setdefault(isin, currency)
    return isin_to_ccy


# Auxiliary files the aggregate ``include``s but doesn't scan as
# transaction sources, plus the aggregate output itself — all excluded
# from the per-year source-file list.
_AUX_FILENAMES = ("prices.beancount", "balances.beancount")


def _source_files(data_dir: Path, output: Path) -> list[Path]:
    """Per-year ``*.beancount`` ingest files under ``data_dir``.

    Excludes the aggregate ``output`` (so re-running is idempotent) and
    the auxiliary price / balance files (which are ``include``d but not
    scanned as transaction sources).
    """

    return [
        f
        for f in sorted(data_dir.glob("*.beancount"))
        if f.resolve() != output.resolve() and f.name not in _AUX_FILENAMES
    ]


def discover_isins(data_dir: Path, output: Path | None = None) -> set[str]:
    """ISINs referenced by priced security postings under ``data_dir``.

    Reuses the same priced-asset scan as the constraint resolver
    (:func:`_extract_isin_currencies`) — every buy/sell carries the ISIN
    on a cost-basis or market-price annotation, so this is the set of
    securities the user has actually traded and therefore needs
    commodity metadata for.
    """

    if output is None:
        output = data_dir / "portfolio.beancount"
    files = _source_files(data_dir, output)
    return set(_extract_isin_currencies(files))


def _commodity_directives(
    in_use_isins: Iterable[str],
    commodities: Mapping[str, CommodityMetadata],
) -> list[str]:
    """Beancount ``commodity`` directives for the in-use ISINs.

    Known ISINs render full UK-tax metadata dated to ``first_acquired``;
    ISINs absent from ``commodities`` get a ``reporting-status:
    "unknown"`` stub dated ``1970-01-01``, preceded by a comment
    nudging the user to complete ``data/commodities.toml``. Beancount
    metadata keys are kebab-case in output (``reporting-status``) even
    though the pydantic fields are snake_case. Directives are ordered by
    (date, ISIN) for deterministic output.
    """

    entries: list[tuple[date, str, list[str]]] = []
    for isin in in_use_isins:
        meta = commodities.get(isin)
        if meta is not None:
            entries.append((
                meta.first_acquired,
                isin,
                [
                    f"{meta.first_acquired.isoformat()} commodity {isin}",
                    f'  name: "{meta.name}"',
                    f'  isin: "{meta.isin}"',
                    f'  domicile: "{meta.domicile}"',
                    f'  reporting-status: "{meta.reporting_status}"',
                    f'  asset-class: "{meta.asset_class}"',
                ],
            ))
        else:
            entries.append((
                date(1970, 1, 1),
                isin,
                [
                    "; missing metadata — add an entry to data/commodities.toml",
                    f"1970-01-01 commodity {isin}",
                    '  reporting-status: "unknown"',
                ],
            ))

    entries.sort(key=lambda e: (e[0], e[1]))
    lines: list[str] = []
    for _, _, directive in entries:
        lines.extend(directive)
        lines.append("")
    return lines


def _scan_files(
    files: Sequence[Path],
) -> tuple[dict[str, str], dict[str, str]]:
    """Walk ``files`` and return ``(earliest_post, inline_opens)`` —
    the earliest posting date per account, and the inline-open date
    per account that already carries one. Files are read in order so
    the per-year output stays deterministic.
    """

    earliest_post: dict[str, str] = {}
    inline_opens: dict[str, str] = {}

    for path in files:
        current_date: str | None = None
        for line in path.read_text(encoding="utf-8").splitlines():
            m_open = _OPEN_RE.match(line)
            if m_open:
                d, a = m_open.groups()
                if a not in inline_opens or d < inline_opens[a]:
                    inline_opens[a] = d
                continue
            m_txn = _TXN_DATE_RE.match(line)
            if m_txn:
                current_date = m_txn.group(1)
                continue
            m_post = _POSTING_RE.match(line)
            if m_post and current_date is not None:
                a = m_post.group(1)
                if a not in earliest_post or current_date < earliest_post[a]:
                    earliest_post[a] = current_date

    return earliest_post, inline_opens


def _render(
    files: Sequence[Path],
    operating_currencies: Iterable[str],
    header: str,
    extra_includes: Sequence[str] = (),
    booking_method: str | None = "FIFO",
    commodities: Mapping[str, CommodityMetadata] | None = None,
) -> tuple[str, int]:
    """Build the aggregate file body. Returns ``(content, account_count)``.

    ``extra_includes`` is appended after the per-year includes — the
    portfolio generator uses it to pull in ``prices.beancount`` when
    that file exists alongside the per-year output, so Fava and
    bean-report can value security holdings in the operating
    currency.

    ``booking_method`` controls beancount's per-account inventory
    reduction policy on sells (``FIFO`` / ``LIFO`` / ``AVERAGE`` /
    ``STRICT`` / ``NONE``). Defaults to ``"FIFO"``; pass ``None`` to
    omit the directive entirely (which leaves beancount on its
    ``STRICT`` default — explicit lot labels required).
    """

    earliest_post, inline_opens = _scan_files(files)
    central = {a: d for a, d in earliest_post.items() if a not in inline_opens}
    rows = sorted(central.items(), key=lambda kv: (kv[1], kv[0]))

    # Per-ISIN quotation-currency map, derived from each security's
    # cost-basis annotations. Powers the constraint resolution on
    # per-ISIN Income accounts (Realized / Unrealized / Dividend
    # / Interest / bare-ISIN forms).
    isin_currencies = _extract_isin_currencies(files)

    lines: list[str] = [header.rstrip("\n"), ""]

    # Beancount ``option`` directives go above the dated entries. Multiple
    # operating currencies are allowed and reported in the order declared.
    op_currencies = list(operating_currencies)
    for ccy in op_currencies:
        lines.append(f'option "operating_currency" "{ccy}"')
    if booking_method is not None:
        lines.append(f'option "booking_method" "{booking_method}"')
    if op_currencies or booking_method is not None:
        lines.append("")

    # UK-tax commodity metadata, above the account opens. Emitted only
    # when a metadata source is supplied (``commodities is not None``);
    # without one the aggregate is byte-identical to before. In-use
    # ISINs without an entry get a stub so the user notices the gap.
    if commodities is not None:
        commodity_lines = _commodity_directives(
            sorted(isin_currencies), commodities
        )
        if commodity_lines:
            lines.append(";; UK-tax commodity metadata.")
            lines.extend(commodity_lines)

    for account, entry_date in rows:
        c = _constraint(account, isin_currencies)
        lines.append(f"{entry_date} open {account}" + (f" {c}" if c else ""))

    lines.append("")
    lines.append(";; Per-year ingest output.")
    for path in files:
        lines.append(f'include "{path.name}"')

    if extra_includes:
        lines.append("")
        lines.append(";; Auxiliary files (prices, etc.).")
        for name in extra_includes:
            lines.append(f'include "{name}"')

    lines.append("")

    total_accounts = len(set(earliest_post) | set(inline_opens))
    return "\n".join(lines), total_accounts


def generate(
    data_dir: Path,
    output: Path | None = None,
    *,
    operating_currencies: Iterable[str] = ("GBP",),
    header: str = _DEFAULT_HEADER,
    booking_method: str | None = "FIFO",
    statement_files: Iterable[Path] = (),
    commodities: Mapping[str, CommodityMetadata] | None = None,
) -> tuple[Path, int]:
    """Write a portfolio aggregate file. Returns ``(output_path, accounts)``.

    ``data_dir`` is scanned for ``*.beancount`` files; ``output`` defaults
    to ``<data_dir>/portfolio.beancount``. The output file is excluded
    from the scan so re-running the generator is idempotent.

    ``operating_currencies`` is the list of currencies that show up as
    ``option "operating_currency" "<ccy>"`` directives at the top of
    the aggregate. Defaults to ``("GBP",)`` because that's the user's
    home currency for net-worth roll-ups today; pass a longer tuple
    when a multi-currency view is needed.

    ``booking_method`` declares the inventory-reduction policy on
    sells. Defaults to ``"FIFO"``; pass ``None`` to omit the
    directive (leaving beancount on its ``STRICT`` default — sells
    must specify lot labels explicitly).
    """

    if output is None:
        output = data_dir / "portfolio.beancount"

    # Auxiliary files that the aggregate ``include``s but doesn't
    # treat as transaction sources. ``prices.beancount`` is the
    # price-database extracted from per-trade inventory annotations
    # (and optionally monthly-statement valuations);
    # ``balances.beancount`` is the per-holding /
    # per-cash-sub-account assertion set extracted from monthly
    # statements. Both are optional — included only when present.
    aux_present = [
        name for name in _AUX_FILENAMES if (data_dir / name).is_file()
    ]

    files = _source_files(data_dir, output)

    content, total = _render(
        files,
        operating_currencies,
        header,
        extra_includes=aux_present,
        booking_method=booking_method,
        commodities=commodities,
    )
    output.write_text(content, encoding="utf-8")
    return output, total

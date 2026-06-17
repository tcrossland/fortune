"""Portfolio-aggregate generator.

Walks a directory of per-year ``*.beancount`` ingest output and writes
a single ``portfolio.beancount`` file that:

  - declares the user's operating currency (or currencies) via
    ``option "operating_currency" "<ccy>"``,
  - emits an ``open`` directive for every account referenced by a
    posting that isn't already opened inline by the writer (the writer
    emits an inline open for first-time security buys; redeclaring
    those centrally would double-open and beancount errors on that),
  - emits a ``close`` directive for every ISIN asset account whose units
    net to exactly zero across the *full* history (the per-source ingest
    output deliberately carries no closes — only the aggregate sees a
    later source re-acquiring a holding, so only it can close safely),
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
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from datetime import date
from pathlib import Path

from banking_pipeline.commodities_metadata import CommodityMetadata
from banking_pipeline.writer import render_close_directives
from banking_pipeline.writer.profile import PROFILES

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
# A posting whose amount is denominated in a bare 3-letter currency (the
# commodity token right after the number, terminated by whitespace or
# end-of-line so ISIN commodities — always longer — never match). Used
# to learn which 3-letter tokens are *actually* currencies in this
# ledger, so a 3-letter account-name segment (e.g. ``…:Earnout:IBM``)
# isn't mistaken for one.
_POSTING_COMMODITY_RE = re.compile(
    r"^\s+(?:Assets|Liabilities|Income|Expenses|Equity)(?::[A-Za-z0-9-]+)+"
    r"\s+-?[\d.,']+\s+([A-Z]{3})(?:\s|$)"
)
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
    currencies: set[str] | None = None,
) -> str | None:
    """Beancount commodity constraint for ``open <account> <ccy>``, or
    ``None`` when the account's last segment doesn't unambiguously imply
    one.

    Three resolution paths, in order:

      - **Last segment is an ISO 4217 currency** (``…:EUR``) — return
        the currency. Used for cash sub-accounts and per-currency
        Fees / Interest accounts. When ``currencies`` is supplied the
        segment must also appear in it — i.e. it's a 3-letter token the
        ledger actually denominates a posting in — so a counterparty
        name that happens to be three letters (``…:Earnout:IBM``) isn't
        misread as a currency and over-constrained.
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
    if _CURRENCY_RE.fullmatch(last) and (currencies is None or last in currencies):
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


def _observed_currencies(files: Sequence[Path]) -> set[str]:
    """The set of 3-letter currency codes the ledger denominates a
    posting amount in, across ``files``.

    A currency is "real" here if it appears as the bare commodity right
    after a posting's amount (``… 1150000.00 EUR``). This is what lets
    :func:`_constraint` tell a genuine currency segment (``…:EUR``) from
    a three-letter counterparty / label segment (``…:Earnout:IBM``):
    only the former shows up as an actual posting currency.
    """

    currencies: set[str] = set()
    for path in files:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _POSTING_COMMODITY_RE.match(line)
            if m is not None:
                currencies.add(m.group(1))
    return currencies


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

# A top-level ``include`` directive — its presence marks a file as an
# aggregate (it pulls in other ledger files) rather than a per-year
# ingest source. Indented matches don't occur in practice; anchor to the
# line start to be safe.
_INCLUDE_RE = re.compile(r'^\s*include\s+"', re.MULTILINE)


def _is_aggregate(path: Path) -> bool:
    """True if ``path`` is itself an aggregate (contains an ``include``).

    Per-year ingest output is a flat list of opens / transactions and
    never includes another file; a portfolio aggregate — this generator's
    own output under any name — does. Re-scanning an aggregate as a source
    would re-include the per-year and auxiliary files it already pulls in,
    so beancount reports ``Duplicate filename parsed`` for each.
    """

    return _INCLUDE_RE.search(path.read_text(encoding="utf-8")) is not None


def _source_files(
    data_dir: Path, output: Path, ignore: frozenset[str] = frozenset()
) -> list[Path]:
    """Per-year ``*.beancount`` ingest files under ``data_dir``.

    Excludes the aggregate ``output`` (so re-running is idempotent), the
    auxiliary price / balance files (which are ``include``d but not
    scanned as transaction sources), any other aggregate file — a stale or
    per-account roll-up this generator wrote under a different name, which
    would otherwise be double-included via its own includes — and any
    ``ignore`` filenames the caller supplies (e.g. the property ledger,
    which ``main.beancount`` includes directly and which owns its own
    opens, so sourcing or re-including it would double-count).
    """

    return [
        f
        for f in sorted(data_dir.glob("*.beancount"))
        if f.resolve() != output.resolve()
        and f.name not in _AUX_FILENAMES
        and f.name not in ignore
        and not _is_aggregate(f)
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
    include_prefix: str = "",
    extra_options: Sequence[str] = (),
) -> tuple[str, int]:
    """Build the aggregate file body. Returns ``(content, account_count)``.

    ``extra_includes`` is appended after the per-year includes — the
    portfolio generator uses it to pull in ``prices.beancount`` when
    that file exists alongside the per-year output, so Fava and
    bean-report can value security holdings in the operating
    currency.

    ``include_prefix`` is prepended to every ``include`` path (both the
    per-year sources and ``extra_includes``). It's empty for the
    aggregate, which sits beside the files it includes; the per-account
    split writes into a sub-directory and passes ``"../"`` so its
    includes still resolve to the per-year output one level up.

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

    # Materialised once: ``operating_currencies`` may be a one-shot
    # iterator, and it's consumed both here and for the option directives.
    op_currencies = list(operating_currencies)

    # 3-letter tokens the ledger actually denominates postings in, so a
    # currency-shaped account-name segment that isn't a currency (e.g.
    # ``Income:External:Earnout:IBM``) doesn't get a bogus constraint.
    # Operating and ISIN-quotation currencies count too.
    currencies = (
        _observed_currencies(files)
        | set(op_currencies)
        | set(isin_currencies.values())
    )

    lines: list[str] = [header.rstrip("\n"), ""]

    # Beancount ``option`` directives go above the dated entries. Multiple
    # operating currencies are allowed and reported in the order declared.
    for ccy in op_currencies:
        lines.append(f'option "operating_currency" "{ccy}"')
    if booking_method is not None:
        lines.append(f'option "booking_method" "{booking_method}"')
    # Verbatim extra ``option`` lines — the per-account split passes the
    # ``inferred_tolerance_default`` directives from the root ledger so an
    # isolated file balances under the same rounding tolerances.
    lines.extend(extra_options)
    if op_currencies or booking_method is not None or extra_options:
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
        c = _constraint(account, isin_currencies, currencies)
        lines.append(f"{entry_date} open {account}" + (f" {c}" if c else ""))

    # Aggregate-aware ``close`` directives. Per-source ingest output carries
    # no closes — a per-batch close can't see a *later* source re-acquiring a
    # holding, and beancount can't reopen a closed account. The aggregate sees
    # every source file, so it sums each ISIN asset account across the full
    # history and closes only those that net to exactly zero. The close date is
    # the day after that account's last posting, which is by construction after
    # any re-buy, so a re-acquired-then-resold position closes cleanly and a
    # still-held one is never closed.
    closes = render_close_directives(
        "\n".join(path.read_text(encoding="utf-8") for path in files)
    )
    if closes:
        lines.append("")
        lines.append(";; Closed accounts (ISIN positions wound down to zero).")
        lines.extend(closes.splitlines())

    lines.append("")
    lines.append(";; Per-year ingest output.")
    for path in files:
        lines.append(f'include "{include_prefix}{path.name}"')

    if extra_includes:
        lines.append("")
        lines.append(";; Auxiliary files (prices, etc.).")
        for name in extra_includes:
            lines.append(f'include "{include_prefix}{name}"')

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
    ignore: Iterable[str] = (),
) -> tuple[Path, int]:
    """Write a portfolio aggregate file. Returns ``(output_path, accounts)``.

    ``data_dir`` is scanned for ``*.beancount`` files; ``output`` defaults
    to ``<data_dir>/portfolio.beancount``. The output file is excluded
    from the scan so re-running the generator is idempotent. ``ignore`` is
    a set of filenames to exclude from the scan in addition — the caller
    passes any generated ledger that ``main.beancount`` includes directly
    (e.g. the property ledger), so it's neither sourced nor re-included.

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

    files = _source_files(data_dir, output, frozenset(ignore))

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


# --- per-account split ---------------------------------------------------
#
# The combined aggregate above rolls every account into one
# ``portfolio.beancount``. The split below instead writes one
# independently-loadable ledger per bank account — the shape Fava wants
# when you open a single Pictet account (or the ISA) in isolation.
#
# It works because each per-year ingest file is, by construction, a single
# bank+account stream: ``2025-K`` is all of Pictet ``K-123456.001``,
# ``vanguard-isa`` is the whole ISA. So splitting is "group the source
# files by account, then run the same open/close scan per group". A
# counterparty leg (e.g. ``Assets:Revolut:GBP`` on a Pictet payment) is a
# minority posting with no bank prefix, so the majority key still pins the
# file to its owning account.


# Known bank account-name prefixes, longest first so a multi-segment
# prefix (``Vgd:ISA``) is matched before a hypothetical single-segment one
# that shares its head.
_BANK_PREFIXES: tuple[tuple[str, ...], ...] = tuple(
    sorted(
        (tuple(p.account_prefix.split(":")) for p in PROFILES.values()),
        key=len,
        reverse=True,
    )
)


def _account_key(account: str) -> str | None:
    """The owning-account key for a beancount ``account`` path, or ``None``.

    Strips the leaf type segment (``Assets`` / ``Income`` / …) and matches
    the remainder against the known bank prefixes. A multi-segment prefix
    (``Vgd:ISA``) is itself the key; a single-segment one (``Pic``) takes
    the following portfolio segment too (``Pic:K123456001``). Accounts with
    no bank prefix — counterparties like ``Assets:Revolut:GBP`` — return
    ``None`` so they don't form a group of their own.
    """

    body = account.split(":")[1:]  # drop Assets / Income / Expenses / …
    for prefix in _BANK_PREFIXES:
        n = len(prefix)
        if tuple(body[:n]) != prefix:
            continue
        if n >= 2:  # multi-segment prefix is the whole account (e.g. Vgd:ISA)
            return ":".join(prefix)
        if len(body) >= 2:  # single-segment prefix + its portfolio segment
            return ":".join(body[:2])
        return ":".join(prefix)
    return None


def _file_account_key(path: Path) -> str | None:
    """The dominant bank-account key across ``path``'s postings.

    Returns the most common :func:`_account_key` among the file's posting
    lines — robust to a stray counterparty leg — or ``None`` when the file
    posts to no recognised bank account (e.g. a property ledger)."""

    keys: Counter[str] = Counter()
    for line in path.read_text(encoding="utf-8").splitlines():
        m = _POSTING_RE.match(line)
        if not m:
            continue
        key = _account_key(m.group(1))
        if key is not None:
            keys[key] += 1
    if not keys:
        return None
    # most_common breaks ties by insertion order; sort the top group for a
    # deterministic key regardless of posting order.
    top = max(keys.values())
    return sorted(k for k, c in keys.items() if c == top)[0]


def group_files_by_account(files: Sequence[Path]) -> dict[str, list[Path]]:
    """Group per-year source ``files`` by their owning bank account.

    Files with no recognised bank account are skipped. Keys are account
    keys (``Pic:K123456001``, ``Vgd:ISA``); values keep ``files`` order.
    """

    groups: dict[str, list[Path]] = {}
    for path in files:
        key = _file_account_key(path)
        if key is not None:
            groups.setdefault(key, []).append(path)
    return groups


def _account_filename(account_key: str) -> str:
    """Filesystem-safe filename for an account key (``Pic:K123456001`` →
    ``Pic-K123456001.beancount``)."""

    return account_key.replace(":", "-") + ".beancount"


_TOLERANCE_RE = re.compile(
    r'^option\s+"inferred_tolerance_default"\s+"[^"]+"\s*$', re.MULTILINE
)


def inferred_tolerance_options(ledger: Path) -> list[str]:
    """The ``inferred_tolerance_default`` option lines from ``ledger``.

    The per-currency rounding tolerances are hand-curated in the root
    ledger (``main.beancount``); a per-account file needs the same set to
    balance standalone. Returns the verbatim ``option`` lines (empty if the
    file is absent or declares none)."""

    if not ledger.is_file():
        return []
    return _TOLERANCE_RE.findall(ledger.read_text(encoding="utf-8"))


def generate_per_account(
    data_dir: Path,
    output_dir: Path | None = None,
    *,
    operating_currencies: Iterable[str] = ("GBP",),
    booking_method: str | None = "FIFO",
    commodities: Mapping[str, CommodityMetadata] | None = None,
    ignore: Iterable[str] = (),
    extra_options: Sequence[str] = (),
) -> list[tuple[Path, str, int]]:
    """Write one independently-loadable ledger per bank account.

    Groups the per-year ingest files under ``data_dir`` by owning account
    and writes ``<output_dir>/<account>.beancount`` for each — its own
    ``option`` directives, central opens, cross-history closes, and
    ``include``s of that account's per-year files plus ``prices.beancount``
    (one level up). ``balances.beancount`` is deliberately *not* included:
    its assertions span every account, so an isolated ledger would fail
    bean-check on accounts it never opens.

    ``extra_options`` are verbatim ``option`` lines emitted in each file —
    the caller passes the root ledger's ``inferred_tolerance_default``
    directives (see :func:`inferred_tolerance_options`) so an isolated
    ledger balances under the same rounding tolerances as the full load.

    ``output_dir`` defaults to ``<data_dir>/accounts``. Returns one
    ``(path, account_key, account_count)`` per file written, sorted by
    account key.
    """

    if output_dir is None:
        output_dir = data_dir / "accounts"

    op_currencies = list(operating_currencies)
    files = _source_files(data_dir, data_dir / "portfolio.beancount", frozenset(ignore))
    groups = group_files_by_account(files)

    # Prices are shared and harmless (marks for unheld commodities are
    # ignored); balances are excluded — see the docstring.
    prices_present = ["prices.beancount"] if (data_dir / "prices.beancount").is_file() else []

    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[tuple[Path, str, int]] = []
    for account_key in sorted(groups):
        group = groups[account_key]
        header = (
            f";; Per-account ledger — {account_key}.\n"
            ";; Independently loadable (own options + opens + closes);\n"
            ";; includes this account's per-year ingest output and prices.\n"
        )
        content, total = _render(
            group,
            op_currencies,
            header,
            extra_includes=prices_present,
            booking_method=booking_method,
            commodities=commodities,
            include_prefix="../",
            extra_options=extra_options,
        )
        out_path = output_dir / _account_filename(account_key)
        out_path.write_text(content, encoding="utf-8")
        written.append((out_path, account_key, total))
    return written

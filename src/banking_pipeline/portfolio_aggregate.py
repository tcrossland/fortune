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
from collections.abc import Iterable, Sequence
from pathlib import Path

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


def _constraint(account: str) -> str | None:
    """Beancount commodity constraint for ``open <account> <ccy>``, or
    ``None`` when the account's last segment doesn't unambiguously imply
    one. Mirrors the writer's per-trade logic so reading the central
    open from this file matches what the inline opens emit.
    """

    last = account.rsplit(":", 1)[-1]
    if _CURRENCY_RE.fullmatch(last):
        return last
    if _ISIN_RE.fullmatch(last) and 11 <= len(last) <= 12:
        return last
    return None


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
) -> tuple[str, int]:
    """Build the aggregate file body. Returns ``(content, account_count)``."""

    earliest_post, inline_opens = _scan_files(files)
    central = {a: d for a, d in earliest_post.items() if a not in inline_opens}
    rows = sorted(central.items(), key=lambda kv: (kv[1], kv[0]))

    lines: list[str] = [header.rstrip("\n"), ""]

    # Beancount ``option`` directives go above the dated entries. Multiple
    # operating currencies are allowed and reported in the order declared.
    op_currencies = list(operating_currencies)
    for ccy in op_currencies:
        lines.append(f'option "operating_currency" "{ccy}"')
    if op_currencies:
        lines.append("")

    for account, date in rows:
        c = _constraint(account)
        lines.append(f"{date} open {account}" + (f" {c}" if c else ""))

    lines.append("")
    lines.append(";; Per-year ingest output.")
    for path in files:
        lines.append(f'include "{path.name}"')
    lines.append("")

    total_accounts = len(set(earliest_post) | set(inline_opens))
    return "\n".join(lines), total_accounts


def generate(
    data_dir: Path,
    output: Path | None = None,
    *,
    operating_currencies: Iterable[str] = ("GBP",),
    header: str = _DEFAULT_HEADER,
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
    """

    if output is None:
        output = data_dir / "portfolio.beancount"

    files = [
        f
        for f in sorted(data_dir.glob("*.beancount"))
        if f.resolve() != output.resolve()
    ]

    content, total = _render(files, operating_currencies, header)
    output.write_text(content, encoding="utf-8")
    return output, total

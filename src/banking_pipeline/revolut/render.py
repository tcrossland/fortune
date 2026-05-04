"""Render :class:`RevolutTxn` objects to beancount text.

Mirrors the format emitted by :mod:`banking_pipeline.writer` (two-space
posting indent, two-decimal amounts, ``@@`` for total cost on FX) so the
output drops cleanly into an existing ledger and survives ``bean-check``.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import timedelta
from decimal import Decimal

from banking_pipeline.revolut.models import Posting, RevolutTxn

INDENT = "  "
# Width chosen so amounts line up after typical Revolut Personal account
# names (``Assets:Revolut:Personal:FlexibleCash:GBP`` is 41 chars). Adjust
# in one place if your ledger uses longer or shorter account paths.
ACCOUNT_COLUMN_WIDTH = 50


def render(txns: Iterable[RevolutTxn]) -> str:
    """Render an iterable of transactions as a beancount text block."""

    chunks = [_render_one(t) for t in txns]
    return "\n".join(chunks).rstrip() + "\n"


def render_open_directives(txns: Iterable[RevolutTxn]) -> str:
    """Emit ``open`` directives for every distinct asset account seen.

    Useful as a one-shot bootstrap when adding Revolut to a fresh ledger.
    Uses 2020-01-01 as the anchor date — replace with the user's preferred
    epoch as needed. Only ``Assets:Revolut:…`` accounts are emitted; the
    user typically has their own opens for Expenses/Income placeholders.
    """

    accounts: set[tuple[str, str]] = set()
    for t in txns:
        for p in t.postings:
            if p.account.startswith("Assets:Revolut:"):
                accounts.add((p.account, p.currency))
    lines = [f"2020-01-01 open {acct} {ccy}" for acct, ccy in sorted(accounts)]
    return "\n".join(lines) + ("\n" if lines else "")


def _render_one(t: RevolutTxn) -> str:
    flag = "!" if t.flagged else "*"
    header = _format_header(t.txn_date, flag, t.payee, t.narration)
    parts = [header]
    parts.extend(_format_metadata(t))
    parts.extend(_format_posting(p) for p in t.postings)
    parts.extend(_format_balances(t))
    parts.append("")  # trailing blank line between transactions
    return "\n".join(parts)


def _format_header(txn_date: object, flag: str, payee: str | None, narration: str) -> str:
    if payee:
        return f'{txn_date} {flag} "{_escape(payee)}" "{_escape(narration)}"'
    return f'{txn_date} {flag} "{_escape(narration)}"'


def _format_metadata(t: RevolutTxn) -> list[str]:
    out: list[str] = []
    for key, value in t.metadata.items():
        if key.startswith("_"):  # internal-only keys (e.g. "_balances")
            continue
        out.append(f'{INDENT}{key}: "{_escape(value)}"')
    return out


def _format_posting(p: Posting) -> str:
    account_field = _pad(f"{INDENT}{p.account}", ACCOUNT_COLUMN_WIDTH)
    amount_field = f"{_fmt(p.amount)} {p.currency}"
    if p.cost is None:
        return f"{account_field}{amount_field}"
    cost_amount, cost_ccy = p.cost
    return f"{account_field}{amount_field} @@ {_fmt(cost_amount)} {cost_ccy}"


def _format_balances(t: RevolutTxn) -> list[str]:
    """Emit ``balance`` directives for end-of-day balances attached to ``t``.

    Beancount checks ``balance`` at the start of the asserted day, so the
    asserted date is the day **after** the txn (i.e. start-of-next-day equals
    end-of-this-day).
    """

    raw = t.metadata.get("_balances", "")
    if not raw:
        return []
    next_day = t.txn_date + timedelta(days=1)
    # The directive prefix is the literal "YYYY-MM-DD balance " (19 chars);
    # pad the account to land the amount at the same column as transaction
    # postings. ``_pad`` enforces a minimum one-space separator so the line
    # stays valid even when the account name overflows the column.
    prefix_width = len(f"{next_day} balance ")
    account_pad = ACCOUNT_COLUMN_WIDTH - prefix_width
    out: list[str] = [""]  # blank line before the balance block
    for line in raw.splitlines():
        if not line:
            continue
        account, balance_str, ccy = line.split("|")
        bal = Decimal(balance_str)
        out.append(
            f"{next_day} balance {_pad(account, account_pad)}{_fmt(bal)} {ccy}"
        )
    return out


def _fmt(amount: Decimal) -> str:
    """Format a Decimal with exactly two fractional digits."""

    return f"{amount.quantize(Decimal('0.01')):.2f}"


def _pad(s: str, width: int) -> str:
    """Pad ``s`` to ``width`` with spaces, but always leave at least one
    trailing space so the next field doesn't run into the account name when
    the account exceeds the requested column width.
    """

    if len(s) < width:
        return s.ljust(width)
    return s + " "


def _escape(s: str) -> str:
    """Escape characters that would break a beancount string literal."""

    return s.replace("\\", "\\\\").replace('"', '\\"')

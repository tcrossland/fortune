"""Shared Markdown/CSV rendering helpers for the analytical reports.

The valuation reports (``concentration``, ``net-worth``, ``allocation``,
``portfolio-allocation``, ``income``) all format money the same way and all
emit the same "some figures were excluded because a GBP rate was missing"
warning section. These helpers are the single definition so the formatting
and wording stay consistent across reports.
"""

from __future__ import annotations

from collections.abc import Iterable
from decimal import ROUND_HALF_UP, Decimal

from banking_pipeline.tax.uk.currency import RateGap

_ZERO = Decimal(0)


def money(value: Decimal) -> str:
    """Bare 2-dp amount (no symbol, no thousands separator) — for CSV cells."""

    return f"{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)}"


def gbp(value: Decimal) -> str:
    """A £-prefixed, thousands-separated 2-dp amount — for Markdown."""

    return f"£{value.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):,}"


def pct(value: Decimal, total: Decimal) -> str:
    """``value`` as a 1-dp percentage of ``total``; ``—`` when total is zero."""

    if total == _ZERO:
        return "—"
    return f"{(value / total * 100).quantize(Decimal('0.1'), rounding=ROUND_HALF_UP)}%"


def rate_gap_lines(
    gaps: Iterable[RateGap], *, title: str, intro: str
) -> list[str]:
    """Markdown for the "excluded — missing GBP rate" warning section.

    Returns a ``## ⚠️ <title>`` heading, the ``intro`` paragraph, then one
    bullet per gap (``- <currency> <month> (<isin>)``), de-duplicated and
    ordered by month / currency / isin. The caller guards on a non-empty
    ``gaps`` and appends these lines to its own ``lines`` list.
    """

    uniq = sorted(set(gaps), key=lambda g: (g.month, g.currency, g.isin))
    return [
        f"## ⚠️ {title}",
        "",
        intro,
        "",
        *[f"- {g.currency} {g.month} ({g.isin})" for g in uniq],
        "",
    ]

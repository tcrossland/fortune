"""Dataclasses for Revolut CSV ingestion.

Two layers:

* :class:`RevolutRow` — a verbatim parsed row from the CSV. One per line.
* :class:`RevolutTxn` — a normalised transaction ready for beancount
  rendering. EXCHANGE legs from two source rows collapse into one
  ``RevolutTxn`` with two postings; everything else is one row → one txn.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True, slots=True)
class RevolutRow:
    """One row from a Revolut Personal CSV export, as-parsed.

    Schema as published by the Revolut Personal app (Statement → CSV)::

        Type, Product, Started Date, Completed Date, Description,
        Amount, Fee, Currency, State, Balance

    ``amount`` and ``fee`` are signed decimals; outflows are negative.
    ``balance`` is the post-transaction balance in this pocket.
    """

    type: str
    product: str
    started_date: datetime
    completed_date: datetime | None
    description: str
    amount: Decimal
    fee: Decimal
    currency: str
    state: str
    balance: Decimal | None
    # Origin filename — only used for diagnostics on unmatched EXCHANGE legs.
    source_file: str = ""


@dataclass(frozen=True, slots=True)
class Posting:
    """One leg of a beancount transaction."""

    account: str
    amount: Decimal
    currency: str
    # When set, renders ``-100.00 GBP @@ 117.50 EUR`` (total cost). Used by
    # EXCHANGE transactions where the source leg's value is the destination
    # leg's amount. Format is (cost_amount, cost_currency).
    cost: tuple[Decimal, str] | None = None


@dataclass(frozen=True, slots=True)
class RevolutTxn:
    """A normalised transaction destined for beancount rendering."""

    txn_date: date
    payee: str | None
    narration: str
    postings: tuple[Posting, ...]
    # End-of-day balance for the source pocket, when available. Rendered as
    # ``YYYY-MM-DD balance ACCOUNT AMOUNT CCY`` after the last txn of the day.
    balance: tuple[str, Decimal, str] | None = None
    # Free-form metadata appended as ``  key: "value"`` lines.
    metadata: dict[str, str] = field(default_factory=dict)
    # When True, the transaction is flagged ``!`` (incomplete / needs review)
    # rather than ``*`` (cleared). Used for unmatched EXCHANGE legs.
    flagged: bool = False

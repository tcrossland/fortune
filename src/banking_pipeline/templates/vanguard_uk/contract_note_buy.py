"""``vanguard_uk.vanguard_contract_note_buy.v1`` — ISA buy contract note.

Vanguard issues this under "Contract note" / "ISA - buy transaction
details" when a contribution is invested. One ``Shares - <name>
(<ticker>)`` block per purchased fund carries the trade detail::

    Shares - FTSE 250 UCITS ETF - Accumulating
    (VMIG)
    ...
    Transaction date and time: 13 Feb 2025 10:18:09.353
    Shares price: £37.330600
    Number of shares purchased: 13.00
    Total purchase cost
    (including Transaction charges): £485.30

The buy side prints no ISIN — the ticker (``VMIG``) is the security
identifier and is used directly as the beancount commodity (the sell
note repeats the same ticker, so lots match). A single note can buy
several funds, so this returns one ``Transaction`` per block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.vanguard_uk._common import (
    find_account_number,
    parse_long_date,
    resolve_ticker,
)

# One match per purchased security. ``re.S`` lets ``.*?`` span the line
# breaks Vanguard inserts between fields; each group is non-greedy so a
# match covers exactly one block (name → … → total purchase cost) and
# stops before the next ``Shares -`` header. The fund name runs up to an
# optional ``(<ticker>)`` and the ``Securities trader name:`` line that
# opens every block — the ticker is optional because sell notes omit it
# (the buy note always carries it, but the shared shape keeps both
# templates aligned).
_BUY_BLOCK_RE = re.compile(
    r"Shares\s*-\s*(?P<name>.+?)\s*"
    r"(?:\((?P<ticker>[A-Z0-9]{2,6})\)\s*)?Securities\s+trader\s+name:"
    r".*?Transaction date and time:\s*(?P<date>\d{1,2}\s+\w+\s+\d{4})"
    r".*?Shares price:\s*£(?P<price>[\d,]+\.\d+)"
    r".*?Number of shares purchased:\s*(?P<qty>[\d,]+\.\d+)"
    r".*?Total purchase cost[\s\S]*?£(?P<total>[\d,]+\.\d{2})",
    re.S,
)


@dataclass
class VanguardContractNoteBuyTemplate:
    template_id: str = "vanguard_uk.vanguard_contract_note_buy.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text
        account_number = find_account_number(text)

        transactions: list[Transaction] = []
        for m in _BUY_BLOCK_RE.finditer(text):
            name = re.sub(r"\s+", " ", m.group("name")).strip()
            ticker = resolve_ticker(name, m.group("ticker"))
            total = Decimal(m.group("total").replace(",", ""))
            transactions.append(
                Transaction(
                    trade_date=parse_long_date(m.group("date")),
                    narration=f"Buy {name} ({ticker})",
                    title="Contract note",
                    # Cash leg: GBP out of the ISA cash account.
                    currency="GBP",
                    amount=-total,
                    # Security leg: ticker as commodity, GBP-quoted.
                    isin=ticker,
                    quantity=Decimal(m.group("qty").replace(",", "")),
                    price=Decimal(m.group("price").replace(",", "")),
                    security_currency="GBP",
                    account_number=account_number,
                    account_wrapper="isa",
                    source_path=doc.path,
                )
            )
        return transactions

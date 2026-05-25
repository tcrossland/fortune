"""``vanguard_uk.vanguard_contract_note_sell.v1`` — ISA sell contract note.

Mirror of the buy note, issued under "Contract note" / "ISA - sell
transaction details"::

    Shares - FTSE 250 UCITS ETF - Accumulating
    (VMIG)
    Security identification number
    (ISIN): IE00BFMXVQ44
    ...
    Transaction date and time: 11 Aug 2025 10:18:26.857
    Shares price: £39.943800
    Number of shares sold: 13.00
    Gross proceeds
    (before transaction charges): £519.27
    Net proceeds: £519.27

The sell side does print an ISIN, but the ticker is used as the
beancount commodity for consistency with the buy note so the disposal
reduces the lot the buy created. ``Net proceeds`` is the cash credited
to the ISA. One ``Transaction`` per sold fund.
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

# Same shape as the buy block. The ticker is optional (some sell blocks
# print only the ISIN); the ISIN is captured as a ticker fallback. The
# name terminates at the optional ``(<ticker>)`` plus the ``Securities
# trader name:`` line, so the ``(ISIN):`` token that follows can't be
# mistaken for the ticker.
_SELL_BLOCK_RE = re.compile(
    r"Shares\s*-\s*(?P<name>.+?)\s*"
    r"(?:\((?P<ticker>[A-Z0-9]{2,6})\)\s*)?Securities\s+trader\s+name:"
    r"(?:.*?\(ISIN\):\s*(?P<isin>[A-Z0-9]{12}))?"
    r".*?Transaction date and time:\s*(?P<date>\d{1,2}\s+\w+\s+\d{4})"
    r".*?Shares price:\s*£(?P<price>[\d,]+\.\d+)"
    r".*?Number of shares sold:\s*(?P<qty>[\d,]+\.\d+)"
    r".*?Net proceeds:\s*£(?P<net>[\d,]+\.\d{2})",
    re.S,
)


@dataclass
class VanguardContractNoteSellTemplate:
    template_id: str = "vanguard_uk.vanguard_contract_note_sell.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text
        account_number = find_account_number(text)

        transactions: list[Transaction] = []
        for m in _SELL_BLOCK_RE.finditer(text):
            name = re.sub(r"\s+", " ", m.group("name")).strip()
            ticker = resolve_ticker(name, m.group("ticker"), m.group("isin"))
            net = Decimal(m.group("net").replace(",", ""))
            transactions.append(
                Transaction(
                    trade_date=parse_long_date(m.group("date")),
                    narration=f"Sell {name} ({ticker})",
                    title="Contract note",
                    # Cash leg: GBP into the ISA cash account.
                    currency="GBP",
                    amount=net,
                    isin=ticker,
                    # Negative: a disposal reduces the position. Vanguard
                    # prints the count unsigned ("shares sold: 13.00"), so
                    # negate it to match the writer's signed-quantity
                    # convention (the asset leg reduces the lot via ``{}``).
                    quantity=-Decimal(m.group("qty").replace(",", "")),
                    price=Decimal(m.group("price").replace(",", "")),
                    security_currency="GBP",
                    account_number=account_number,
                    account_wrapper="isa",
                    source_path=doc.path,
                )
            )
        return transactions

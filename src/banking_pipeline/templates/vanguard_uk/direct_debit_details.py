"""``vanguard_uk.vanguard_direct_debit_details.v1`` — quarterly account fee.

Vanguard collects the platform account fee from the user's external
bank by direct debit, so it never moves the ISA's own cash (the regular
statement shows it ``charged`` then ``cleared``, netting to zero). The
fee notice carries the amount and the period it covers::

    Your Account fee payable for this quarter is £10.11.
    The Account fee covers the period 14 Feb 2025 through 13 May 2025.

This emits one ``Transaction`` for the fee. The Vanguard fee builder
books it as ``Expenses:…:Fees`` against the contributions-equity
account — the same self-contained external leg used for contributions —
so the ISA cash balance is untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.vanguard_uk._common import (
    find_account_number,
    parse_long_date,
)

_FEE_RE = re.compile(
    r"Account\s+fee\s+payable\s+for\s+this\s+quarter\s+is\s*£([\d,]+\.\d{2})",
    re.I,
)
# ``... covers the period 14 Feb 2025 through 13 May 2025`` — the end
# date is the fee's effective booking date.
_PERIOD_END_RE = re.compile(
    r"through\s+(\d{1,2}\s+\w+\s+\d{4})", re.I
)


@dataclass
class VanguardDirectDebitDetailsTemplate:
    template_id: str = "vanguard_uk.vanguard_direct_debit_details.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        fee_match = _FEE_RE.search(text)
        period_match = _PERIOD_END_RE.search(text)
        if fee_match is None or period_match is None:
            return []

        amount = Decimal(fee_match.group(1).replace(",", ""))
        return [
            Transaction(
                trade_date=parse_long_date(period_match.group(1)),
                narration="Platform account fee",
                title="Account fee",
                currency="GBP",
                amount=amount,
                account_number=find_account_number(text),
                account_wrapper="isa",
                source_path=doc.path,
            )
        ]

"""``vanguard_uk.vanguard_regular_statement.v1`` — quarterly ISA statement.

The regular statement's ``Activity ... for your ISA`` table is the
complete cash ledger for the ISA::

    Transaction date Transaction details   Cash amount  Cash balance
    13/02/2025 Deposit for Investment
    Purchases                              £1,000.00    £1,000.00
    13/02/2025 Bought 13 FTSE 250 ...      -£485.30     £514.70
    01/03/2025 Cash Account Interest       £0.19        £16.94

It restates the buys/sells — which the contract notes own — so this
template emits **only** the two row kinds that appear nowhere else: the
cash ``Deposit ...`` contributions and the monthly ``Cash Account
Interest`` credits. Bought / Sold / Account-fee rows are skipped to
avoid double-counting (the fee is booked from the direct-debit details
advice; the trades from the contract notes).

Both emitted kinds carry ``account_wrapper="isa"`` and route to the
Vanguard statement builder, which keys off the narration to post the
contribution against the contributions-equity account and the interest
against the ISA interest income account.

Empty periods
-------------
A statement for a drained / £0 account carries no ``Activity`` section
and legitimately yields ``[]``. Because this doctype normally emits, the
extractor logs that empty result at WARN ("investigate if a regression")
and ``--strict`` raises on it — a false positive for a genuinely
nil-activity statement. Normal ``ingest`` / ``rebuild`` are unaffected
(exit 0, no output emitted); only ``--strict`` runs need such statements
excluded from their source glob.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.vanguard_uk._common import (
    find_account_number,
    parse_short_date,
)

# Canonical narrations the statement builder branches on. Kept as
# constants so the template and the builder agree on the exact strings.
DEPOSIT_NARRATION = "Deposit for Investment Purchases"
INTEREST_NARRATION = "Cash Account Interest"

# The activity table sits between its header and the protection notice.
_ACTIVITY_RE = re.compile(
    r"Activity\s+from\b.*?for\s+your\s+ISA(?P<body>.*?)"
    r"(?:Your\s+cash\s+and\s+asset\s+protection|\Z)",
    re.S | re.I,
)
# One activity row: a ``dd/mm/yyyy`` date, then everything up to the next
# date (or the end of the table). The body spans the wrapped description
# lines and the trailing ``<cash amount> <cash balance>`` pair.
_ROW_RE = re.compile(
    r"(?P<date>\d{2}/\d{2}/\d{4})(?P<body>.*?)(?=\d{2}/\d{2}/\d{4}|\Z)",
    re.S,
)
# A ``£`` amount inside a row, with an optional leading minus.
_MONEY_RE = re.compile(r"(-?)£\s*([\d,]+\.\d{2})")


@dataclass
class VanguardRegularStatementTemplate:
    template_id: str = "vanguard_uk.vanguard_regular_statement.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text
        account_number = find_account_number(text)

        activity = _ACTIVITY_RE.search(text)
        if activity is None:
            return []

        transactions: list[Transaction] = []
        for row in _ROW_RE.finditer(activity.group("body")):
            body = row.group("body")
            money = _MONEY_RE.findall(body)
            if not money:
                continue
            # First £ amount on the row is the cash movement; the second
            # is the running balance, which we don't book.
            sign, digits = money[0]
            cash = Decimal(digits.replace(",", ""))
            if sign:
                cash = -cash
            desc = _MONEY_RE.sub("", body)
            desc = re.sub(r"\s+", " ", desc).strip()

            if desc.startswith("Deposit"):
                narration = DEPOSIT_NARRATION
            elif INTEREST_NARRATION in desc:
                narration = INTEREST_NARRATION
            else:
                # Bought / Sold / Account fee rows — owned by the contract
                # notes and the direct-debit advice respectively.
                continue

            transactions.append(
                Transaction(
                    trade_date=parse_short_date(row.group("date")),
                    narration=narration,
                    title="Regular statement",
                    currency="GBP",
                    amount=cash,
                    account_number=account_number,
                    account_wrapper="isa",
                    source_path=doc.path,
                )
            )
        return transactions

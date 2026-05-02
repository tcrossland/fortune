"""``pictet.final_redemption.v1`` — bond / structured-product maturity payout.

Pictet emits this document under ``SECURITY EVENT / Redemption / Final
redemption`` when a bond or structured product reaches maturity and the
issuer pays out the holder. Field shape sits between a trade advice and a
dividend notice:

  - ``Quantity`` (negative — units leaving the portfolio) instead of
    ``Executed quantity``;
  - ``Redemption price`` instead of ``Execution price``;
  - the security event narration line ``Redemption - <name>`` matches the
    same shape used by dividend advices.

We extract a single :class:`~banking_pipeline.models.Transaction` for the
cash leg (``Net amount``, positive — proceeds in). Quantity is preserved
as printed (negative on a redemption, mirroring the trade-advice helper's
sign convention).
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    find_amount_field,
    find_field,
    find_subject_line,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
    resolve_isin,
)


@dataclass
class PictetFinalRedemptionTemplate:
    template_id: str = "pictet.final_redemption.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Redemption price`` is the load-bearing tell that distinguishes a
        # final redemption from a fund redemption (which uses
        # ``Execution price`` instead). Bail if it's missing.
        price_match = find_amount_field(text, "Redemption price")
        if price_match is None:
            return []

        trade_date_raw = find_field(text, "Trade date")
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, "Net amount")
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, "Value date")
        quantity_raw = find_field(text, "Quantity")

        subject = find_subject_line(text, "Redemption")
        narration = (
            f"Final redemption - {subject}"
            if subject
            else "Final redemption"
        )[:140]

        tx = Transaction(
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
            currency=currency,
            amount=amount,
            isin=resolve_isin(text),
            quantity=parse_pictet_amount(quantity_raw) if quantity_raw else None,
            price=price_match[1],
            account_number=resolve_account_number(text),
            source_path=doc.path,
        )
        return [tx]

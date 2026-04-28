"""``pictet.payment.v1`` — outgoing third-party payment advice.

Pictet emits this document under ``PAYMENT TRANSACTIONS / Payment`` when
the client wires money to an external account. The advice carries:

  - the destination details (``Beneficiary``, ``Bank``, destination IBAN),
  - a free-text ``Communication`` field (the wire memo),
  - and the cash leg as ``Net amount`` under ``CASH EFFECT`` — already
    inclusive of any ``Payment fees`` Pictet charged on the wire.

We map the advice to a single
:class:`~banking_pipeline.models.Transaction`. Two render paths:

  1. **Self-to-self transfer**: when the destination ``Bank`` resolves
     via :data:`banking_pipeline.config.settings.beneficiary_bank_map`
     (e.g. ``REVOLUT BANK UAB`` → ``Revolut``) the user is wiring
     money to one of their own external accounts. Populate
     ``gross_amount`` and ``counter_account`` so the writer can emit
     a three-leg entry (destination credited with the principal,
     source debited with the net, Pictet's wire fee broken out as
     an expense).
  2. **Genuine third-party**: when the destination bank isn't in the
     map, leave the cross-leg fields ``None``; the writer falls back
     to the simpler two-leg-elastic shape (cash leg + ``Expenses:<prefix>:Other``).

Self-to-self detection is keyed on the destination bank, *not* on a
name match between ``Beneficiary`` and ``Client``. The bank map is
authoritatively user-configured (these are the user's own external
banks); name matching is brittle in practice because Pictet's PDF
extractor often produces case-shifted or middle-name-stripped
beneficiary strings on real wires (``First LASTNAMES`` vs the
client's ``FIRST MIDDLE LASTNAMES``).

Narration combines the beneficiary and the wire's communication, e.g.
``FIRST MIDDLE LASTNAMES - Liquidity``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.config import settings
from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    EN_LABELS,
    find_amount_field,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
)

def _resolve_counter_account(bank_field: str | None) -> str | None:
    """Map Pictet's ``Bank`` field to the destination account-name segment.

    Looks up substrings from :data:`settings.beneficiary_bank_map`; the
    first map entry whose key is a substring of ``bank_field`` wins. The
    map's natural form ('REVOLUT BANK UAB' → 'Revolut') matches Pictet's
    printed bank text (``REVOLUT BANK UAB, SUCURSAL EN ESPAN``). Returns
    ``None`` when no entry matches — the writer falls back to the
    elastic third-party-payment shape on those.
    """

    if not bank_field:
        return None
    upper = bank_field.upper()
    for needle, segment in settings.beneficiary_bank_map.items():
        if needle.upper() in upper:
            return segment
    return None


@dataclass
class PictetPaymentTemplate:
    template_id: str = "pictet.payment.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Beneficiary`` is the load-bearing field that distinguishes an
        # outgoing payment from an incoming one (``Instructing party``);
        # absence means the doc was misrouted.
        beneficiary = find_field(text, "Beneficiary")
        if beneficiary is None:
            return []

        trade_date_raw = find_field(text, EN_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, EN_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, EN_LABELS.value_date)
        booking_date_raw = find_field(text, EN_LABELS.booking_date)
        communication = find_field(text, "Communication")

        if beneficiary and communication:
            narration = f"{beneficiary} - {communication}"
        else:
            narration = communication or beneficiary or "Pictet payment"

        # --- Self-to-self detection ----------------------------------
        # Bank-map presence is the load-bearing signal: if the
        # destination bank resolves to a configured account-name
        # segment, this is by definition a transfer to one of the
        # user's own external accounts. More robust than name matching,
        # which Pictet routinely truncates / case-shifts on real wires
        # (``First LASTNAMES`` vs the client's ``FIRST MIDDLE LASTNAMES``).
        #
        # ``Bank`` is ambiguous — the document carries one in its
        # signature block (``Bank Pictet & Cie (Europe) AG``, naming
        # Pictet itself) and one inside the ``Payment`` section
        # (``Bank REVOLUT BANK UAB, …``, the beneficiary's bank).
        # ``find_field`` returns the first match, which is the wrong
        # one. Scope the lookup to the text *after* the ``Beneficiary``
        # line so we read the beneficiary-side bank.
        bank_field = None
        benef_pos = re.search(r"^Beneficiary\b", text, re.M)
        if benef_pos is not None:
            sub = text[benef_pos.start():]
            bank_match = re.search(r"^Bank\s+(.+?)\s*$", sub, re.M)
            if bank_match is not None:
                bank_field = bank_match.group(1)
        counter_account = _resolve_counter_account(bank_field)

        gross_amount: object = None  # Decimal | None
        fees: object = None
        fees_currency: object = None
        if counter_account is not None:
            # Pictet prints the gross amount twice: once near the top
            # (``Gross amount GBP 12'000.00`` — positive, the principal
            # sent) and once inside the ``CASH EFFECT`` block (negative,
            # the source-account perspective). ``find_amount_field``
            # matches the first occurrence, which is the positive
            # principal we want for the destination leg.
            gross_match = find_amount_field(text, "Gross amount")
            if gross_match is not None:
                _, gross_amount = gross_match  # type: ignore[assignment]
            # Wire fee — read from the ``CASH EFFECT`` block's
            # ``Costs <ccy> <amount>`` line. Surfaced as a dedicated
            # ``Expenses:<prefix>:Fees:<ccy>`` posting in the writer.
            costs_match = find_amount_field(text, EN_LABELS.costs)
            if costs_match is not None:
                fees_currency, fees = costs_match  # type: ignore[assignment]

        return [
            Transaction(
                trade_date=parse_pictet_date(trade_date_raw),
                settlement_date=(
                    parse_pictet_date(value_date_raw) if value_date_raw else None
                ),
                booking_date=(
                    parse_pictet_date(booking_date_raw)
                    if booking_date_raw
                    else None
                ),
                narration=narration[:140],
                title="Payment",
                currency=currency,
                amount=amount,
                gross_amount=gross_amount,  # type: ignore[arg-type]
                counter_account=counter_account,
                fees=fees,  # type: ignore[arg-type]
                fees_currency=fees_currency,  # type: ignore[arg-type]
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]

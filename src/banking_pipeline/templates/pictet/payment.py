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
    resolve_counterparty,
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


def _payment_section(text: str) -> str | None:
    """Return the ``Payment`` section text — between the ``Beneficiary``
    line and the next ``CASH EFFECT`` marker — or ``None`` when the
    ``Beneficiary`` anchor is absent.

    Pictet payment advices have two ambiguous fields the parser has to
    disambiguate by section:

      - ``Bank`` appears both in the signature block (``Bank Pictet & Cie
        (Europe) AG``, naming Pictet itself) and inside the Payment
        section (``Bank REVOLUT BANK UAB, …``, the beneficiary's bank).
        Document layout determines which precedes the other.
      - ``Gross amount`` appears once in the Payment section (positive,
        the principal sent) and once inside the ``CASH EFFECT`` block
        (negative, source-account perspective).

    Bounding lookups to the section between ``Beneficiary`` and
    ``CASH EFFECT`` makes both fields unambiguous: the beneficiary's
    bank and the positive principal are the only matches that survive.
    """

    benef_match = re.search(r"^Beneficiary\b", text, re.M)
    if benef_match is None:
        return None
    end = text.find(EN_LABELS.cash_effect_marker, benef_match.start())
    if end == -1:
        return text[benef_match.start():]
    return text[benef_match.start():end]


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
        # The bank lookup is scoped to ``_payment_section`` — the
        # signature-block ``Bank Pictet & Cie...`` is structurally
        # outside that section regardless of where it falls in the
        # document, so the first ``^Bank`` match within the section is
        # always the beneficiary's bank.
        section = _payment_section(text)
        bank_field = None
        if section is not None:
            bank_match = re.search(r"^Bank\s+(.+?)\s*$", section, re.M)
            if bank_match is not None:
                bank_field = bank_match.group(1)
        counter_account = _resolve_counter_account(bank_field)

        gross_amount: object = None  # Decimal | None
        fees: object = None
        fees_currency: object = None
        if counter_account is not None:
            # Section-bounded ``Gross amount`` lookup: the Payment
            # section's occurrence is the positive principal we want
            # for the destination leg. The ``CASH EFFECT`` block's
            # signed-negative occurrence is excluded by the section
            # bound — pinning to the section instead of relying on
            # document order is what guarantees correctness across
            # layouts.
            #
            # If the bank-map fired but the section's ``Gross amount``
            # line is missing, we fail loud rather than silently
            # downgrading: the writer's three-leg shape requires a
            # gross amount, and a missing one means either a parser
            # regression or a Pictet format change worth surfacing.
            # Silent downgrade would misroute the destination credit
            # to ``Expenses:<prefix>:Other`` instead of
            # ``Assets:<counter_account>:<ccy>``.
            assert section is not None  # counter_account ⇒ section was found
            gross_match = find_amount_field(section, "Gross amount")
            if gross_match is None:
                raise ValueError(
                    f"pictet.payment.v1: counter_account resolved to "
                    f"{counter_account!r} but Payment-section 'Gross "
                    f"amount' line is missing in {doc.path}"
                )
            _, gross_amount = gross_match  # type: ignore[assignment]
            # Wire fee — read from the ``CASH EFFECT`` block's
            # ``Costs <ccy> <amount>`` line. Surfaced as a dedicated
            # ``Expenses:<prefix>:Fees:<ccy>`` posting in the writer.
            costs_match = find_amount_field(text, EN_LABELS.costs)
            if costs_match is not None:
                fees_currency, fees = costs_match  # type: ignore[assignment]

        # Counterparty resolution — only when self-to-self routing
        # didn't fire (counter_account is None, i.e., the destination
        # bank isn't in beneficiary_bank_map). On genuine third-party
        # outgoing wires the Beneficiary name is the load-bearing
        # signal for routing the elastic ``Expenses:...`` leg.
        counterparty_account = (
            resolve_counterparty(beneficiary)
            if counter_account is None
            else None
        )

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
                counterparty_account=counterparty_account,
                fees=fees,  # type: ignore[arg-type]
                fees_currency=fees_currency,  # type: ignore[arg-type]
                account_number=resolve_account_number(text, EN_LABELS),
                transaction_number=find_transaction_number(text, EN_LABELS),
                source_path=doc.path,
            )
        ]

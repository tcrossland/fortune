"""``pictet.pago.v1`` — Spanish-locale outgoing third-party payment.

The ES counterpart to :mod:`payment`. Pictet's Madrid succursale
emits this document under ``TRÁFICO DE PAGOS / Pago`` (mixed-case
``Pago``, on its own line) when the client wires money out to an
external account. Field shape mirrors the EN ``PAYMENT`` advice with
translated labels:

  - ``Beneficiario`` (was ``Beneficiary``) — destination name; load-
    bearing for distinguishing outgoing from incoming wires (incoming
    advices use ``Ordenante`` instead).
  - ``Banco`` (was ``Bank``) — destination bank; fed into
    :data:`settings.beneficiary_bank_map` for self-to-self routing.
  - ``Comunicación`` (was ``Communication``) — free-text wire memo,
    folded into the entry's narration.
  - ``Importe bruto`` / ``Costes`` / ``Gastos de pago`` / ``Importe
    neto`` — the cash-impact block, all in ``EFECTO CASH en la
    cartera``.

Two render paths, same as the EN payment template:

  1. **Self-to-self transfer**: when the destination ``Banco``
     resolves via ``beneficiary_bank_map`` (e.g. ``REVOLUT BANK
     UAB`` → ``Revolut``) the user is wiring to one of their own
     external accounts; populate ``gross_amount`` and
     ``counter_account`` so the writer's three-leg shape fires.
  2. **Genuine third-party**: bank-map miss; fall back to the
     elastic two-leg form. When the ``Beneficiario`` resolves via
     ``settings.counterparty_account_map`` (e.g. ``BANCO SANTANDER``
     → ``External:Vendor:Santander``) the elastic leg is routed to
     that account; otherwise the catch-all
     ``Expenses:Pic:<portfolio>:Other`` placeholder fires.

Self-to-self detection is keyed on the destination bank, *not* on a
name match between ``Beneficiario`` and ``Cliente`` — same rationale
as :mod:`payment`. Pictet's PDF extractor often case-shifts or
truncates beneficiary names on real wires, but the bank field stays
stable because it's printed verbatim from Pictet's bank database.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.config import settings
from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
    resolve_counterparty,
)


def _resolve_counter_account(bank_field: str | None) -> str | None:
    """Map Pictet's ``Banco`` field to the destination account-name
    segment via :data:`settings.beneficiary_bank_map`. Mirrors
    :func:`payment._resolve_counter_account` exactly — the same map
    covers both EN ``Bank`` and ES ``Banco`` fields because both
    name the same external bank from the user's perspective.
    """

    if not bank_field:
        return None
    upper = bank_field.upper()
    for needle, segment in settings.beneficiary_bank_map.items():
        if needle.upper() in upper:
            return segment
    return None


def _payment_section(text: str) -> str | None:
    """Return the ``Pago`` section text — between the ``Beneficiario``
    line and the next ``EFECTO CASH`` marker — or ``None`` when the
    ``Beneficiario`` anchor is absent.

    Bounding lookups to this section makes ``Banco`` and
    ``Importe bruto`` unambiguous: the document carries a ``Banco``
    line in its signature block (``Pictet & Cie (Europe) S.A.``) and
    a ``Importe bruto`` line inside the ``EFECTO CASH`` block too,
    both of which would shadow the Pago-section values without the
    section bound.
    """

    benef_match = re.search(r"^Beneficiario\b", text, re.M)
    if benef_match is None:
        return None
    end = text.find(ES_LABELS.cash_effect_marker, benef_match.start())
    if end == -1:
        return text[benef_match.start():]
    return text[benef_match.start():end]


@dataclass
class PictetPagoTemplate:
    template_id: str = "pictet.pago.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        # ``Beneficiario`` is the load-bearing field that distinguishes
        # an outgoing payment from an incoming one (which uses
        # ``Ordenante`` instead); absence means the doc was misrouted.
        beneficiario = find_field(text, "Beneficiario")
        if beneficiario is None:
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, ES_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, ES_LABELS.value_date)
        booking_date_raw = find_field(text, ES_LABELS.booking_date)
        comunicacion = find_field(text, "Comunicación")

        if beneficiario and comunicacion:
            narration = f"{beneficiario} - {comunicacion}"
        else:
            narration = comunicacion or beneficiario or "Pictet pago"

        # --- Self-to-self detection (Banco line in Pago section) -----
        section = _payment_section(text)
        bank_field = None
        if section is not None:
            bank_match = re.search(r"^Banco\s+(.+?)\s*$", section, re.M)
            if bank_match is not None:
                bank_field = bank_match.group(1)
        counter_account = _resolve_counter_account(bank_field)

        gross_amount: object = None  # Decimal | None
        fees: object = None
        fees_currency: object = None
        if counter_account is not None:
            # Section-bounded ``Importe bruto`` lookup (same trick as
            # the EN ``payment`` template): the Payment-section
            # occurrence is the positive principal we want for the
            # destination leg; the ``EFECTO CASH`` block's
            # signed-negative occurrence is excluded by the section.
            assert section is not None
            gross_match = find_amount_field(section, ES_LABELS.gross_amount)
            if gross_match is None:
                raise ValueError(
                    f"pictet.pago.v1: counter_account resolved to "
                    f"{counter_account!r} but Pago-section "
                    f"'Importe bruto' line is missing in {doc.path}"
                )
            _, gross_amount = gross_match  # type: ignore[assignment]
            # Wire fee — read from the ``EFECTO CASH`` block's
            # ``Costes <ccy> <amount>`` line.
            costs_match = find_amount_field(text, ES_LABELS.costs)
            if costs_match is not None:
                fees_currency, fees = costs_match  # type: ignore[assignment]

        # Counterparty resolution — only when self-to-self routing
        # didn't fire (genuine third-party path). The destination's
        # printed name is the lookup key against
        # ``counterparty_account_map``.
        counterparty_account = (
            resolve_counterparty(beneficiario)
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
                title="Pago",
                currency=currency,
                amount=amount,
                gross_amount=gross_amount,  # type: ignore[arg-type]
                counter_account=counter_account,
                counterparty_account=counterparty_account,
                fees=fees,  # type: ignore[arg-type]
                fees_currency=fees_currency,  # type: ignore[arg-type]
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]

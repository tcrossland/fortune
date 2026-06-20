"""``pictet.pago_interna.v1`` — Spanish-locale incoming self-to-self payment.

Issued by Pictet's Madrid branch under ``TRÁFICO DE PAGOS / PAGO
ENTRANTE`` when a client-owned external account (Revolut etc.)
credits the client's Pictet portfolio. The Spanish counterpart to
the EN-locale ``INCOMING_PAYMENT``, but in the *self-to-self* shape
where the ordering party is the user themselves rather than a
third-party counterparty.

Self-to-self detection is keyed on the source bank rather than on a
name match between ``Ordenante`` and ``Cliente``: same approach as
the outgoing :mod:`payment` template uses for the ``Bank`` field.
The ``Banco`` line on the advice (``Banco REVOLUT PAYMENTS UAB``)
is looked up against
:data:`banking_pipeline.config.settings.beneficiary_bank_map`; when
the bank resolves (e.g. ``REVOLUT PAYMENTS UAB`` → ``Revolut``) we
populate ``counter_account`` so the writer can emit a clean two-leg
entry ``Equity:Transfers:<counter_account>:<ccy>`` ↔
``Assets:Pic:<portfolio>:<ccy>``. When the bank doesn't resolve the
template still extracts but ``counter_account`` stays ``None`` and
the writer falls back to the elastic ``Income:Pic:<portfolio>:Other``
counter-leg the third-party variant uses.

Earlier this template emitted a single ``Transaction`` whose only
counter-leg was the legacy ``_CASH_IN_TEMPLATE``'s
``Equity:Uncategorized`` placeholder; the new shape resolves the
real source-and-destination flow so balance-sheet roll-ups don't
accumulate spurious equity balances on every Revolut → Pictet
transfer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.config import settings
from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_comment_line,
    find_field,
    parse_pictet_date,
    resolve_account_number,
)

_PAGO_ENTRANTE_TITLE_RE = re.compile(r"^PAGO\s+ENTRANTE\s*$", re.M)


def _resolve_counter_account(bank_field: str | None) -> str | None:
    """Map the ``Banco`` field to the source bank's account-name segment.

    Mirrors :func:`payment._resolve_counter_account` exactly — the
    same ``beneficiary_bank_map`` covers both directions because the
    map is a list of the user's own external banks; the only
    difference is which Pictet field carries the bank name
    (``Bank`` for outgoing, ``Banco`` for incoming).
    """

    if not bank_field:
        return None
    upper = bank_field.upper()
    for needle, segment in settings.beneficiary_bank_map.items():
        if needle.upper() in upper:
            return segment
    return None


@dataclass
class PictetPagoInternaTemplate:
    template_id: str = "pictet.pago_interna.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _PAGO_ENTRANTE_TITLE_RE.search(text):
            return []

        ordenante = find_field(text, "Ordenante")
        if ordenante is None:
            return []

        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        cash_effect = find_amount_field(text, ES_LABELS.net_amount)
        if cash_effect is None:
            return []
        currency, amount = cash_effect

        value_date_raw = find_field(text, ES_LABELS.value_date)
        comment = find_comment_line(text, label="Comentario")

        narration_parts = [f"Pictet pago entrante de {ordenante}"]
        if comment:
            narration_parts.append(comment)
        narration = " - ".join(narration_parts)[:140]

        # Source-bank resolution. Pictet prints the source bank under
        # ``Banco`` inside the Pago section. Look up the line directly
        # rather than via ``find_field("Banco")`` because that label
        # also appears elsewhere; ``Ordenante`` is the load-bearing
        # anchor for the section.
        counter_account = None
        ord_match = re.search(r"^Ordenante\b", text, re.M)
        if ord_match is not None:
            sub = text[ord_match.start():]
            bank_match = re.search(r"^Banco\s+(.+?)\s*$", sub, re.M)
            if bank_match is not None:
                counter_account = _resolve_counter_account(
                    bank_match.group(1)
                )

        tx = Transaction(
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration,
            currency=currency,
            amount=amount,
            isin=None,
            quantity=None,
            price=None,
            counter_account=counter_account,
            account_number=resolve_account_number(text, ES_LABELS),
            source_path=doc.path,
        )
        return [tx]

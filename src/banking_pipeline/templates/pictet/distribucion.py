"""``pictet.distribucion.v1`` — Spanish-locale fund distribution / dividend advice.

Spanish counterpart to :mod:`dividend_notice`. Pictet's Madrid branch
issues this document under ``HECHOS RELEVANTES / Distribución /
Dividendo ordinario`` when a held fund pays an income distribution.

Field-label divergences from the EN :mod:`dividend_notice` advice
-----------------------------------------------------------------
This is a security-event document, not a stock-exchange trade, so the
trade-advice labels are absent or renamed:

  - ``Cantidad detenida`` (held quantity, the position that generated
    the dividend) instead of ``Quantity held``.
  - ``Renta unitaria`` (per-unit income) instead of ``Income per unit``.
  - Trade / value / booking dates use the standard ES labels
    (``Fecha de transacción`` / ``Fecha valor`` / ``Fecha contable``)
    and align with ``Fecha Ex`` / ``Fecha de pago`` / publication date.
  - The narration line is ``Dividendo - <fund name>``; ``find_subject_line``
    picks up the fund name from the line below the ``Tipo`` block.

Dispatched to the same beancount builder as ``DIVIDEND_NOTICE``
(:func:`banking_pipeline.writer.builders.dividend.render`) — the
income/cash leg shape is identical across locales.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_field,
    find_subject_line,
    find_transaction_number,
    find_withholding_tax,
    parse_pictet_amount,
    parse_pictet_date,
    resolve_account_number,
    resolve_isin,
)

# ``Distribución`` is the load-bearing tell that separates this advice
# from the regular ``Reembolso`` / ``Reembolso final`` security events
# under the same ``HECHOS RELEVANTES`` banner. Anchored to a full line
# via ``^...$`` + ``re.M`` so the word doesn't false-match inside any
# narration text.
_DISTRIBUCION_TITLE_RE = re.compile(r"^Distribuci[oó]n\s*$", re.M | re.I)


@dataclass
class PictetDistribucionTemplate:
    template_id: str = "pictet.distribucion.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _DISTRIBUCION_TITLE_RE.search(text):
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
        # ``Cantidad detenida`` (Quantity held) — the position that
        # generated the dividend, not a transferred quantity.
        quantity_raw = find_field(text, "Cantidad detenida")
        # ``Renta unitaria`` (Income per unit) — Spanish counterpart to
        # the EN ``Income per unit`` line.
        income_match = find_amount_field(text, "Renta unitaria")

        # Narration: ``Dividendo - <fund>``. Mirrors the EN advice's
        # ``Dividend - <fund>`` form (and the ES ``Reembolso - <fund>``
        # narration on reembolso_final), preserving the issuer's
        # vocabulary rather than translating across locales.
        subject = find_subject_line(text, "Dividendo")
        narration = (
            f"Dividendo - {subject}" if subject else "Pictet distribución"
        )[:140]

        isin = resolve_isin(text)
        wht = find_withholding_tax(text, ES_LABELS, isin)

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
                narration=narration,
                title="Distribución",
                currency=currency,
                amount=amount,
                isin=isin,
                quantity=parse_pictet_amount(quantity_raw) if quantity_raw else None,
                price=income_match[1] if income_match else None,
                gross_income=wht[0] if wht else None,
                withholding_tax=wht[1] if wht else None,
                withholding_country=wht[2] if wht else None,
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]

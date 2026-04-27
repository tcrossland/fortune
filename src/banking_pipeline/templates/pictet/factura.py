"""``pictet.factura.v1`` — Spanish tax invoice for management fees.

Issued by Pictet's Madrid branch under ``FACTURA / Servicios financieros``
as a tax-compliant Spanish invoice for quarterly management fees. Unlike
:mod:`debito_de_gastos` (which carries an ``EFECTO CASH`` block as a cash
debit), the factura is structured as an *invoice document*:

  - Top-of-document summary lists ``Importe bruto`` / ``Costes`` /
    ``Total`` as positive amounts (invoice line items, not signed cash
    impacts).
  - There is no ``EFECTO CASH`` block — instead a ``Débito`` section
    points at the current account that will be debited, but doesn't
    repeat the amount as a signed line.
  - The doc carries an ``N° de factura`` (invoice number) which is the
    primary identifier used for the document in the client's records.

We synthesise a single :class:`Transaction` representing the cash debit
that will hit the client's account at the value date. The amount is
``Total`` *negated* — the doc presents the invoice amount as positive,
but Transaction records cash impact, which is negative for a fee debit.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_field,
    find_period,
    parse_pictet_date,
    resolve_account_number,
)

_FACTURA_TITLE_RE = re.compile(r"^FACTURA\s*$", re.M)
_INVOICE_NO_RE = re.compile(r"^N°\s*de\s+factura\s*:\s*(\S+)", re.M)


@dataclass
class PictetFacturaTemplate:
    template_id: str = "pictet.factura.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        text = doc.text

        if not _FACTURA_TITLE_RE.search(text):
            return []

        # ``Fecha de transacción`` and ``Fecha valor`` both appear under
        # the INFORMACIÓN ADICIONAL block — find_field returns the first
        # match, which is the General-section value (the one we want).
        trade_date_raw = find_field(text, ES_LABELS.trade_date)
        if not trade_date_raw:
            return []

        # ``Total`` line — the first ``Total`` is the doc-level invoice
        # total at the top of the page; the second appears under the
        # ``Costes`` breakdown (same value). Either works; we take the
        # first match.
        total = find_amount_field(text, "Total")
        if total is None:
            return []
        currency, gross_total = total

        value_date_raw = find_field(text, ES_LABELS.value_date)

        invoice_no_match = _INVOICE_NO_RE.search(text)
        invoice_label = (
            f"n° {invoice_no_match.group(1)}" if invoice_no_match else ""
        )
        period = find_period(text, label="Período")
        if period:
            start, end = period
            period_str = f" {start.isoformat()} a {end.isoformat()}"
        else:
            period_str = ""
        narration = (
            f"Pictet factura {invoice_label} - Honorarios de gestión{period_str}"
            .replace("  ", " ")
            .strip()
        )

        tx = Transaction(
            trade_date=parse_pictet_date(trade_date_raw),
            settlement_date=(
                parse_pictet_date(value_date_raw) if value_date_raw else None
            ),
            narration=narration[:140],
            currency=currency,
            # Negate: the invoice presents the amount as positive (an
            # invoice amount, not a cash impact). Transaction.amount is
            # signed cash-impact, negative for a fee debit.
            amount=-gross_total,
            isin=None,
            quantity=None,
            price=None,
            account_number=resolve_account_number(text, ES_LABELS),
            source_path=doc.path,
        )
        return [tx]

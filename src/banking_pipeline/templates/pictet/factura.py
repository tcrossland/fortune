"""``pictet.factura.v1`` — Spanish tax invoice for management fees.

Issued by Pictet's Madrid branch under ``FACTURA / Servicios financieros``
as a tax-compliant Spanish invoice for quarterly management fees. Unlike
:mod:`debito_de_gastos` (which carries an ``EFECTO CASH`` block as a
cash debit), the factura is structured as an *invoice document*:

  - The top-of-document summary lists ``Importe bruto`` / ``Costes`` /
    ``Total`` as positive amounts (invoice line items, not signed cash
    impacts).
  - There is no ``EFECTO CASH`` block — instead a ``Débito`` section
    points at the current account that will be debited.
  - The doc carries an ``N° de factura`` (invoice number) which is the
    primary identifier in the client's records.

To avoid double-counting we emit no beancount entry for this document.
The factura describes the same economic event as the matching
``Débito de gastos`` advice (the cash leg the bank actually books
against the user's account), and emitting both would post the fee
twice. The classifier still routes ``FACTURA`` documents correctly so
audit/diagnostic logs see them; the template intentionally returns
an empty list, similar to :mod:`interest_scale`.
"""

from __future__ import annotations

from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction


@dataclass
class PictetFacturaTemplate:
    template_id: str = "pictet.factura.v1"

    def extract(self, doc: RawDocument) -> list[Transaction]:
        # Intentionally empty: the matching ``Débito de gastos`` advice
        # carries the cash leg for the same fee event. Emitting anything
        # here would double-count.
        return []

"""``pictet.pago_entrante.v1`` — Spanish-locale third-party incoming payment.

Counterpart to :mod:`pago_interna`, which handles the self-to-self
variant of the same Pictet ``TRÁFICO DE PAGOS`` advice. The two are
distinguished structurally by Pictet's title casing:

  - ``PAGO ENTRANTE`` (all caps) — self-to-self transfer from a
    client-owned external account (Revolut etc.). Goes through
    :mod:`pago_interna`.
  - ``Pago entrante`` (mixed case) — third-party incoming payment
    (employer earnout, vendor invoice settlement). Goes through this
    template.

Books the cash leg into ``Assets:<prefix>:<currency>`` and credits an
elastic ``Income:<prefix>:Other`` posting that beancount auto-balances
against. The income-account naming is a placeholder convention —
adjust the writer's render path if the user wants payer-specific
income accounts (``Income:Pic:Earnout``, ``Income:Pic:<Ordenante>``,
etc.) per :func:`banking_pipeline.beancount_writer._render_third_party_payment`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from banking_pipeline.models import RawDocument, Transaction
from banking_pipeline.templates.pictet._common import (
    ES_LABELS,
    find_amount_field,
    find_comment_line,
    find_field,
    find_transaction_number,
    parse_pictet_date,
    resolve_account_number,
    resolve_counterparty,
)

# Case-sensitive title gate — the load-bearing discriminator from
# ``pago_interna`` (whose title is all-caps ``PAGO ENTRANTE``). Anchored
# to a full line so the ``TRÁFICO DE PAGOS`` banner above doesn't
# accidentally match.
_PAGO_ENTRANTE_TITLE_RE = re.compile(r"^Pago\s+entrante\s*$", re.M)


@dataclass
class PictetPagoEntranteTemplate:
    template_id: str = "pictet.pago_entrante.v1"

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
        booking_date_raw = find_field(text, ES_LABELS.booking_date)
        comment = find_comment_line(text, label="Comentario")

        # Narration combines the third-party payer with the comment when
        # both are present, falling back to either alone when only one
        # is. Format ``<Ordenante> - <Comentario>`` is informative
        # enough for cross-referencing without dragging in the
        # correspondent bank / country / address fields.
        if ordenante and comment:
            narration = f"{ordenante} - {comment}"
        else:
            narration = comment or ordenante or "Pictet pago entrante"

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
                title="Pago entrante",
                currency=currency,
                amount=amount,
                # ``Ordenante`` → mapped income-account segment via
                # ``settings.counterparty_account_map`` when the name
                # resolves; ``None`` otherwise. Writer falls back to
                # ``Income:<prefix>:<portfolio>:Other`` on those.
                counterparty_account=resolve_counterparty(ordenante),
                account_number=resolve_account_number(text, ES_LABELS),
                transaction_number=find_transaction_number(text, ES_LABELS),
                source_path=doc.path,
            )
        ]

"""Catch-all fallback beancount builder.

Used by the dispatcher for any ``DocumentType`` not handled by a
shape-specific builder, and by
:mod:`banking_pipeline.writer.builders.internal_transfer` when the
cross-leg fields (``counter_currency`` / ``counter_amount``) are
missing — defensive cover for legacy callers that built ``Transaction``
objects without populating them.

Produces a two-leg ``Equity:Uncategorized``-balanced entry and a
``; TODO review`` audit comment so the user notices it on next
``bean-check`` / Fava load. The entry is *parseable*, not correct;
the comment is the user's signal to wire a real builder for that
doctype.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.format import (
    cash_account,
    escape,
    format_amount,
)


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a generic two-leg ``Equity:Uncategorized``-balanced entry.

    Layout (matches the historical Jinja form byte-for-byte so any
    ledgers produced via the fallback path keep diffing cleanly)::

        <trade_date> * "<narration>"
          ; TODO review — document type: <doc_type>
          Assets:<prefix>:<portfolio>:<currency>  <amount> <ccy>
          Equity:Uncategorized                           <-amount> <ccy>

    Trade date (not booking date) anchors the entry — the fallback
    shape can't assume the document carried a booking date. The
    ``Equity:Uncategorized`` leg is signed-flipped so the entry
    balances arithmetically; the ``TODO review`` comment surfaces the
    fact that the writer didn't recognise the doctype and the user
    should either wire a real builder or rewrite the entry by hand.

    Spacing intentionally mirrors the legacy Jinja template (two
    spaces after the asset account, 43 spaces after
    ``Equity:Uncategorized``) rather than running through
    :func:`banking_pipeline.writer.format.align` — column alignment
    via ``align`` would shift the amount column relative to ledgers
    written by the old Jinja form.
    """

    asset_account = cash_account(prefix, tx.account_number, tx.currency)
    amount_str = format_amount(tx.amount)
    neg_amount_str = format_amount(-tx.amount)
    return (
        f'{tx.trade_date} * "{escape(tx.narration)}"\n'
        f"  ; TODO review — document type: {doc_type}\n"
        f"  {asset_account}  {amount_str} {tx.currency}\n"
        f"  Equity:Uncategorized                           "
        f"{neg_amount_str} {tx.currency}\n"
    )

"""Credit-line / limit-extension beancount builder.

Pictet emits a ``LIMIT_EXTENSION`` advice when the current-account
overdraft / lombard credit line is raised. The advice has no cash
effect — only the limit changes — so the rendered entry carries a
narration line and a single ``;`` audit comment, with no postings.

A dedicated builder (rather than a Jinja template) keeps the writer
free of any templating engine dependency and matches the shape of
every other doctype's renderer.
"""

from __future__ import annotations

from banking_pipeline.models import DocumentType, Transaction
from banking_pipeline.writer.format import escape

LIMIT_EXTENSION_TYPES: frozenset[DocumentType] = frozenset({
    DocumentType.LIMIT_EXTENSION,
})


def render(tx: Transaction, doc_type: DocumentType, prefix: str) -> str:
    """Render a non-cash credit-line adjustment.

    Layout::

        <trade_date> * "<narration>"
          ; non-cash event — credit line adjustment, no postings

    ``trade_date`` rather than ``booking_date`` because the advice
    records the date the limit changes — there's no cash leg to
    "book", so the trade-date semantics fit better.
    """

    return (
        f'{tx.trade_date} * "{escape(tx.narration)}"\n'
        f"  ; non-cash event — credit line adjustment, no postings\n"
    )

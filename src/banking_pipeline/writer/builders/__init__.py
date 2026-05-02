"""Per-shape beancount builders.

One module per render shape. Each builder exposes a ``render(tx, doc_type,
prefix) -> str`` callable that returns a single beancount entry (terminated
with a newline). The dispatcher in :mod:`banking_pipeline.writer.dispatch`
picks which builder to call based on the document's ``DocumentType``.

All builders share the same call signature so the dispatcher can index a
table from doctype-set to builder without further plumbing.
"""

from __future__ import annotations

from banking_pipeline.writer.builders.bond_trade import render as render_bond_trade
from banking_pipeline.writer.builders.dividend import render as render_dividend
from banking_pipeline.writer.builders.fee_advice import render as render_fee_advice
from banking_pipeline.writer.builders.fx_settlement import (
    render as render_fx_settlement,
)
from banking_pipeline.writer.builders.interest import render as render_interest
from banking_pipeline.writer.builders.internal_transfer import (
    render as render_internal_transfer,
)
from banking_pipeline.writer.builders.payment import (
    render as render_third_party_payment,
)
from banking_pipeline.writer.builders.security_trade import (
    render as render_security_trade,
)
from banking_pipeline.writer.builders.switch_trade import (
    render as render_switch_trade,
)

__all__ = [
    "render_bond_trade",
    "render_dividend",
    "render_fee_advice",
    "render_fx_settlement",
    "render_interest",
    "render_internal_transfer",
    "render_security_trade",
    "render_switch_trade",
    "render_third_party_payment",
]

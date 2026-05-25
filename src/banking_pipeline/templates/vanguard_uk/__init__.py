"""Vanguard UK (Stocks & Shares ISA) extraction templates.

Four document types carry bookable events:

  - ``vanguard_contract_note_buy`` / ``vanguard_contract_note_sell`` —
    the per-trade buys and sells (ticker as commodity, GBP).
  - ``vanguard_regular_statement`` — the cash deposits and the monthly
    cash-account interest (the trades on the same statement are owned by
    the contract notes and skipped).
  - ``vanguard_direct_debit_details`` — the quarterly platform account
    fee.

The remaining four (``vanguard_isa_declaration``,
``vanguard_cash_holding_statement``, ``vanguard_costs_and_charges``,
``vanguard_direct_debit_confirmation``) are paper-trail-only — they sit
in :data:`~banking_pipeline.models.NO_OUTPUT_DOCTYPES` and get a
:class:`~banking_pipeline.templates.vanguard_uk._common.NoOpTemplate`
so the extractor takes the explicit no-emit branch.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from banking_pipeline.templates.vanguard_uk._common import NoOpTemplate
from banking_pipeline.templates.vanguard_uk.contract_note_buy import (
    VanguardContractNoteBuyTemplate,
)
from banking_pipeline.templates.vanguard_uk.contract_note_sell import (
    VanguardContractNoteSellTemplate,
)
from banking_pipeline.templates.vanguard_uk.direct_debit_details import (
    VanguardDirectDebitDetailsTemplate,
)
from banking_pipeline.templates.vanguard_uk.regular_statement import (
    VanguardRegularStatementTemplate,
)

if TYPE_CHECKING:
    from banking_pipeline.templates import Template

VANGUARD_UK_TEMPLATES: tuple[Template, ...] = (
    VanguardContractNoteBuyTemplate(),
    VanguardContractNoteSellTemplate(),
    VanguardRegularStatementTemplate(),
    VanguardDirectDebitDetailsTemplate(),
    # Paper-trail-only doctypes — explicit no-emit (see NO_OUTPUT_DOCTYPES).
    NoOpTemplate(template_id="vanguard_uk.vanguard_isa_declaration.v1"),
    NoOpTemplate(template_id="vanguard_uk.vanguard_cash_holding_statement.v1"),
    NoOpTemplate(template_id="vanguard_uk.vanguard_costs_and_charges.v1"),
    NoOpTemplate(
        template_id="vanguard_uk.vanguard_direct_debit_confirmation.v1"
    ),
)

__all__ = [
    "VANGUARD_UK_TEMPLATES",
    "VanguardContractNoteBuyTemplate",
    "VanguardContractNoteSellTemplate",
    "VanguardDirectDebitDetailsTemplate",
    "VanguardRegularStatementTemplate",
]

"""Golden-file test for the multi-leg ``Débito de gastos`` render.

Pictet's quarterly fee advice prints a per-line breakdown of fee
components (management fees, foreign VAT, etc.) inside a ``Costes``
block. The new render shape preserves that detail by emitting one
``Expenses:<prefix>:Fees:<ccy>`` posting per item, each with the
component name as an inline beancount comment, rather than collapsing
everything into a single aggregate expense leg.

This test pins the entry shape against the canonical 2021 golden;
multi-line fee labels (used by the 2023 ES and 2026 EN fixtures) are
intentionally out of scope for this round — the breakdown helper only
handles single-line items today.
"""

from __future__ import annotations

from pathlib import Path

from banking_pipeline import beancount_writer
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.pictet import PictetDebitoDeGastosTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_debito_de_gastos_2021_renders_to_golden_beancount() -> None:
    txs = PictetDebitoDeGastosTemplate().extract(
        _load("debito_de_gastos.2021.txt")
    )
    assert len(txs) == 1, "Expected exactly one transaction from the fee fixture"
    tx = txs[0]

    # Sanity-check the new model surface populated from the document
    # before going to the writer — these are the bits the legacy fee
    # template didn't carry through.
    assert tx.title == "Débito de gastos"
    assert tx.transaction_number == "743477730"
    assert tx.booking_date is not None
    assert len(tx.fee_breakdown) == 2
    assert {item.description for item in tx.fee_breakdown} == {
        "Honorarios de gestión",
        "IVA extranjero",
    }

    classification = Classification(
        document_type=DocumentType.DEBITO_DE_GASTOS,
        confidence=0.95,
        source="rules",
        template_id="pictet.debito_de_gastos.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(tx, classification)
    golden = (FIXTURES / "debito_de_gastos.2021.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == golden, (
        "Rendered Débito de gastos entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )

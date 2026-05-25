"""Golden-file tests for the multi-transaction Vanguard ISA documents.

The parametric collector in ``tests/test_render_goldens.py`` renders one
transaction per fixture, but Vanguard's contract notes (several funds per
note) and regular statement (a deposit plus monthly interest) each yield
more than one. These tests render every extracted entry and diff the
multi-entry ``.beancount`` golden, the same join the collector would use.
"""

from __future__ import annotations

import pytest

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
from banking_pipeline.templates import TEMPLATE_REGISTRY
from tests.conftest import FIXTURES_DIR

_VANGUARD = FIXTURES_DIR / "en" / "vanguard_uk"

# (fixture stem, doctype, expected transaction count).
_CASES = [
    ("vanguard_contract_note_buy", DocumentType.VANGUARD_CONTRACT_NOTE_BUY, 2),
    ("vanguard_contract_note_sell", DocumentType.VANGUARD_CONTRACT_NOTE_SELL, 2),
    # Opening statement: deposit + three monthly interest credits.
    ("vanguard_regular_statement", DocumentType.VANGUARD_REGULAR_STATEMENT, 4),
    # Closure statement: three interest credits (incl. an "Interest
    # Payment"-labelled one) + a "One-off withdrawal" — exercises the
    # template's keyword classification of the label variants.
    ("vanguard_regular_statement.closure", DocumentType.VANGUARD_REGULAR_STATEMENT, 4),
]


@pytest.mark.parametrize(
    ("stem", "doctype", "expected_count"),
    _CASES,
    ids=[stem for stem, _, _ in _CASES],
)
def test_vanguard_multi_entry_golden(
    stem: str, doctype: DocumentType, expected_count: int
) -> None:
    template_id = f"vanguard_uk.{doctype.value}.v1"
    template = TEMPLATE_REGISTRY[template_id]

    txt = _VANGUARD / f"{stem}.txt"
    doc = RawDocument(
        path=txt, text=txt.read_text(encoding="utf-8"), page_count=1
    )
    txs = template.extract(doc)
    assert len(txs) == expected_count, (
        f"{stem}: expected {expected_count} transactions, got {len(txs)}"
    )

    classification = Classification(
        document_type=doctype,
        confidence=0.95,
        source="rules",
        template_id=template_id,
        bank=BankClassification(
            bank=BankId.VANGUARD_UK, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.ENGLISH, confidence=0.99, source="rules"
        ),
    )

    rendered = "\n".join(
        beancount_writer.render_entry(tx, classification) for tx in txs
    )
    golden = (_VANGUARD / f"{stem}.beancount").read_text(encoding="utf-8")
    assert rendered == golden

"""Golden-file test for the no-cash-effect switch_salida render.

Switch advices are structurally distinct from regular trade advices:
no cash leaves or enters a current account, the proceeds land in an
intermediate ``Assets:<prefix>:Switch:<ccy>`` holding account, and the
realised gain/loss surfaces on an elastic ``Income:<prefix>:<ISIN>:Unrealized``
posting that beancount auto-balances. This test pins the entry shape
against the canonical golden file.
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
from banking_pipeline.templates.pictet import PictetSwitchSalidaTemplate

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _load(name: str) -> RawDocument:
    path = FIXTURES / name
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_switch_salida_2021_renders_to_golden_beancount() -> None:
    txs = PictetSwitchSalidaTemplate().extract(_load("switch_salida.2021.txt"))
    assert len(txs) == 1, "Expected exactly one transaction from the switch fixture"

    classification = Classification(
        document_type=DocumentType.SWITCH_SALIDA,
        confidence=0.95,
        source="rules",
        template_id="pictet.switch_salida.v1",
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=Language.SPANISH, confidence=0.99, source="rules"
        ),
    )

    rendered = beancount_writer.render_entry(txs[0], classification)
    golden = (FIXTURES / "switch_salida.2021.beancount").read_text(
        encoding="utf-8"
    )

    assert rendered == golden, (
        "Rendered switch_salida entry doesn't match the golden file.\n"
        f"--- rendered ---\n{rendered}"
        f"--- golden ---\n{golden}"
    )

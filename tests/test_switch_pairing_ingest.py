"""Switch pairing wired through ``ingest``: shared link end-to-end + strict.

The matcher itself is unit-tested in ``test_switch_pairing``. These tests
cover the CLI wiring — that ingesting a salida + entrada *together* makes
both legs render the salida's link (and carry it in the sidecar), and that
the ``--strict`` orphan escalation fires on a non-netting in-batch pair but
not on a lone leg.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
import typer

from banking_pipeline import cli
from banking_pipeline.cli.ingest import _apply_switch_pairing
from banking_pipeline.models import DocumentType, RawDocument, Transaction
from banking_pipeline.transaction_sidecar import load_transactions

_FIXTURES = Path(__file__).parent / "fixtures" / "es" / "pictet"
_SALIDA = _FIXTURES / "switch_salida.txt"
_ENTRADA = _FIXTURES / "switch_entrada.txt"


@pytest.fixture
def txt_as_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    """Read ``.txt`` fixtures as document text through the ingest path."""

    def _load(path: Path) -> RawDocument:
        return RawDocument(
            path=path, text=path.read_text(encoding="utf-8"), page_count=1
        )

    monkeypatch.setattr("banking_pipeline.pipeline.load_pdf", _load)


def test_ingested_pair_shares_the_salida_link(
    tmp_path: Path, txt_as_pdf: None
) -> None:
    from typer.testing import CliRunner

    out = tmp_path / "switch.beancount"
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["ingest", str(_SALIDA), str(_ENTRADA), "--output", str(out)]
    )
    assert result.exit_code == 0, result.output

    rendered = out.read_text(encoding="utf-8")
    # Both legs link to the salida's number; the entrada no longer uses
    # its own as the link, but keeps it as the ``no:`` reference.
    assert rendered.count("^889193120") == 2
    assert "^889193126" not in rendered
    assert "no: 889193120" in rendered
    assert "no: 889193126" in rendered

    sidecar = load_transactions(tmp_path / "switch.transactions.jsonl")
    assert {tx.transaction_number: tx.link_id for tx in sidecar} == {
        "889193120": "889193120",
        "889193126": "889193120",
    }


def _leg(doctype: DocumentType, number: str, amount: str) -> Transaction:
    return Transaction(
        trade_date=date(2023, 8, 1),
        booking_date=date(2023, 8, 1),
        order_date=None,  # force the amount-netting path
        narration="switch leg",
        currency="EUR",
        amount=Decimal(amount),
        account_number="K-123456.001",
        transaction_number=number,
        document_type=doctype,
        source_path=Path("inbox/x.pdf"),
    )


def test_strict_fails_on_non_netting_in_batch_pair() -> None:
    txns = [
        _leg(DocumentType.SWITCH_SALIDA, "600", "100.00"),
        _leg(DocumentType.SWITCH_ENTRADA, "601", "-97.00"),
    ]
    with pytest.raises(typer.Exit) as exc:
        _apply_switch_pairing(txns, strict=True)
    assert exc.value.exit_code == 1


def test_lone_leg_only_warns_under_strict() -> None:
    txns = [_leg(DocumentType.SWITCH_SALIDA, "700", "100.00")]
    # No raise — a lone leg is a warning, not a strict failure.
    _apply_switch_pairing(txns, strict=True)
    assert txns[0].link_id is None

"""Counterparty-account-map routing tests.

Pins the new ``settings.counterparty_account_map`` behaviour
introduced for point 4 of the chart-of-accounts review: a name-based
lookup that lets the writer route the elastic counter-leg of a
third-party payment to a named account (``Income:External:Earnout:Acme``
etc.) instead of the catch-all ``:Other`` placeholder.

The map is empty by default, so every existing golden continues to
hit the ``:Other`` fallback — these tests use ``monkeypatch`` to
populate the map for the duration of each case.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from banking_pipeline import beancount_writer
from banking_pipeline.config import settings
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    Language,
    LanguageClassification,
    RawDocument,
)
from banking_pipeline.templates.pictet import (
    PictetIncomingPaymentTemplate,
    PictetPagoEntranteTemplate,
    PictetPaymentTemplate,
)

FIXTURES_EN = Path(__file__).resolve().parents[1] / "fixtures" / "en" / "pictet"
FIXTURES_ES = Path(__file__).resolve().parents[1] / "fixtures" / "es" / "pictet"


def _classification(
    doc_type: DocumentType,
    template_id: str,
    language: Language = Language.ENGLISH,
) -> Classification:
    return Classification(
        document_type=doc_type,
        confidence=0.95,
        source="rules",
        template_id=template_id,
        bank=BankClassification(
            bank=BankId.PICTET, confidence=0.99, source="rules"
        ),
        language=LanguageClassification(
            language=language, confidence=0.99, source="rules"
        ),
    )


def _load(path: Path) -> RawDocument:
    return RawDocument(
        path=path, text=path.read_text(encoding="utf-8"), page_count=1
    )


def test_incoming_payment_routes_to_mapped_counterparty(monkeypatch) -> None:
    """``Instructing party`` ``Nilufer Keskin`` is mapped to
    ``External:Donation:Keskin``; the writer's elastic counter-leg
    for the incoming wire becomes
    ``Income:External:Donation:Keskin`` instead of the catch-all
    ``Income:Pic:<portfolio>:Other``."""

    monkeypatch.setattr(
        settings, "counterparty_account_map",
        {"NILUFER KESKIN": "External:Donation:Keskin"},
    )

    txs = PictetIncomingPaymentTemplate().extract(
        _load(FIXTURES_EN / "incoming_payment.txt")
    )
    assert len(txs) == 1
    assert txs[0].counterparty_account == "External:Donation:Keskin"

    rendered = beancount_writer.render_entry(
        txs[0],
        _classification(
            DocumentType.INCOMING_PAYMENT, "pictet.incoming_payment.v1"
        ),
    )
    assert "Income:External:Donation:Keskin" in rendered
    # The catch-all ``:Other`` placeholder didn't fire.
    assert ":Other" not in rendered


def test_pago_entrante_routes_to_mapped_counterparty(monkeypatch) -> None:
    """ES counterpart — ``Ordenante`` ``SOME CORP`` is mapped to
    ``External:Earnout:SomeCorp``; the writer credits
    ``Income:External:Earnout:SomeCorp``."""

    monkeypatch.setattr(
        settings, "counterparty_account_map",
        {"SOME CORP": "External:Earnout:SomeCorp"},
    )

    txs = PictetPagoEntranteTemplate().extract(
        _load(FIXTURES_ES / "pago_entrante.txt")
    )
    assert len(txs) == 1
    assert txs[0].counterparty_account == "External:Earnout:SomeCorp"

    rendered = beancount_writer.render_entry(
        txs[0],
        _classification(
            DocumentType.PAGO_ENTRANTE,
            "pictet.pago_entrante.v1",
            language=Language.SPANISH,
        ),
    )
    assert "Income:External:Earnout:SomeCorp" in rendered


def test_payment_third_party_routes_to_mapped_counterparty(
    monkeypatch,
) -> None:
    """Outgoing third-party PAYMENT to a named counterparty —
    ``Beneficiary`` ``First Middle Lastnames`` (an external recipient,
    *not* in beneficiary_bank_map for self-to-self) maps to
    ``External:Donation:Lastnames``; the writer debits
    ``Expenses:External:Donation:Lastnames`` instead of the catch-all.

    Uses ``payment.2026.txt`` because its ``Bank`` field
    (``REVOLUT BANK UAB``) self-to-self-routes via the bank map; we
    flip it to a non-mapped bank so the third-party path fires.
    """

    monkeypatch.setattr(
        settings, "counterparty_account_map",
        {"FIRST MIDDLE LASTNAMES": "External:Donation:Lastnames"},
    )

    text = (FIXTURES_EN / "payment.2026.txt").read_text(encoding="utf-8")
    # Synthetic edit: replace Beneficiary name (the file has ``First
    # LASTNAMES`` because it's the self-to-self anonymisation; we
    # want a third-party name) AND the bank line so the bank map
    # doesn't resolve.
    text = text.replace("Beneficiary First LASTNAMES", "Beneficiary First Middle Lastnames")
    text = text.replace("Bank REVOLUT BANK UAB, SUCURSAL EN", "Bank BANCO SANTANDER")
    doc = RawDocument(
        path=FIXTURES_EN / "payment.2026.txt", text=text, page_count=1
    )

    txs = PictetPaymentTemplate().extract(doc)
    assert len(txs) == 1
    tx = txs[0]
    # bank-map missed (bank is BANCO SANTANDER) so counter_account
    # stays None; counterparty resolved instead.
    assert tx.counter_account is None
    assert tx.counterparty_account == "External:Donation:Lastnames"

    rendered = beancount_writer.render_entry(
        tx, _classification(DocumentType.PAYMENT, "pictet.payment.v1")
    )
    assert "Expenses:External:Donation:Lastnames" in rendered


def test_no_match_falls_back_to_other(monkeypatch) -> None:
    """When the map is empty (default state) or no needle matches,
    ``counterparty_account`` stays ``None`` and the writer keeps
    emitting the catch-all ``:Other`` shape — this is the contract
    the existing third-party payment goldens lock in."""

    monkeypatch.setattr(settings, "counterparty_account_map", {})

    txs = PictetIncomingPaymentTemplate().extract(
        _load(FIXTURES_EN / "incoming_payment.txt")
    )
    assert txs[0].counterparty_account is None

    rendered = beancount_writer.render_entry(
        txs[0],
        _classification(
            DocumentType.INCOMING_PAYMENT, "pictet.incoming_payment.v1"
        ),
    )
    assert "Income:Pic:" in rendered  # the catch-all still fires
    assert ":Other" in rendered

"""Tests for the ``import`` subcommand and its filing module.

``import`` is the first pipeline stage: it files raw bank PDFs into a
``<dest>/<year>/<account>/`` tree, routing bank + doctype through the shared
classifier. As in ``test_cli_scan``, PDF loading is the only part that
touches binary formats, so we monkeypatch
:func:`banking_pipeline.archive.load_pdf` to read each ``.pdf`` file's bytes
as UTF-8 text and point the source folder at copies of the real banking text
fixtures (so the live classifier recognises them).
"""

from __future__ import annotations

import shutil
import zipfile
from datetime import date
from pathlib import Path

import pytest
from typer.testing import CliRunner

from banking_pipeline import archive, cli
from banking_pipeline.models import (
    BankClassification,
    BankId,
    Classification,
    DocumentType,
    RawDocument,
)

# --- Pure helpers -----------------------------------------------------------


def _advice_tuple(
    fields: archive.ParsedFields | None,
) -> tuple[str | None, str | None, date | None, str | None]:
    """The advice-relevant fields of a parse (ignoring the statement-only
    ``as_of``), for terse equality assertions."""

    assert fields is not None
    return (fields.account, fields.reference, fields.published, fields.currency)


def test_pictet_filing_fields_on_real_fixtures(fixtures_dir: Path) -> None:
    en = (fixtures_dir / "en" / "pictet" / "subscription_notice.txt").read_text()
    assert _advice_tuple(archive.pictet_filing_fields(en)) == (
        "P-999999.999",
        "1129889269",
        date(2025, 10, 21),
        "EUR",
    )
    # A switch advice has no current-account leg → no currency.
    es = (fixtures_dir / "es" / "pictet" / "switch_salida.txt").read_text()
    assert _advice_tuple(archive.pictet_filing_fields(es)) == (
        "P-999999.999",
        "889193120",
        date(2023, 8, 1),
        None,
    )
    # On an invoice the transaction number sits on its own line, away from
    # the invoice number (80) and date — the reference must be the txn no.
    factura = (fixtures_dir / "es" / "pictet" / "factura.txt").read_text()
    assert _advice_tuple(archive.pictet_filing_fields(factura)) == (
        "P-999999.999",
        "1177002958",
        date(2026, 3, 23),
        None,
    )


def test_pictet_filing_fields_reads_interest_currency(fixtures_dir: Path) -> None:
    payment = (fixtures_dir / "en" / "pictet" / "interest_payment.txt").read_text()
    assert _advice_tuple(archive.pictet_filing_fields(payment)) == (
        "P-999999.999",
        "1180262700",
        date(2026, 3, 31),
        "GBP",
    )
    scale = (fixtures_dir / "en" / "pictet" / "interest_scale.txt").read_text()
    assert _advice_tuple(archive.pictet_filing_fields(scale)) == (
        "P-999999.999",
        "1180263452",
        date(2026, 3, 31),
        "USD",
    )


def test_pictet_filing_fields_none_when_missing() -> None:
    assert archive.pictet_filing_fields("a grocery receipt, no header") is None


def test_pictet_filing_fields_scrapes_statement_as_of(fixtures_dir: Path) -> None:
    """A monthly statement has no transaction reference but does carry an
    ``As at`` period-end date — the field statements file by."""

    text = (fixtures_dir / "en" / "pictet" / "monthly_statement.txt").read_text()
    fields = archive.pictet_filing_fields(text)
    assert fields is not None
    assert fields.account == "K-123456.789"
    assert fields.reference is None
    assert fields.as_of == date(2025, 12, 31)


def test_pictet_tax_as_of_reads_numeric_date(fixtures_dir: Path) -> None:
    """Tax reports carry a numeric as-of date: the unrealised report an ``Al
    DD.MM.YYYY`` snapshot, the realised report the ``al`` end of its ``Del …
    al …`` range."""

    realised = (fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt").read_text()
    assert archive._pictet_tax_as_of(
        realised, DocumentType.TAX_REALISED_PL
    ) == date(2023, 7, 20)
    unrealised = (
        fixtures_dir / "es" / "pictet" / "tax_unrealised_pl.txt"
    ).read_text()
    assert archive._pictet_tax_as_of(
        unrealised, DocumentType.TAX_UNREALISED_PL
    ) == date(2023, 7, 20)
    # No date-like text → None, not a raise.
    assert (
        archive._pictet_tax_as_of("no date here", DocumentType.TAX_REALISED_PL)
        is None
    )


def test_pictet_as_of_parses_both_locales() -> None:
    # English "As at <day> <Month> <year>".
    assert archive._pictet_as_of("As at 30 April 2026 (GBP)") == date(2026, 4, 30)
    # Spanish monthly "AL <day> <Mes> <year>" (case-insensitive).
    assert archive._pictet_as_of("AL 31 ENERO 2026") == date(2026, 1, 31)
    # Spanish quarterly/annual banner range — the period *end* is what "AL"
    # precedes, so the range start is ignored.
    banner = "ESTADO FINANCIERO DEL 1 JULIO 2025 AL 30 SEPTIEMBRE 2025"
    assert archive._pictet_as_of(banner) == date(2025, 9, 30)
    # An anonymised placeholder day (99) is out of range → None, not a raise.
    assert archive._pictet_as_of("As at 99 December 9999") is None
    assert archive._pictet_as_of("nothing date-like here") is None


def test_destination_for_bare_and_disambiguated() -> None:
    info = archive.FilingInfo(
        "P-999999.999", "900000001", date(2024, 6, 15), DocumentType.FACTURA
    )
    assert archive.destination_for(Path("/arch"), info) == Path(
        "/arch/2024/P-999999.999/20240615-900000001.pdf"
    )
    assert archive.destination_for(Path("/arch"), info, disambiguate=True) == Path(
        "/arch/2024/P-999999.999/20240615-900000001-Factura.pdf"
    )
    # Multi-word doctypes title-case each segment.
    fees = archive.FilingInfo(
        "P-1", "5", date(2024, 1, 1), DocumentType.DEBIT_OF_FEES
    )
    assert archive.destination_for(Path("/arch"), fees, disambiguate=True).name == (
        "20240101-5-DebitOfFees.pdf"
    )


def test_destination_for_interest_uses_currency_suffix() -> None:
    pay = archive.FilingInfo(
        "P-999999.999",
        "1180262700",
        date(2026, 3, 31),
        DocumentType.INTEREST_PAYMENT,
        currency="GBP",
    )
    assert archive.destination_for(Path("/arch"), pay, disambiguate=True).name == (
        "20260331-1180262700-Interest GBP.pdf"
    )
    scale = archive.FilingInfo(
        "P-999999.999",
        "1180264049",
        date(2026, 3, 31),
        DocumentType.INTEREST_SCALE,
        currency="HKD",
    )
    assert archive.destination_for(Path("/arch"), scale, disambiguate=True).name == (
        "20260331-1180264049-Interest scale HKD.pdf"
    )
    # Falls back to the doctype label when an interest advice has no currency.
    nocur = archive.FilingInfo(
        "P-1", "9", date(2026, 1, 1), DocumentType.INTEREST_PAYMENT
    )
    assert archive.destination_for(Path("/arch"), nocur, disambiguate=True).name == (
        "20260101-9-InterestPayment.pdf"
    )


def test_destination_for_statement_uses_reports_subfolder() -> None:
    """A periodic statement files by as-of date into ``reports/``, named in
    Pictet's ``Valuation <period> <YYYYMMDD>`` convention — not by reference,
    and never disambiguated."""

    info = archive.FilingInfo(
        account="K-999999.999",
        reference=None,
        published=date(2026, 5, 4),  # publication date is ignored for statements
        document_type=DocumentType.MONTHLY_STATEMENT,
        as_of=date(2026, 4, 30),
        period="monthly",
    )
    assert archive.destination_for(Path("/arch"), info) == Path(
        "/arch/2026/K-999999.999/reports/Valuation monthly 20260430.pdf"
    )
    # disambiguate is moot for statements — the as-of name is already unique.
    assert (
        archive.destination_for(Path("/arch"), info, disambiguate=True)
        == archive.destination_for(Path("/arch"), info)
    )
    quarterly = archive.FilingInfo(
        account="P-999999.999",
        reference=None,
        published=None,
        document_type=DocumentType.ESTADO_TRIMESTRAL,
        as_of=date(2026, 3, 31),
        period="quarterly",
    )
    assert archive.destination_for(Path("/arch"), quarterly).name == (
        "Valuation quarterly 20260331.pdf"
    )


def _classification(doc_type: DocumentType, bank: BankId | None) -> Classification:
    return Classification(
        document_type=doc_type,
        confidence=0.9,
        source="rules",
        bank=(
            BankClassification(bank=bank, confidence=0.9, source="rules")
            if bank is not None
            else None
        ),
    )


def test_filing_info_combines_classifier_and_fields(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "es" / "pictet" / "factura.txt").read_text()
    info = archive.filing_info(
        _classification(DocumentType.FACTURA, BankId.PICTET), text
    )
    assert info == archive.FilingInfo(
        "P-999999.999", "1177002958", date(2026, 3, 23), DocumentType.FACTURA
    )


def test_filing_info_statement_carries_as_of_and_period(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "en" / "pictet" / "monthly_statement.txt").read_text()
    info = archive.filing_info(
        _classification(DocumentType.MONTHLY_STATEMENT, BankId.PICTET), text
    )
    assert info is not None
    assert info.is_statement
    assert (info.account, info.as_of, info.period, info.reference) == (
        "K-123456.789",
        date(2025, 12, 31),
        "monthly",
        None,
    )


def test_filing_info_tax_report_carries_as_of_and_stem(fixtures_dir: Path) -> None:
    """A Spanish IRPF tax report files by its as-of date with a filename
    stem-prefix (``Realised PL`` / ``Unrealised PL`` / ``Fiscal statement``)
    — no account, no reference."""

    text = (fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt").read_text()
    info = archive.filing_info(
        _classification(DocumentType.TAX_REALISED_PL, BankId.PICTET), text
    )
    assert info is not None
    assert info.is_tax_report
    assert not info.is_statement
    assert (info.account, info.reference, info.as_of, info.tax_label) == (
        "",
        None,
        date(2023, 7, 20),
        "Realised PL",
    )
    # The comprehensive annual statement files by its full-year ``al``-end
    # date under the ``Fiscal statement`` stem.
    stmt = (fixtures_dir / "es" / "pictet" / "tax_fiscal_statement.txt").read_text()
    info = archive.filing_info(
        _classification(DocumentType.TAX_FISCAL_STATEMENT, BankId.PICTET), stmt
    )
    assert info is not None
    assert info.is_tax_report
    assert (info.as_of, info.tax_label) == (date(2024, 12, 31), "Fiscal statement")


def test_effective_date_from_filename() -> None:
    cases = {
        "0173837-Tax+-+Realised+P%2FL+report-20231005.pdf": date(2023, 10, 5),
        "0173837.001-Tax+-+Unrealised+P%2FL+report-20231211.pdf": date(2023, 12, 11),
        # a ``-(N)`` re-cut suffix is tolerated
        "0173837-Tax+-+Realised+P%2FL+report-20250422-(1).pdf": date(2025, 4, 22),
        # canonical archived names carry the date the same way
        "Realised PL 20231229.pdf": date(2023, 12, 29),
        "Fiscal statement 20241231.pdf": date(2024, 12, 31),
        # dateless / invalid → None (fall back to content)
        "Tax - Realised PL report-.pdf": None,
        "Realised PL 20239999.pdf": None,
    }
    for name, expected in cases.items():
        assert archive._effective_date_from_filename(name) == expected, name


def test_filing_info_tax_report_prefers_effective_date(fixtures_dir: Path) -> None:
    """A tax report is dated by the effective date in its filename, not the
    (possibly stale) content fiscal date. The realised fixture's content as-of
    is 2023-07-20; a source filename dated 2023-10-05 wins."""

    text = (fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt").read_text()
    info = archive.filing_info(
        _classification(DocumentType.TAX_REALISED_PL, BankId.PICTET),
        text,
        source_name="0173837-Tax+-+Realised+P%2FL+report-20231005.pdf",
    )
    assert info is not None
    assert info.as_of == date(2023, 10, 5)  # effective (filename) date wins
    assert archive.destination_for(Path("/arch"), info).name == (
        "Realised PL 20231005.pdf"
    )


def test_filing_info_tax_report_falls_back_to_content_date(fixtures_dir: Path) -> None:
    """When the filename carries no date (or no source name is passed), the
    content fiscal date is used."""

    text = (fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt").read_text()
    cls = _classification(DocumentType.TAX_REALISED_PL, BankId.PICTET)
    # no source_name at all
    assert archive.filing_info(cls, text).as_of == date(2023, 7, 20)  # type: ignore[union-attr]
    # dateless source_name
    info = archive.filing_info(
        cls, text, source_name="Tax - Realised PL report-.pdf"
    )
    assert info is not None and info.as_of == date(2023, 7, 20)


def test_filing_info_logs_effective_content_date_mismatch(fixtures_dir: Path) -> None:
    """A filename/content date disagreement (the stale-label signal) is logged;
    agreement is silent."""
    from structlog.testing import capture_logs

    text = (fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt").read_text()
    cls = _classification(DocumentType.TAX_REALISED_PL, BankId.PICTET)
    ev = "archive.tax_report_date_mismatch"
    # content 2023-07-20 vs filename 2023-10-05 → warn
    with capture_logs() as logs:
        archive.filing_info(
            cls, text, source_name="0173837-Tax+-+Realised+P%2FL+report-20231005.pdf"
        )
    warns = [e for e in logs if e.get("event") == ev]
    assert len(warns) == 1 and warns[0]["used"] == "effective"
    assert warns[0]["effective_date"] == date(2023, 10, 5)
    # filename matches content → no warning
    with capture_logs() as logs2:
        archive.filing_info(
            cls, text, source_name="0173837-Tax+-+Realised+P%2FL+report-20230720.pdf"
        )
    assert not [e for e in logs2 if e.get("event") == ev]


def test_filing_info_none_when_tax_report_has_no_date() -> None:
    """A tax-report classification with no scrapable as-of date is left
    unfiled rather than filed under a wrong name."""

    assert (
        archive.filing_info(
            _classification(DocumentType.TAX_UNREALISED_PL, BankId.PICTET),
            "SIMULACIÓN FISCAL but no numeric date anywhere",
        )
        is None
    )


def test_filing_info_tax_authority_filings(fixtures_dir: Path) -> None:
    """ETE / Modelo 720 / the UK report file by a per-kind fixed as-of date
    scraped from a prose period end: ETE + 720 → 31 Dec of the year; the UK
    report → 5 Apr (UK tax-year end)."""

    cases = [
        ("es/pictet/declaracion_ete.txt", DocumentType.DECLARACION_ETE,
         date(2024, 12, 31), "ETE"),
        ("es/pictet/modelo_720.txt", DocumentType.MODELO_720,
         date(2024, 12, 31), "Modelo 720"),
        ("en/pictet/income_capital_gains_uk.txt",
         DocumentType.INCOME_CAPITAL_GAINS_UK, date(2025, 4, 5),
         "Income and capital gains UK"),
    ]
    for rel, doc_type, as_of, stem in cases:
        text = (fixtures_dir / rel).read_text()
        info = archive.filing_info(_classification(doc_type, BankId.PICTET), text)
        assert info is not None, rel
        assert info.is_tax_report
        assert (info.as_of, info.tax_label) == (as_of, stem), rel
        assert archive.destination_for(Path("/arch"), info) == Path(
            f"/arch/{as_of:%Y}/tax/{stem} {as_of:%Y%m%d}.pdf"
        )


def test_destination_for_tax_report_uses_tax_subfolder() -> None:
    """A tax report files into ``<as-of-year>/tax/`` named ``<stem>
    <YYYYMMDD>.pdf`` — no account segment, never disambiguated."""

    info = archive.FilingInfo(
        account="",
        reference=None,
        published=None,
        document_type=DocumentType.TAX_UNREALISED_PL,
        as_of=date(2026, 4, 5),
        tax_label="Unrealised PL",
    )
    assert archive.destination_for(Path("/arch"), info) == Path(
        "/arch/2026/tax/Unrealised PL 20260405.pdf"
    )
    # disambiguate is moot — the as-of name is already unique.
    assert archive.destination_for(
        Path("/arch"), info, disambiguate=True
    ) == archive.destination_for(Path("/arch"), info)
    realised = archive.FilingInfo(
        account="",
        reference=None,
        published=None,
        document_type=DocumentType.TAX_REALISED_PL,
        as_of=date(2023, 12, 31),
        tax_label="Realised PL",
    )
    assert archive.destination_for(Path("/arch"), realised).name == (
        "Realised PL 20231231.pdf"
    )
    # The annual statement uses the ``Fiscal statement`` stem (no ``PL``).
    stmt = archive.FilingInfo(
        account="",
        reference=None,
        published=None,
        document_type=DocumentType.TAX_FISCAL_STATEMENT,
        as_of=date(2024, 12, 31),
        tax_label="Fiscal statement",
    )
    assert archive.destination_for(Path("/arch"), stmt).name == (
        "Fiscal statement 20241231.pdf"
    )


def test_filing_info_none_for_bank_without_parser(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "es" / "pictet" / "factura.txt").read_text()
    # Vanguard is classified but has no filing parser yet → left unfiled.
    assert (
        archive.filing_info(
            _classification(DocumentType.FACTURA, BankId.VANGUARD_UK), text
        )
        is None
    )


def test_filing_info_none_when_no_bank(fixtures_dir: Path) -> None:
    text = (fixtures_dir / "es" / "pictet" / "factura.txt").read_text()
    assert archive.filing_info(_classification(DocumentType.UNKNOWN, None), text) is None


# --- CLI --------------------------------------------------------------------


@pytest.fixture
def import_tree(tmp_path: Path, fixtures_dir: Path) -> Path:
    """A flat download folder with one EN Pictet PDF, one ES Pictet PDF, and
    a non-bank PDF the classifier can't place."""

    src = tmp_path / "downloads"
    src.mkdir()
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "subscription_notice.txt", src / "a.pdf"
    )
    shutil.copy(fixtures_dir / "es" / "pictet" / "switch_salida.txt", src / "b.pdf")
    (src / "junk.pdf").write_text("a coffee shop receipt, total 4.50", encoding="utf-8")
    return src


@pytest.fixture(autouse=True)
def _fake_pdf_loader(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route the filing module's ``load_pdf`` to a text reader; empty files
    raise so the report-and-continue ``error`` path is exercised."""

    def fake(path: Path) -> RawDocument:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise ValueError("empty document")
        return RawDocument(path=path, text=text, page_count=1)

    monkeypatch.setattr(archive, "load_pdf", fake)


def test_import_files_recognised_pdfs(import_tree: Path, tmp_path: Path) -> None:
    dest = tmp_path / "archive"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(import_tree), str(dest)])

    assert result.exit_code == 0, result.output
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()
    assert (dest / "2023" / "P-999999.999" / "20230801-889193120.pdf").is_file()
    # Recognised originals are gone (moved, not copied); the unknown one stays.
    assert not (import_tree / "a.pdf").exists()
    assert not (import_tree / "b.pdf").exists()
    assert (import_tree / "junk.pdf").exists()
    assert "no match" in result.output
    assert "2 filed, 0 skipped, 1 unmatched, 0 error(s)." in result.output


def test_import_dry_run_moves_nothing(import_tree: Path, tmp_path: Path) -> None:
    dest = tmp_path / "archive"
    runner = CliRunner()
    result = runner.invoke(
        cli.app, ["import", str(import_tree), str(dest), "--dry-run"]
    )

    assert result.exit_code == 0, result.output
    assert not dest.exists()
    assert (import_tree / "a.pdf").exists()
    assert (import_tree / "b.pdf").exists()
    assert "would file" in result.output
    assert "[dry-run]" in result.output


def test_import_skips_existing_destination(
    import_tree: Path, tmp_path: Path
) -> None:
    dest = tmp_path / "archive"
    target = dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf"
    target.parent.mkdir(parents=True)
    target.write_text("already filed", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(import_tree), str(dest)])

    assert result.exit_code == 0, result.output
    assert target.read_text() == "already filed"  # untouched
    assert (import_tree / "a.pdf").exists()  # source left in place
    assert "skip (exists)" in result.output
    assert "1 filed, 1 skipped, 1 unmatched, 0 error(s)." in result.output


def test_import_disambiguates_shared_reference(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """An invoice and its debit-of-fees advice share a transaction number;
    both file with a doctype suffix, while a third unique doc stays bare."""

    src = tmp_path / "downloads"
    src.mkdir()
    factura = (fixtures_dir / "es" / "pictet" / "factura.txt").read_text()
    debito = (fixtures_dir / "es" / "pictet" / "debito_de_gastos.txt").read_text()
    # Force a shared (date, reference) so the bare name would collide.
    factura = factura.replace("1177002958", "900000001").replace(
        "23.03.2026", "15.06.2024"
    )
    debito = debito.replace("855093717", "900000001").replace(
        "20.03.2023", "15.06.2024"
    )
    (src / "factura.pdf").write_text(factura, encoding="utf-8")
    (src / "debito.pdf").write_text(debito, encoding="utf-8")
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "subscription_notice.txt", src / "sub.pdf"
    )

    dest = tmp_path / "archive"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(src), str(dest)])

    assert result.exit_code == 0, result.output
    base = dest / "2024" / "P-999999.999"
    assert (base / "20240615-900000001-Factura.pdf").is_file()
    assert (base / "20240615-900000001-DebitoDeGastos.pdf").is_file()
    # The colliding bare name is never written.
    assert not (base / "20240615-900000001.pdf").exists()
    # The unique third document keeps the bare name.
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()
    assert "3 filed, 0 skipped, 0 unmatched, 0 error(s)." in result.output


def test_import_interest_advices_get_currency_suffix(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """Interest payment + scale are always filed with a currency suffix,
    matching the archive convention (``Interest GBP`` / ``Interest scale
    USD``), even though their references differ (no collision)."""

    src = tmp_path / "downloads"
    src.mkdir()
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "interest_payment.txt", src / "pay.pdf"
    )
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "interest_scale.txt", src / "scale.pdf"
    )
    dest = tmp_path / "archive"

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(src), str(dest)])

    assert result.exit_code == 0, result.output
    base = dest / "2026" / "P-999999.999"
    assert (base / "20260331-1180262700-Interest GBP.pdf").is_file()
    assert (base / "20260331-1180263452-Interest scale USD.pdf").is_file()
    # Never the bare names.
    assert not (base / "20260331-1180262700.pdf").exists()
    assert not (base / "20260331-1180263452.pdf").exists()
    assert "2 filed, 0 skipped, 0 unmatched, 0 error(s)." in result.output


def test_import_files_statement_into_reports_subfolder(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """A periodic valuation statement (no transaction reference) files by its
    as-of date into the account's ``reports/`` subfolder, alongside an advice
    filed the usual way in the same batch."""

    src = tmp_path / "downloads"
    src.mkdir()
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "monthly_statement.txt", src / "stmt.pdf"
    )
    shutil.copy(
        fixtures_dir / "en" / "pictet" / "subscription_notice.txt", src / "sub.pdf"
    )
    dest = tmp_path / "archive"

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(src), str(dest)])

    assert result.exit_code == 0, result.output
    assert (
        dest / "2025" / "K-123456.789" / "reports" / "Valuation monthly 20251231.pdf"
    ).is_file()
    # The advice in the same batch still files the reference way.
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()
    assert not (src / "stmt.pdf").exists()
    assert "2 filed, 0 skipped, 0 unmatched, 0 error(s)." in result.output


def test_import_files_tax_reports_into_tax_subfolder(
    tmp_path: Path, fixtures_dir: Path
) -> None:
    """The Realised / Unrealised IRPF reports (no account, no reference) file
    by their as-of date into ``<year>/tax/``, and a same-day duplicate is
    skipped rather than overwriting the first filed copy."""

    src = tmp_path / "downloads"
    src.mkdir()
    shutil.copy(
        fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt", src / "a.pdf"
    )
    shutil.copy(
        fixtures_dir / "es" / "pictet" / "tax_unrealised_pl.txt", src / "b.pdf"
    )
    dest = tmp_path / "archive"
    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(src), str(dest)])

    assert result.exit_code == 0, result.output
    realised = dest / "2023" / "tax" / "Realised PL 20230720.pdf"
    unrealised = dest / "2023" / "tax" / "Unrealised PL 20230720.pdf"
    assert realised.is_file()
    assert unrealised.is_file()
    assert "2 filed, 0 skipped, 0 unmatched, 0 error(s)." in result.output

    # A second same-day download (the portal re-cuts a report later the same
    # day) resolves to the same name → skip, keeping the first filed copy.
    realised.write_text("first filed", encoding="utf-8")
    src2 = tmp_path / "downloads2"
    src2.mkdir()
    shutil.copy(
        fixtures_dir / "es" / "pictet" / "tax_realised_pl.txt", src2 / "again.pdf"
    )
    result2 = runner.invoke(cli.app, ["import", str(src2), str(dest)])
    assert result2.exit_code == 0, result2.output
    assert realised.read_text() == "first filed"  # untouched
    assert (src2 / "again.pdf").exists()  # source left in place
    assert "1 skipped" in result2.output


def test_import_uses_config_defaults(
    import_tree: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dest = tmp_path / "archive"
    monkeypatch.setattr(cli.ingest.settings, "import_source_glob", None)
    monkeypatch.setattr(cli.ingest.settings, "import_source_dir", import_tree)
    monkeypatch.setattr(cli.ingest.settings, "import_archive_dir", dest)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import"])

    assert result.exit_code == 0, result.output
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()


def test_import_requires_source_and_dest(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.ingest.settings, "import_source_glob", None)
    monkeypatch.setattr(cli.ingest.settings, "import_source_dir", None)
    monkeypatch.setattr(cli.ingest.settings, "import_archive_dir", None)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import"])

    assert result.exit_code == 2
    assert "an import source and archive are required" in result.output


def test_import_reports_unreadable_pdf_and_continues(
    import_tree: Path, tmp_path: Path
) -> None:
    (import_tree / "broken.pdf").write_bytes(b"")
    dest = tmp_path / "archive"

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(import_tree), str(dest)])

    assert result.exit_code == 0, result.output
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()
    assert "error:" in result.output
    assert "1 error(s)." in result.output


# --- Zip source -------------------------------------------------------------


def _make_zip(path: Path, entries: dict[str, str]) -> Path:
    with zipfile.ZipFile(path, "w") as zf:
        for name, text in entries.items():
            zf.writestr(name, text)
    return path


def test_source_pdfs_zip_extracts_then_cleans_up(tmp_path: Path) -> None:
    zip_path = _make_zip(
        tmp_path / "batch.zip",
        {"a.pdf": "one", "b.PDF": "two", "notes.txt": "ignored"},
    )

    with archive.source_pdfs([zip_path]) as pdfs:
        # Only the two PDF members (case-insensitive extension); .txt ignored.
        assert len(pdfs) == 2
        assert all(p.is_file() for p in pdfs)
        captured = pdfs[0]

    # The temp extraction is cleaned up when the context exits.
    assert not captured.exists()


def test_expand_source_glob_matches_and_sorts(tmp_path: Path) -> None:
    (tmp_path / "files-1.zip").write_text("x", encoding="utf-8")
    (tmp_path / "files-2.zip").write_text("y", encoding="utf-8")
    (tmp_path / "other.zip").write_text("z", encoding="utf-8")

    assert archive.expand_source_glob(str(tmp_path / "files-*.zip")) == [
        tmp_path / "files-1.zip",
        tmp_path / "files-2.zip",
    ]
    assert archive.expand_source_glob(str(tmp_path / "none-*.zip")) == []


def test_import_from_zip(tmp_path: Path, fixtures_dir: Path) -> None:
    sub = (fixtures_dir / "en" / "pictet" / "subscription_notice.txt").read_text()
    switch = (fixtures_dir / "es" / "pictet" / "switch_salida.txt").read_text()
    zip_path = _make_zip(
        tmp_path / "files.zip",
        {
            "sub.pdf": sub,
            # Uppercase extension confirms the case-insensitive member match.
            "B.PDF": switch,
            "junk.pdf": "a coffee shop receipt, total 4.50",
            "notes.txt": "should be ignored — not a PDF member",
        },
    )
    dest = tmp_path / "archive"

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(zip_path), str(dest)])

    assert result.exit_code == 0, result.output
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()
    assert (dest / "2023" / "P-999999.999" / "20230801-889193120.pdf").is_file()
    # The zip itself is read-only input — left in place.
    assert zip_path.exists()
    assert "2 filed, 0 skipped, 1 unmatched, 0 error(s)." in result.output


def test_import_rejects_non_zip_file(tmp_path: Path) -> None:
    bogus = tmp_path / "statement.pdf"
    bogus.write_text("a single pdf, not a folder or zip", encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import", str(bogus), str(tmp_path / "arch")])

    assert result.exit_code == 2
    assert "must be an existing directory or a .zip file" in result.output


def test_import_uses_source_glob_config(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sub = (fixtures_dir / "en" / "pictet" / "subscription_notice.txt").read_text()
    switch = (fixtures_dir / "es" / "pictet" / "switch_salida.txt").read_text()
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    _make_zip(downloads / "files-1.zip", {"a.pdf": sub})
    _make_zip(downloads / "files-2.zip", {"b.pdf": switch})
    # A zip the glob doesn't match is never opened.
    _make_zip(downloads / "other.zip", {"c.pdf": "ignored receipt"})
    dest = tmp_path / "archive"

    monkeypatch.setattr(
        cli.ingest.settings, "import_source_glob", str(downloads / "files-*.zip")
    )
    monkeypatch.setattr(cli.ingest.settings, "import_source_dir", None)
    monkeypatch.setattr(cli.ingest.settings, "import_archive_dir", dest)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import"])

    assert result.exit_code == 0, result.output
    assert (dest / "2025" / "P-999999.999" / "20251021-1129889269.pdf").is_file()
    assert (dest / "2023" / "P-999999.999" / "20230801-889193120.pdf").is_file()
    assert "2 filed, 0 skipped, 0 unmatched, 0 error(s)." in result.output


def test_import_glob_disambiguates_reference_across_zips(
    tmp_path: Path, fixtures_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A shared reference split across two zips is still one batch, so both
    documents file with a doctype suffix."""

    factura = (
        (fixtures_dir / "es" / "pictet" / "factura.txt")
        .read_text()
        .replace("1177002958", "900000001")
        .replace("23.03.2026", "15.06.2024")
    )
    debito = (
        (fixtures_dir / "es" / "pictet" / "debito_de_gastos.txt")
        .read_text()
        .replace("855093717", "900000001")
        .replace("20.03.2023", "15.06.2024")
    )
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    _make_zip(downloads / "files-a.zip", {"factura.pdf": factura})
    _make_zip(downloads / "files-b.zip", {"debito.pdf": debito})
    dest = tmp_path / "archive"

    monkeypatch.setattr(
        cli.ingest.settings, "import_source_glob", str(downloads / "files-*.zip")
    )
    monkeypatch.setattr(cli.ingest.settings, "import_source_dir", None)
    monkeypatch.setattr(cli.ingest.settings, "import_archive_dir", dest)

    runner = CliRunner()
    result = runner.invoke(cli.app, ["import"])

    assert result.exit_code == 0, result.output
    base = dest / "2024" / "P-999999.999"
    assert (base / "20240615-900000001-Factura.pdf").is_file()
    assert (base / "20240615-900000001-DebitoDeGastos.pdf").is_file()
    assert not (base / "20240615-900000001.pdf").exists()
    assert "2 filed, 0 skipped, 0 unmatched, 0 error(s)." in result.output

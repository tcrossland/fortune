"""File raw bank-statement PDFs into a dated archive tree.

This is the first stage of the pipeline: it takes a folder (or a ``.zip``,
the shape the bank's bulk download arrives in) of freshly downloaded PDFs
and files each into the archive tree so the later ingest / report stages
have a stable, organised source tree to read from. Three filing shapes:

* **Transaction advices** (buys, sells, FX, interest, …) file by their
  per-document reference: ``<root>/<year>/<account>/<YYYYMMDD>-<reference>.pdf``.
* **Periodic valuation statements** (monthly / quarterly / annual, both
  locales) carry no transaction reference, so they file by their as-of
  (period-end) date into the account's ``reports/`` subfolder:
  ``<root>/<as-of-year>/<account>/reports/Valuation <period> <YYYYMMDD>.pdf``
  — the convention the ingest / valuation stages already glob for.
* **Spanish IRPF tax reports** (Realised / Unrealised P&L, and the
  comprehensive annual fiscal statement) carry neither an account header nor
  a reference, so they file by their numeric as-of date into a per-year
  ``tax/`` folder: ``<root>/<as-of-year>/tax/<stem> <YYYYMMDD>.pdf`` where
  ``<stem>`` is ``Realised PL`` / ``Unrealised PL`` / ``Fiscal statement``.
  These are an archive-only reference source — never ingested into beancount,
  never fed to the UK-tax pipeline (see ``prune_tax_reports`` for the
  retention command that trims the daily volume).

Bank and document type come from the shared :class:`LayeredClassifier` (the
same language → bank → doctype classifier the rest of the pipeline uses), so
filing stays consistent with classification and a new bank needs no
bank-detection code here. Only the three filing fields — account number,
per-document reference and publication date — are scraped with a
bank-specific parser, keyed by :class:`BankId` in :data:`FIELD_PARSERS`
(Pictet, both locales, today). A second bank is a data-only addition: add a
parser returning ``None`` when the text isn't its document.

The standalone ``rename.py`` precursor used PyMuPDF (``fitz``), which is
AGPL-3.0 and banned here (see ``CLAUDE.md``); this uses the pypdfium2
extractor like everything else.

Two source PDFs can legitimately share a reference — e.g. a Spanish-branch
invoice (``factura``) and the corresponding debit-of-fees advice both quote
the same ``N° de transacción``. When that happens within a single import
batch the bare ``<date>-<ref>.pdf`` name would collide, so every member of
the colliding group is filed with a title-cased doctype suffix
(``<date>-<ref>-<DocType>.pdf``) instead. A reference that's unique in the
batch keeps the bare name.

Interest advices are a special case: Pictet issues an interest payment and
an interest scale per currency that share a transaction number, so they
always collide — and they're filed with a currency-bearing suffix
(``-Interest <CCY>`` / ``-Interest scale <CCY>``) rather than the plain
doctype, applied to every interest advice so the currency is always in the
name.
"""

from __future__ import annotations

import fnmatch
import glob
import os
import re
import shutil
import tempfile
import zipfile
from collections import defaultdict
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

from banking_pipeline.classifiers import LayeredClassifier
from banking_pipeline.extractors import load_pdf
from banking_pipeline.models import BankId, Classification, DocumentType


@dataclass(frozen=True)
class ParsedFields:
    """The raw filing fields a bank parser scrapes from a document's text.

    A parser returns ``None`` only when the text isn't its bank's document at
    all (no account header); every other field is optional, because different
    document shapes carry different ones. A transaction advice has a
    ``reference`` + ``published`` date; a periodic valuation statement has an
    ``as_of`` (period-end) date instead. :func:`filing_info` validates that
    the right fields are present for the document's shape. ``currency`` is the
    account currency when discernible (used in interest suffixes).
    """

    account: str | None
    reference: str | None = None
    published: date | None = None
    currency: str | None = None
    as_of: date | None = None


@dataclass(frozen=True)
class FilingInfo:
    """Everything needed to file one document into the archive tree.

    Three filing shapes share this struct. A **transaction advice** carries a
    ``reference`` + ``published`` date and files to
    ``<year>/<account>/<date>-<reference>.pdf``. A **periodic valuation
    statement** carries an ``as_of`` date + ``period`` instead and files to
    ``<year>/<account>/reports/Valuation <period> <as-of>.pdf``. A **Spanish
    IRPF tax report** carries an ``as_of`` date + a ``tax_label`` (the
    filename stem-prefix: ``Realised PL`` / ``Unrealised PL`` / ``Fiscal
    statement``) and no account, filing to
    ``<year>/tax/<tax_label> <as-of>.pdf``. :attr:`is_tax_report` and
    :attr:`is_statement` tell the three apart.
    """

    account: str
    reference: str | None
    published: date | None
    document_type: DocumentType
    currency: str | None = None
    as_of: date | None = None
    period: str | None = None
    tax_label: str | None = None

    @property
    def is_tax_report(self) -> bool:
        """True for a Spanish IRPF tax report (filed by as-of date into
        ``<year>/tax/``, no account segment)."""

        return self.tax_label is not None

    @property
    def is_statement(self) -> bool:
        """True for a periodic valuation statement (filed by as-of date into
        ``reports/``), false for a transaction advice or a tax report."""

        return self.period is not None


# Pictet prints the account on one header line and the per-document
# reference + publication date nearby, in the document's own language. The
# patterns tolerate both locales (and a degree sign / accent rendered as any
# single char by the extractor): ``N.`` matches ``N°``, ``transacci.n``
# matches ``transacción``, ``publicaci.n`` matches ``publicación``. Searched
# independently against the whole text, so line-grouping and ordering don't
# matter — on an invoice the reference sits on its own line, away from the
# invoice number and date, and these still pick out the right field.
_PICTET_ACCOUNT = re.compile(r"(?:Account no\.|N.\s*de cuenta)\s*:\s*(\S+)")
_PICTET_REFERENCE = re.compile(
    r"(?:Transaction no\.|N.\s*de transacci.n)\s*:\s*(\d+)"
)
_PICTET_PUBLISHED = re.compile(
    r"(?:Publication date|Fecha de publicaci.n)\s*:\s*"
    r"(\d{2})\.(\d{2})\.(\d{4})"
)
# The account currency, read from the current-account leg the advice quotes
# (``Current account <acct>.00.<CCY>/Ordinary``) — present on both interest
# locales and other current-account advices. ``None`` when absent (most
# security advices); only interest filenames use it.
_PICTET_CURRENCY = re.compile(r"Current account\b[^\n]*?\.00\.([A-Z]{3})/")

# Periodic valuation statements print their period-end (``as-of``) date in
# prose: English ``As at <day> <Month> <year>`` (also on the cover as ``As at
# <day> <Month> <year> (CCY)``), Spanish ``AL <day> <Mes> <year>``. The
# Spanish pattern also catches the *end* of a quarterly/annual banner range
# (``ESTADO FINANCIERO DEL <start> AL <end>``): ``AL`` only precedes the end
# date, so the first match is the period end. Month names map per locale.
_EN_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
)
_ES_MONTH_NAMES = (
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
)
_EN_MONTHS = {name: n for n, name in enumerate(_EN_MONTH_NAMES, start=1)}
_ES_MONTHS = {name: n for n, name in enumerate(_ES_MONTH_NAMES, start=1)}
_EN_AS_AT = re.compile(r"As at\s+(\d{1,2})\s+([A-Za-z]+)\s+(\d{4})", re.IGNORECASE)
_ES_AL = re.compile(r"\bAL\s+(\d{1,2})\s+([A-Za-zÁÉÍÓÚáéíóúñ]+)\s+(\d{4})", re.IGNORECASE)


def _pictet_as_of(text: str) -> date | None:
    """The period-end date of a Pictet valuation statement, or ``None``.

    Tries the English ``As at`` phrasing first, then the Spanish ``AL``; an
    unrecognised month name or an out-of-range day (e.g. an anonymised ``99``
    placeholder) yields ``None`` rather than raising."""

    en = _EN_AS_AT.search(text)
    if en is not None:
        day, name, year = en.group(1, 2, 3)
        month = _EN_MONTHS.get(name.lower())
    else:
        es = _ES_AL.search(text)
        if es is None:
            return None
        day, name, year = es.group(1, 2, 3)
        month = _ES_MONTHS.get(name.lower())
    if month is None:
        return None
    try:
        return date(int(year), month, int(day))
    except ValueError:
        return None


# Spanish IRPF tax reports carry their as-of date *numerically* (distinct
# from the prose ``AL 30 junio 2026`` a valuation statement uses): the
# unrealised report prints a point-in-time ``Al DD.MM.YYYY`` snapshot date,
# the realised report a cumulative ``Del DD.MM.YYYY al DD.MM.YYYY`` range
# whose ``al`` end is the as-of date. Both anchor at the top of the document,
# so the first match is the report's own date.
_TAX_UNREALISED_AS_OF = re.compile(r"\bAl\s+(\d{2})\.(\d{2})\.(\d{4})")
_TAX_REALISED_AS_OF = re.compile(
    r"\bDel\s+\d{2}\.\d{2}\.\d{4}\s+al\s+(\d{2})\.(\d{2})\.(\d{4})"
)
# The annual tax-authority filings state their period end in prose, in two
# languages, and their as-of month-day is fixed by the filing kind: ETE /
# Modelo 720 close on 31 Dec (``31 Diciembre|December <year>``); the UK income
# & capital-gains report on 5 Apr (the UK tax-year end, ``5 April <year>``).
# Only the year is scraped; the month-day is supplied per doctype.
_TAX_YEAR_END = re.compile(r"\b31\s+(?:Diciembre|December)\s+(\d{4})\b", re.I)
_UK_TAX_YEAR_END = re.compile(r"\b5\s+April\s+(\d{4})\b")
_TAX_FIXED_ASOF: dict[DocumentType, tuple[re.Pattern[str], int, int]] = {
    DocumentType.DECLARACION_ETE: (_TAX_YEAR_END, 12, 31),
    DocumentType.MODELO_720: (_TAX_YEAR_END, 12, 31),
    DocumentType.INCOME_CAPITAL_GAINS_UK: (_UK_TAX_YEAR_END, 4, 5),
}


def _pictet_tax_as_of(text: str, doc_type: DocumentType) -> date | None:
    """The as-of date of a Pictet tax report / annual filing, or ``None``.

    Realised reports and the annual fiscal statement file by the ``al`` end of
    their ``Del … al …`` range (the statement's is the full year, ``Del 01.01
    … al 31.12``); unrealised reports by their ``Al …`` snapshot date. The
    annual tax-authority filings scrape only the year from a prose period end
    and pin the month-day per kind (ETE / Modelo 720 → 31 Dec; UK → 5 Apr). An
    out-of-range placeholder day yields ``None`` rather than raising."""

    fixed = _TAX_FIXED_ASOF.get(doc_type)
    if fixed is not None:
        pattern, month, day = fixed
        match = pattern.search(text)
        if match is None:
            return None
        try:
            return date(int(match.group(1)), month, day)
        except ValueError:
            return None

    range_doctypes = (
        DocumentType.TAX_REALISED_PL,
        DocumentType.TAX_FISCAL_STATEMENT,
    )
    pattern = (
        _TAX_REALISED_AS_OF
        if doc_type in range_doctypes
        else _TAX_UNREALISED_AS_OF
    )
    match = pattern.search(text)
    if match is None:
        return None
    day_s, month_s, year_s = match.group(1, 2, 3)
    try:
        return date(int(year_s), int(month_s), int(day_s))
    except ValueError:
        return None


def pictet_filing_fields(text: str) -> ParsedFields | None:
    """Scrape Pictet's filing fields from ``text``, or ``None`` when the text
    carries no account header (not a Pictet document).

    Every field but the account is optional: a transaction advice yields a
    ``reference`` + ``published`` date; a periodic valuation statement yields
    an ``as_of`` date instead. :func:`filing_info` enforces which are required
    for the document's shape."""

    account = _PICTET_ACCOUNT.search(text)
    if account is None:
        return None
    reference = _PICTET_REFERENCE.search(text)
    published = _PICTET_PUBLISHED.search(text)
    currency = _PICTET_CURRENCY.search(text)
    pub_date: date | None = None
    if published is not None:
        day, month, year = published.group(1, 2, 3)
        pub_date = date(int(year), int(month), int(day))
    return ParsedFields(
        account=account.group(1),
        reference=reference.group(1) if reference is not None else None,
        published=pub_date,
        currency=currency.group(1) if currency is not None else None,
        as_of=_pictet_as_of(text),
    )


# Filing-field parsers keyed by the classifier's bank verdict. Add a bank by
# registering its parser here (and the classifier already knowing the bank).
FIELD_PARSERS: dict[BankId, Callable[[str], ParsedFields | None]] = {
    BankId.PICTET: pictet_filing_fields,
}

# Periodic valuation statements file differently from transaction advices:
# they carry no transaction reference, so they're keyed on the statement's
# as-of (period-end) date and filed into a ``reports/`` subfolder, named in
# Pictet's own convention — ``Valuation <period> <YYYYMMDD>.pdf``. Maps each
# periodic-statement doctype (both locales) to its period word.
_STATEMENT_PERIODS: dict[DocumentType, str] = {
    DocumentType.MONTHLY_STATEMENT: "monthly",
    DocumentType.QUARTERLY_STATEMENT: "quarterly",
    DocumentType.ANNUAL_STATEMENT: "annual",
    DocumentType.ESTADO_MENSUAL: "monthly",
    DocumentType.ESTADO_TRIMESTRAL: "quarterly",
    DocumentType.ESTADO_ANUAL: "annual",
}

# Spanish IRPF tax reports (and the annual tax-authority filings) file into
# ``<year>/tax/`` by their as-of date, named ``<stem> <YYYYMMDD>.pdf`` — no
# account, no reference. Maps each doctype to its filename stem-prefix. The
# P&L reports use ``Realised PL`` / ``Unrealised PL``; the comprehensive
# annual statement uses ``Fiscal statement`` (no ``PL`` — it isn't a P&L
# report); the tax-authority filings keep their own form names.
_TAX_REPORT_STEMS: dict[DocumentType, str] = {
    DocumentType.TAX_REALISED_PL: "Realised PL",
    DocumentType.TAX_UNREALISED_PL: "Unrealised PL",
    DocumentType.TAX_FISCAL_STATEMENT: "Fiscal statement",
    DocumentType.DECLARACION_ETE: "ETE",
    DocumentType.MODELO_720: "Modelo 720",
    DocumentType.INCOME_CAPITAL_GAINS_UK: "Income and capital gains UK",
}


def filing_info(classification: Classification, text: str) -> FilingInfo | None:
    """Combine the classifier verdict with the scraped fields, or ``None``.

    ``None`` when no bank was identified, the bank has no filing parser, or
    the required fields for the document's shape are missing — the document is
    then left unfiled. A Spanish IRPF tax report needs only an as-of date (no
    account header — it carries none). A periodic valuation statement needs an
    account + an as-of date; a transaction advice needs an account + a
    reference + publication date.
    """

    bank = classification.bank.bank if classification.bank else BankId.UNKNOWN
    parser = FIELD_PARSERS.get(bank)
    if parser is None:
        return None
    doc_type = classification.document_type
    # Spanish IRPF tax reports carry no account header, so they're routed
    # before the account-requiring parser path: keyed on the numeric as-of
    # date alone, filed into ``<year>/tax/``.
    tax_stem = _TAX_REPORT_STEMS.get(doc_type)
    if tax_stem is not None:
        as_of = _pictet_tax_as_of(text, doc_type)
        if as_of is None:
            return None
        return FilingInfo(
            account="",
            reference=None,
            published=None,
            document_type=doc_type,
            as_of=as_of,
            tax_label=tax_stem,
        )
    fields = parser(text)
    if fields is None or fields.account is None:
        return None
    period = _STATEMENT_PERIODS.get(doc_type)
    if period is not None:
        # Periodic valuation statement — keyed on the as-of date, no reference.
        if fields.as_of is None:
            return None
        return FilingInfo(
            account=fields.account,
            reference=None,
            published=fields.published,
            document_type=doc_type,
            currency=fields.currency,
            as_of=fields.as_of,
            period=period,
        )
    # Transaction advice — keyed on the reference + publication date.
    if fields.reference is None or fields.published is None:
        return None
    return FilingInfo(
        account=fields.account,
        reference=fields.reference,
        published=fields.published,
        document_type=doc_type,
        currency=fields.currency,
    )


def _doctype_suffix(doc_type: DocumentType) -> str:
    """Title-case a doctype value for a filename suffix (``factura`` ->
    ``Factura``; ``debit_of_fees`` -> ``DebitOfFees``)."""

    return "".join(part.capitalize() for part in doc_type.value.split("_"))


# Interest advices are filed with a currency-bearing suffix: Pictet issues an
# interest payment + interest scale per currency that share a transaction
# number (so the bare name collides), and the currency keeps a period's
# several advices distinct and readable (``Interest GBP`` /
# ``Interest scale HKD``). Maps each interest doctype to its label stem.
_INTEREST_LABELS: dict[DocumentType, str] = {
    DocumentType.INTEREST_PAYMENT: "Interest",
    DocumentType.INTEREST_SCALE: "Interest scale",
}


def _suffix_label(info: FilingInfo) -> str:
    """The filename suffix that distinguishes documents sharing a reference.

    Interest advices carry their currency (``Interest GBP`` / ``Interest
    scale HKD``); everything else uses the title-cased doctype. Falls back to
    the doctype if an interest advice has no currency (shouldn't happen — the
    current-account leg always names it)."""

    label = _INTEREST_LABELS.get(info.document_type)
    if label is not None and info.currency is not None:
        return f"{label} {info.currency}"
    return _doctype_suffix(info.document_type)


def destination_for(
    dest_root: Path, info: FilingInfo, *, disambiguate: bool = False
) -> Path:
    """The archive path a document with ``info`` files to under ``dest_root``.

    A Spanish IRPF tax report files into ``<as-of-year>/tax/`` named
    ``<stem> <YYYYMMDD>.pdf`` (``Realised PL`` / ``Unrealised PL`` / ``Fiscal
    statement``; no account segment; the as-of date makes it unique, so
    ``disambiguate`` is moot). A periodic valuation statement files
    into the account's ``reports/`` subfolder under its as-of year, named
    ``Valuation <period> <YYYYMMDD>.pdf`` (likewise unique). A transaction
    advice files to ``<pub-year>/<account>/<date>-<ref>.pdf``; with
    ``disambiguate`` the doctype is appended to the stem so two documents
    sharing a reference don't claim the same name.
    """

    if info.is_tax_report:
        assert info.as_of is not None  # guaranteed by filing_info for tax reports
        stem = f"{info.tax_label} {info.as_of:%Y%m%d}"
        return dest_root / f"{info.as_of:%Y}" / "tax" / f"{stem}.pdf"
    if info.is_statement:
        assert info.as_of is not None  # guaranteed by filing_info for statements
        stem = f"Valuation {info.period} {info.as_of:%Y%m%d}"
        return (
            dest_root / f"{info.as_of:%Y}" / info.account / "reports" / f"{stem}.pdf"
        )
    stem = f"{info.published:%Y%m%d}-{info.reference}"
    if disambiguate:
        stem += f"-{_suffix_label(info)}"
    return dest_root / f"{info.published:%Y}" / info.account / f"{stem}.pdf"


FilingStatus = Literal["move", "skip", "no-match", "error"]


@dataclass(frozen=True)
class FilingPlan:
    """The outcome (or planned outcome under ``dry_run``) for one PDF.

    ``status`` is ``"move"`` (filed, or would be), ``"skip"`` (destination
    already exists — left untouched), ``"no-match"`` (the classifier didn't
    identify a bank with a filing parser, or the fields were missing) or
    ``"error"`` (the PDF couldn't be read / classified). ``destination`` is
    ``None`` for the non-move outcomes; ``detail`` carries the error message.
    """

    source: Path
    destination: Path | None
    status: FilingStatus
    detail: str = ""


def file_documents(
    pdfs: Sequence[Path],
    dest_root: Path,
    *,
    dry_run: bool,
    classifier: LayeredClassifier | None = None,
) -> list[FilingPlan]:
    """File each PDF into ``dest_root`` and return what happened per file.

    Existing destinations are never overwritten (``"skip"``); an unreadable
    PDF is reported and the run continues (``"error"``). With ``dry_run`` the
    moves are computed but no file is touched. Collision disambiguation (the
    doctype suffix) is computed across this batch.
    """

    classifier = classifier or LayeredClassifier()

    # Pass 1: classify + scrape fields. Resolved files become ``infos``;
    # unreadable / unrecognised ones get a terminal plan now.
    infos: dict[Path, FilingInfo] = {}
    early: dict[Path, FilingPlan] = {}
    for pdf in pdfs:
        try:
            doc = load_pdf(pdf)
            info = filing_info(classifier.classify(doc), doc.text)
        except Exception as exc:  # noqa: BLE001 — one bad PDF mustn't abort filing
            early[pdf] = FilingPlan(
                pdf, None, "error", f"{type(exc).__name__}: {exc}"
            )
            continue
        if info is None:
            early[pdf] = FilingPlan(pdf, None, "no-match")
        else:
            infos[pdf] = info

    # Pass 2: a (account, date, reference) claimed by more than one distinct
    # doctype is a within-batch collision — its members file with a suffix.
    # Statements and tax reports don't participate (no reference; their as-of
    # name is unique).
    doctypes_by_key: dict[tuple[str, date, str], set[DocumentType]] = defaultdict(
        set
    )
    for info in infos.values():
        if info.is_statement or info.is_tax_report:
            continue
        assert info.published is not None and info.reference is not None
        key = (info.account, info.published, info.reference)
        doctypes_by_key[key].add(info.document_type)

    # Pass 3: build destinations and move (or skip), preserving input order.
    plans: list[FilingPlan] = []
    for pdf in pdfs:
        if pdf in early:
            plans.append(early[pdf])
            continue
        info = infos[pdf]
        # Interest advices always carry a currency suffix (payment + scale
        # share a reference, and the currency belongs in the name); other
        # advices only when a reference is shared by more than one doctype.
        # Statements and tax reports are never disambiguated.
        disambiguate = False
        if not info.is_statement and not info.is_tax_report:
            assert info.published is not None and info.reference is not None
            key = (info.account, info.published, info.reference)
            disambiguate = (
                info.document_type in _INTEREST_LABELS
                or len(doctypes_by_key[key]) > 1
            )
        dest = destination_for(dest_root, info, disambiguate=disambiguate)
        if dest.exists():
            plans.append(FilingPlan(pdf, dest, "skip"))
            continue
        if not dry_run:
            dest.parent.mkdir(parents=True, exist_ok=True)
            # shutil.move (not Path.rename) so a download folder and archive
            # on different mounts don't raise EXDEV.
            shutil.move(str(pdf), str(dest))
        plans.append(FilingPlan(pdf, dest, "move"))

    return plans


def _glob_dir_pdfs(directory: Path, pattern: str) -> list[Path]:
    """Top-level PDFs in ``directory`` matching ``pattern`` (case-insensitive
    on the extension, so ``*.pdf`` also picks up ``.PDF`` / ``.Pdf``)."""

    seen: set[Path] = set()
    for pat in {pattern, pattern.lower(), pattern.upper()}:
        for candidate in directory.glob(pat):
            if candidate.is_file():
                seen.add(candidate)
    return sorted(seen)


def _extract_zip_pdfs(zf: zipfile.ZipFile, dest: Path, pattern: str) -> list[Path]:
    """Extract the PDF members of ``zf`` matching ``pattern`` into ``dest``
    and return their paths (sorted).

    ``ZipFile.extract`` sanitises member names (strips leading separators and
    ``..`` components), so a malicious entry can't escape ``dest``. Source
    names don't matter downstream — the archive path is derived from each
    PDF's content — so URL-encoded bank filenames pass through untouched.
    """

    paths: list[Path] = []
    for member in zf.infolist():
        if member.is_dir():
            continue
        name = Path(member.filename).name
        if not fnmatch.fnmatch(name.lower(), pattern.lower()):
            continue
        paths.append(Path(zf.extract(member, dest)))
    return sorted(paths)


def _pdfs_from_source(source: Path, pattern: str, stack: ExitStack) -> list[Path]:
    """PDF paths from one source: a ``.zip`` (members extracted onto
    ``stack``), a directory (top-level glob), or a single loose PDF file
    matching ``pattern``. Anything else contributes nothing."""

    if source.is_file() and source.suffix.lower() == ".zip":
        zf = stack.enter_context(zipfile.ZipFile(source))
        tmp = stack.enter_context(
            tempfile.TemporaryDirectory(prefix="bankpipe-import-")
        )
        return _extract_zip_pdfs(zf, Path(tmp), pattern)
    if source.is_dir():
        return _glob_dir_pdfs(source, pattern)
    if source.is_file() and fnmatch.fnmatch(source.name.lower(), pattern.lower()):
        return [source]
    return []


@contextmanager
def source_pdfs(
    sources: Sequence[Path], pattern: str = "*.pdf"
) -> Iterator[list[Path]]:
    """Yield the combined PDF paths to file from one or more ``sources``.

    Each source is a directory (its top level is globbed), a ``.zip`` (its
    PDF members are extracted to a temp dir), or a loose PDF file. Zip
    extractions live for the duration of the ``with`` block — across *all*
    sources at once — so a single filing pass can disambiguate a reference
    shared across two zips, and every temp extraction is cleaned up on exit
    (files that get filed are moved out first; the rest are discarded).
    """

    with ExitStack() as stack:
        pdfs: list[Path] = []
        for source in sources:
            pdfs.extend(_pdfs_from_source(source, pattern, stack))
        yield sorted(pdfs)


def expand_source_glob(pattern: str) -> list[Path]:
    """Expand an import-source glob (``~`` allowed) to sorted matching paths.

    Matches files and directories alike, so ``~/Downloads/files-*.zip`` picks
    up the bank's periodic zip downloads. Returns ``[]`` when nothing matches.
    """

    return sorted(
        {Path(p) for p in glob.glob(os.path.expanduser(pattern), recursive=True)}
    )

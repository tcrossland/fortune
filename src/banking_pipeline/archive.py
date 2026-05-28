"""File raw bank-statement PDFs into a dated archive tree.

This is the first stage of the pipeline: it takes a folder (or a ``.zip``,
the shape the bank's bulk download arrives in) of freshly downloaded PDFs
and files each into ``<root>/<year>/<account>/<YYYYMMDD>-<reference>.pdf`` so
the later ingest / report stages have a stable, organised source tree to
read from.

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

# A bank parser pulls the filing fields from the document text, or returns
# ``None`` when the required ones aren't present (so a misrouted document
# fails closed rather than filing under a garbage name). ``currency`` is the
# document's account currency when discernible (used in interest suffixes),
# else ``None``.
FilingFields = tuple[str, str, date, str | None]
#                    (account, reference, published, currency)


@dataclass(frozen=True)
class FilingInfo:
    """Everything needed to file one document into the archive tree."""

    account: str
    reference: str
    published: date
    document_type: DocumentType
    currency: str | None = None


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


def pictet_filing_fields(text: str) -> FilingFields | None:
    """Scrape Pictet's account / reference / publication date (and the
    account currency when present) from ``text``."""

    account = _PICTET_ACCOUNT.search(text)
    reference = _PICTET_REFERENCE.search(text)
    published = _PICTET_PUBLISHED.search(text)
    if account is None or reference is None or published is None:
        return None
    day, month, year = published.group(1, 2, 3)
    currency = _PICTET_CURRENCY.search(text)
    return (
        account.group(1),
        reference.group(1),
        date(int(year), int(month), int(day)),
        currency.group(1) if currency is not None else None,
    )


# Filing-field parsers keyed by the classifier's bank verdict. Add a bank by
# registering its parser here (and the classifier already knowing the bank).
FIELD_PARSERS: dict[BankId, Callable[[str], FilingFields | None]] = {
    BankId.PICTET: pictet_filing_fields,
}


def filing_info(classification: Classification, text: str) -> FilingInfo | None:
    """Combine the classifier verdict with the scraped fields, or ``None``.

    ``None`` when no bank was identified, the bank has no filing parser, or
    the parser couldn't find the fields — the document is then left unfiled.
    """

    bank = classification.bank.bank if classification.bank else BankId.UNKNOWN
    parser = FIELD_PARSERS.get(bank)
    if parser is None:
        return None
    fields = parser(text)
    if fields is None:
        return None
    account, reference, published, currency = fields
    return FilingInfo(
        account=account,
        reference=reference,
        published=published,
        document_type=classification.document_type,
        currency=currency,
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

    With ``disambiguate`` the doctype is appended to the stem so two
    documents sharing a reference don't claim the same name.
    """

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
    doctypes_by_key: dict[tuple[str, date, str], set[DocumentType]] = defaultdict(
        set
    )
    for info in infos.values():
        key = (info.account, info.published, info.reference)
        doctypes_by_key[key].add(info.document_type)

    # Pass 3: build destinations and move (or skip), preserving input order.
    plans: list[FilingPlan] = []
    for pdf in pdfs:
        if pdf in early:
            plans.append(early[pdf])
            continue
        info = infos[pdf]
        key = (info.account, info.published, info.reference)
        # Interest advices always carry a currency suffix (payment + scale
        # share a reference, and the currency belongs in the name); other
        # doctypes only when a reference is shared by more than one doctype.
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

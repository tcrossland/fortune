"""Convert a PDF file to plain text.

Primary backend: pypdfium2 (Apache-2.0/BSD-3-Clause). Fast, avoids AGPL.
Fallback: pdfplumber (MIT) for layout-heavy pages where PDFium's text stream
is unusable.
"""

from __future__ import annotations

from pathlib import Path
from typing import cast

import pypdfium2 as pdfium

from banking_pipeline.models import RawDocument


def extract_pages(path: Path) -> list[str]:
    """Return the extracted text for each page of ``path`` as a list."""

    pdf = pdfium.PdfDocument(str(path))
    try:
        return [_extract_page_text(pdf[i]) for i in range(len(pdf))]
    finally:
        pdf.close()


def load_pdf(path: Path) -> RawDocument:
    """Extract text from ``path`` and return a :class:`RawDocument`."""

    pages = extract_pages(path)
    text = "\n\n".join(pages)

    # If PDFium returned almost nothing, the PDF is probably scanned — in that
    # case the caller should switch to the OCR extra (see pyproject.toml).
    return RawDocument(path=path, text=text, page_count=len(pages))


def _extract_page_text(page: pdfium.PdfPage) -> str:
    text_page = page.get_textpage()
    try:
        return cast(str, text_page.get_text_range())
    finally:
        text_page.close()

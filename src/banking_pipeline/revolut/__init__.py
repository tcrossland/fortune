"""Revolut CSV → beancount importer.

Separate path from the PDF pipeline: Revolut publishes per-pocket CSVs from
the Personal app (https://app.revolut.com → Account → Statement → Excel/CSV),
which arrive already structured. This module parses those CSVs and emits
beancount transactions; no field extraction or LLM fallback is needed.

Public surface:

* :func:`import_csvs` — parse one or more CSV files, pair EXCHANGE legs
  across them, and return a list of :class:`~.models.RevolutTxn`.
* :func:`render` — turn an iterable of transactions into beancount text.
"""

from __future__ import annotations

from banking_pipeline.revolut.csv_importer import import_csvs, parse_csv
from banking_pipeline.revolut.models import RevolutRow, RevolutTxn
from banking_pipeline.revolut.render import render

__all__ = [
    "RevolutRow",
    "RevolutTxn",
    "import_csvs",
    "parse_csv",
    "render",
]

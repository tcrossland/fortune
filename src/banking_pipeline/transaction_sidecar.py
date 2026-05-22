"""Structured JSONL sidecar for extracted transactions.

Each generated ``.beancount`` file gets a companion
``<stem>.transactions.jsonl`` carrying the raw :class:`Transaction`
objects that produced it. The rendered beancount encodes much of the
UK-tax-relevant data (GBP rate, withholding tax, accrued interest) into
free-text postings / metadata; persisting the structured form alongside
lets the tax-report stage consume it without re-parsing beancount —
which also keeps us clear of the ``import beancount`` (GPL-2.0)
constraint the rest of the writer is built around.

File format: a header line (a single JSON object with a ``_schema``
marker and the originating ``source_document``), then one JSON object
per transaction — :meth:`Transaction.model_dump` in ``mode="json"`` so
``Decimal`` round-trips as a string (never a float), ``date`` as
``YYYY-MM-DD``, and enums as their values.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from pathlib import Path

from banking_pipeline.models import Transaction

# Bump the version suffix when the on-disk shape changes incompatibly so
# a future reader can branch on it.
SCHEMA = "banking-pipeline/transactions/v1"


def sidecar_path(beancount_path: Path) -> Path:
    """Return the sidecar path for a generated ``.beancount`` file.

    ``data/2024-K.beancount`` → ``data/2024-K.transactions.jsonl`` (the
    stem is reused, the suffix replaced), so the two sit side by side and
    the ``*.transactions.jsonl`` clean glob matches.
    """

    return beancount_path.with_name(f"{beancount_path.stem}.transactions.jsonl")


def transactions_to_jsonl(
    transactions: Iterable[Transaction], *, source_document: str | None = None
) -> str:
    """Serialise ``transactions`` to JSONL text (header line first).

    ``source_document`` records where the transactions came from — a
    single PDF's relative path for a one-document dump, or ``None`` for a
    combined per-label file (each line still carries its own
    ``source_path``). An empty ``transactions`` yields a header-only
    string, which lets a reader tell "no transactions, expected" from a
    missing file.
    """

    header = {"_schema": SCHEMA, "source_document": source_document}
    lines = [json.dumps(header)]
    lines.extend(
        json.dumps(tx.model_dump(mode="json")) for tx in transactions
    )
    return "\n".join(lines) + "\n"


def dump_transactions(
    transactions: Iterable[Transaction],
    path: Path,
    *,
    source_document: str | None = None,
) -> None:
    """Write ``transactions`` to ``path`` as JSONL (see :func:`transactions_to_jsonl`)."""

    path.write_text(
        transactions_to_jsonl(transactions, source_document=source_document),
        encoding="utf-8",
    )


def load_transactions(path: Path) -> list[Transaction]:
    """Read a sidecar back into validated :class:`Transaction` objects.

    The schema header line is skipped; every other non-empty line is
    validated through the model, so a malformed sidecar fails loudly
    rather than yielding half-typed dicts.
    """

    transactions: list[Transaction] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        obj = json.loads(line)
        if "_schema" in obj:
            continue
        transactions.append(Transaction.model_validate(obj))
    return transactions

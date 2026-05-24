"""Duplicate-transaction audit over the JSONL sidecars.

Re-running ``ingest`` / ``rebuild`` is already idempotent — each writes
its output by overwrite, and ``rebuild`` cleans then regenerates per
year. What that *doesn't* catch is **double-counting**: the same
economic event landing in the ledger twice, e.g. the same advice PDF
matched by two source globs, a file copied into two year folders, or a
re-issued document. Those inflate balances silently.

This module is the read-only audit for that. It assigns each
:class:`~banking_pipeline.models.Transaction` a **content key** —
a hash over the fields that identify the *event* (date, signed amount,
currency, ISIN, doctype, account) — and groups transactions that share
one. A group with more than one member is a suspected duplicate.

The key deliberately **excludes** the per-document reference
(``transaction_number``) and the free-text narration, so the same event
extracted from two *different* documents still collides. The cost is
that two genuinely-distinct-but-identical events (say two equal
dividends on the same day in the same account) also collide — a false
positive. That's the right bias for an advisory check a human reviews:
better to over-report and let the ``transaction_number`` tell exact
duplicates (same ref → same document twice) from coincidental ones.

Pure by design — no file IO, so it never imports the sidecar reader
(which imports :func:`transaction_key` to stamp the key on write). The
CLI does the loading and hands :class:`DuplicateMember` objects in.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from banking_pipeline.models import Transaction


def _canonical_amount(amount: Decimal) -> str:
    """Value-canonical, sign-preserving, non-scientific string form.

    ``normalize`` collapses trailing-zero differences so the same amount
    printed at different precisions (``85.00`` vs ``85.0000``) keys
    identically; ``format(..., "f")`` keeps it out of scientific
    notation (``normalize`` would render ``100.00`` as ``1E+2``)."""

    return format(amount.normalize(), "f")


def transaction_key(tx: Transaction) -> str:
    """Stable content hash identifying the same economic event.

    Built from ``trade_date``, ``currency``, the canonical signed
    ``amount``, ``isin``, ``document_type`` and ``account_number`` —
    everything that pins down *what happened*, with the per-document
    reference and narration left out on purpose (see the module
    docstring). Returns a hex SHA-1; collisions on distinct content are
    not a concern here (the inputs are short and low-cardinality).
    """

    parts = (
        tx.trade_date.isoformat(),
        tx.currency,
        _canonical_amount(tx.amount),
        tx.isin or "",
        tx.document_type.value if tx.document_type else "",
        tx.account_number or "",
    )
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class DuplicateMember:
    """One transaction in a duplicate group, tagged with its sidecar."""

    transaction: Transaction
    sidecar: Path


@dataclass(frozen=True)
class DuplicateGroup:
    """Transactions that share a content key — a suspected duplicate."""

    key: str
    members: tuple[DuplicateMember, ...]

    @property
    def exact(self) -> bool:
        """True when every member shares one non-null ``transaction_number``.

        That's the near-certain case: the very same document was ingested
        more than once. A group whose members carry differing or missing
        references is only a *possible* duplicate (two distinct documents,
        or refs the extractor couldn't read) and is flagged for review
        rather than asserted."""

        numbers = {m.transaction.transaction_number for m in self.members}
        return len(numbers) == 1 and None not in numbers


def find_duplicates(members: Iterable[DuplicateMember]) -> list[DuplicateGroup]:
    """Group ``members`` by content key; return only the groups > 1.

    Ordered by the event's trade date then key, so the report reads
    chronologically and is stable across runs.
    """

    by_key: dict[str, list[DuplicateMember]] = {}
    for member in members:
        by_key.setdefault(transaction_key(member.transaction), []).append(member)

    groups = [
        DuplicateGroup(key, tuple(ms)) for key, ms in by_key.items() if len(ms) > 1
    ]
    groups.sort(
        key=lambda g: (g.members[0].transaction.trade_date.isoformat(), g.key)
    )
    return groups


def _describe(tx: Transaction) -> str:
    """One-line event description for the summary header."""

    bits = [tx.trade_date.isoformat(), f"{tx.amount} {tx.currency}"]
    if tx.isin:
        bits.append(tx.isin)
    if tx.document_type:
        bits.append(tx.document_type.value)
    return "  ".join(bits)


def render_summary(
    groups: list[DuplicateGroup], *, scanned: int, sidecars: int
) -> str:
    """Human-readable audit summary."""

    lines = [
        f"Duplicate audit — {scanned} transaction(s) across "
        f"{sidecars} sidecar(s)",
        "",
    ]
    if not groups:
        lines.append("No duplicates found.")
        lines.append("")
        return "\n".join(lines)

    exact = sum(1 for g in groups if g.exact)
    lines.append(
        f"{len(groups)} suspected duplicate group(s) "
        f"({exact} exact, {len(groups) - exact} possible):"
    )
    lines.append("")
    for group in groups:
        tag = "EXACT" if group.exact else "POSSIBLE"
        lines.append(f"  [{tag}] {_describe(group.members[0].transaction)}")
        for m in group.members:
            ref = m.transaction.transaction_number or "—"
            lines.append(f"      {m.transaction.source_path}  (no: {ref})  [{m.sidecar}]")
        lines.append("")
    return "\n".join(lines)


def render_csv(groups: list[DuplicateGroup]) -> str:
    """Machine-readable duplicates: one row per member."""

    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(
        [
            "key",
            "classification",
            "trade_date",
            "amount",
            "currency",
            "isin",
            "document_type",
            "account_number",
            "transaction_number",
            "source_path",
            "sidecar",
        ]
    )
    for group in groups:
        classification = "exact" if group.exact else "possible"
        for m in group.members:
            tx = m.transaction
            writer.writerow(
                [
                    group.key,
                    classification,
                    tx.trade_date.isoformat(),
                    str(tx.amount),
                    tx.currency,
                    tx.isin or "",
                    tx.document_type.value if tx.document_type else "",
                    tx.account_number or "",
                    tx.transaction_number or "",
                    str(tx.source_path),
                    str(m.sidecar),
                ]
            )
    return out.getvalue()

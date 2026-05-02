"""Beancount text-rendering package.

Public surface:

* :func:`render` — turn an :class:`~banking_pipeline.models.ExtractionResult`
  into a chunk of beancount text (header + one entry per transaction).
* :func:`render_entry` — render a single
  :class:`~banking_pipeline.models.Transaction` without the ``;`` audit header.
* :func:`render_all` — render a batch of results, prepending a single
  ``open`` directive block via :func:`render_open_directives`.
* :func:`render_open_directives` — collect ISINs across a batch and emit one
  ``open`` directive per (bank, portfolio, ISIN).

Internals are split by render shape under :mod:`banking_pipeline.writer.builders`,
with bank-specific configuration under :mod:`banking_pipeline.writer.profile`
and the dispatcher / routing tables under :mod:`banking_pipeline.writer.dispatch`.
The legacy :mod:`banking_pipeline.beancount_writer` module re-exports this
package's public API for callers that import the old path.
"""

from __future__ import annotations

from decimal import Decimal

from banking_pipeline.writer.dispatch import (
    render,
    render_all,
    render_entry,
    render_open_directives,
)

# Re-exported for convenience in callers that want the zero-amount shortcut.
ZERO = Decimal("0")

__all__ = [
    "render",
    "render_all",
    "render_entry",
    "render_open_directives",
    "ZERO",
]

"""Backwards-compatible re-export shim.

The writer's implementation now lives in the
:mod:`banking_pipeline.writer` package — split by render shape under
``writer/builders/``, with shared formatting helpers in
``writer/format.py`` and bank-specific configuration in
``writer/profile.py``. This module preserves the historical import
path (``from banking_pipeline import beancount_writer``;
``beancount_writer.render(...)``) so callers and goldens that imported
the old surface keep working byte-stably.

Prefer importing from :mod:`banking_pipeline.writer` in new code.
"""

from __future__ import annotations

from banking_pipeline.writer import (
    ZERO,
    render,
    render_all,
    render_close_directives,
    render_entry,
    render_open_directives,
)

__all__ = [
    "render",
    "render_all",
    "render_close_directives",
    "render_entry",
    "render_open_directives",
    "ZERO",
]

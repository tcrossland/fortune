"""Jinja environment and the ``DocumentType``-keyed template registry.

Loads ``*.beancount.j2`` templates from
:mod:`banking_pipeline.writer.templates` via ``FileSystemLoader``. The
:data:`TEMPLATES` registry maps the doctypes that fall through to Jinja
to their template name; everything else routes through a Python builder
in :mod:`banking_pipeline.writer.builders`.

Most doctypes are rendered by Python builders, not by Jinja — the
FX-vs-non-FX split, bank-prefixed accounts, and column-aligned amounts
are awkward to express as a static template, and the builder approach
avoids the trim_blocks/whitespace fragility we hit when the legacy
inline Jinja templates carried inline conditionals.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from banking_pipeline.models import DocumentType
from banking_pipeline.writer.format import portfolio_segment

_TEMPLATE_DIR = Path(__file__).parent / "templates"

# NOTE: do NOT enable ``trim_blocks`` / ``lstrip_blocks`` here. With those
# defaults Jinja swallows the newline that follows a block tag (``{% endif %}``
# etc.), which historically collapsed multi-leg postings onto a single line
# and produced output bean-check rejected. None of the surviving Jinja
# templates use block tags today, but the safe-default stays as a guard
# against the next template that adds an inline conditional.
ENV = Environment(
    loader=FileSystemLoader(str(_TEMPLATE_DIR)),
    autoescape=select_autoescape(enabled_extensions=()),
    keep_trailing_newline=True,
)

# Expose ``portfolio_segment`` to Jinja so the fallback template can
# sanitise ``tx.account_number`` the same way the Python builders do.
ENV.filters["portfolio_segment"] = portfolio_segment


# Doctypes routed through a static Jinja template rather than a Python
# builder. Every entry here corresponds to one ``<name>.beancount.j2``
# file under :mod:`banking_pipeline.writer.templates`.
TEMPLATES = {
    # --- Non-cash events ---
    DocumentType.LIMIT_EXTENSION: ENV.get_template("limit_extension.beancount.j2"),
}


# Catch-all template for doctypes the dispatcher doesn't recognise. Emits
# an ``Equity:Uncategorized``-balanced two-leg entry with a ``TODO review``
# audit comment so the user notices it on next bean-check / Fava load.
DEFAULT_TEMPLATE = ENV.get_template("default.beancount.j2")

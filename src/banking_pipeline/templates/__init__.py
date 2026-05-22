"""Per-bank / per-document-type extractors.

Drop a new module in the relevant bank subpackage and add the template
instance to that subpackage's exported tuple (e.g.
:data:`banking_pipeline.templates.pictet.PICTET_TEMPLATES`) to teach the
pipeline a new statement layout. Each template exposes an ``extract`` method
that receives a :class:`~banking_pipeline.models.RawDocument` and returns a
list of :class:`~banking_pipeline.models.Transaction`.

The registry is keyed on ``template_id`` (e.g. ``pictet.subscription_notice.v1``);
the rule classifier emits that string under
:attr:`~banking_pipeline.models.Classification.template_id` and the hybrid
extractor uses it to route documents to their template.
"""

from __future__ import annotations

from typing import Protocol

from banking_pipeline.models import RawDocument, Transaction


class Template(Protocol):
    template_id: str

    def extract(self, doc: RawDocument) -> list[Transaction]: ...


# Registry binding comes first, populate-by-mutation second. The order matters:
# loading the bank subpackages eventually re-enters this module via
# ``banking_pipeline.fields.hybrid`` (which imports ``TEMPLATE_REGISTRY`` at
# module level), so the name has to be bound to a real dict before any of
# those imports run. ``hybrid.py`` will then receive a reference to the *same*
# dict object that ``_populate_registry`` mutates, and will see the populated
# entries by the time its ``HybridExtractor.extract`` actually runs.
TEMPLATE_REGISTRY: dict[str, Template] = {}


def _populate_registry() -> None:
    """Fill :data:`TEMPLATE_REGISTRY` with the templates exposed by each
    bank-specific subpackage.

    Imports live inside the function so the registry binding above is fully
    in place before we trigger any module loads that might re-enter this
    module (see the note on the binding for why that matters).
    """

    from banking_pipeline.templates.pictet import PICTET_TEMPLATES

    for template in PICTET_TEMPLATES:
        TEMPLATE_REGISTRY[template.template_id] = template


_populate_registry()

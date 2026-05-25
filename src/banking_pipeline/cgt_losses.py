"""Pre-ledger allowable capital losses carried into the ledger era.

The loss-carry-forward chain (:mod:`banking_pipeline.tax.uk.cgt_allowance`)
computes losses *within* the ledger's history, but allowable losses
realised before the earliest data — or already carried forward on the
user's prior returns — have to be seeded in. This loads a single GBP
figure the user maintains.

The file is gitignored (personal tax data); a committed
``data/cgt-losses.example.toml`` documents the schema.
"""

from __future__ import annotations

import tomllib
from decimal import Decimal
from pathlib import Path


def load_cgt_brought_forward_losses(path: Path) -> Decimal:
    """Return the pre-ledger brought-forward allowable loss in GBP.

    Reads the ``brought_forward_gbp`` key; a missing key (or empty file)
    means no pre-ledger losses, i.e. ``0``.
    """

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    value = raw.get("brought_forward_gbp", 0)
    return Decimal(str(value))

"""Hermetic defaults for the UK-tax CLI tests.

The CLI commands read the module-level ``settings``, which is built from
the developer's ``.env`` — and that may configure UK residence and FIG
claims (it does in this repo). Reset those to their no-op defaults before
every test so the suite is independent of local configuration; tests that
exercise residence / FIG set them explicitly via monkeypatch.
"""

from __future__ import annotations

import pytest

from banking_pipeline import cli


@pytest.fixture(autouse=True)
def _neutralise_residence_and_fig(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cli.settings, "uk_residence_start_date", None)
    monkeypatch.setattr(cli.settings, "fig_claim_years", frozenset())

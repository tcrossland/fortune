"""Unit tests for the HMRC reporting-funds metadata rewrite.

The network path isn't exercised; the pure, order-independent
``apply_reporting_status`` rewrite is (the part that touches the user's
``commodities.toml``). The script is loaded by path since ``scripts/``
isn't an importable package.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "fetch_reporting_funds.py"


def _mod() -> ModuleType:
    spec = importlib.util.spec_from_file_location("fetch_reporting_funds", _SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # top level does no network
    return mod


def test_upgrades_only_listed_unknown_entries() -> None:
    mod = _mod()
    text = (
        "# preamble comment\n\n"
        '[[commodity]]\n'
        'isin = "LU0000000001"\n'
        'name = "Listed Fund"\n'
        'reporting_status = "unknown"\n'
        'asset_class = "equity-fund"\n\n'
        '[[commodity]]\n'
        'isin = "IE0000000002"\n'
        'name = "Unlisted Fund"\n'
        'reporting_status = "unknown"\n\n'
        '[[commodity]]\n'
        'isin = "GB0000000003"\n'
        'reporting_status = "non-reporting"\n'
    )
    new, n = mod.apply_reporting_status(text, {"LU0000000001"})

    assert n == 1
    # Listed + unknown → upgraded; surrounding lines untouched.
    assert 'isin = "LU0000000001"' in new
    assert 'name = "Listed Fund"\nreporting_status = "reporting"' in new
    # Unlisted unknown left alone.
    assert 'name = "Unlisted Fund"\nreporting_status = "unknown"' in new
    # A deliberate non-reporting is never touched.
    assert 'reporting_status = "non-reporting"' in new
    # Preamble / comments preserved.
    assert new.startswith("# preamble comment")


def test_rewrite_is_order_independent() -> None:
    # reporting_status BEFORE isin within the block — must still upgrade
    # (the A8 fragility the old line-coupled rewrite had).
    mod = _mod()
    text = (
        "[[commodity]]\n"
        'reporting_status = "unknown"\n'
        'isin = "LU0000000001"\n'
        'name = "Reversed-order block"\n'
    )
    new, n = mod.apply_reporting_status(text, {"LU0000000001"})
    assert n == 1
    assert 'reporting_status = "reporting"' in new


def test_noop_returns_input_byte_for_byte() -> None:
    mod = _mod()
    text = '[[commodity]]\nisin = "X"\nreporting_status = "unknown"\n'
    new, n = mod.apply_reporting_status(text, set())
    assert n == 0
    assert new == text


def test_preserves_indentation_on_upgrade() -> None:
    mod = _mod()
    text = '[[commodity]]\nisin = "A"\n  reporting_status = "unknown"\n'
    new, n = mod.apply_reporting_status(text, {"A"})
    assert n == 1
    assert '  reporting_status = "reporting"' in new


def test_needs_review_lists_remaining_unknown() -> None:
    mod = _mod()
    parsed = {
        "commodity": [
            {"isin": "B", "reporting_status": "reporting"},
            {"isin": "A", "reporting_status": "unknown"},
        ]
    }
    assert mod._needs_review(parsed) == ["A"]

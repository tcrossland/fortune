"""UK tax-year boundary helpers.

The UK personal tax year runs 6 April to the following 5 April. A year
is labelled ``"YYYY-YY"`` where the second part is the next year's last
two digits — ``"2025-26"`` is 6 Apr 2025 → 5 Apr 2026.
"""

from __future__ import annotations

import re
from datetime import date

_LABEL_RE = re.compile(r"^(\d{4})-(\d{2})$")


def tax_year_bounds(label: str) -> tuple[date, date]:
    """Return ``(start, end)`` for a ``"YYYY-YY"`` UK tax-year label.

    ``"2025-26"`` → ``(date(2025, 4, 6), date(2026, 4, 5))``. Raises
    ``ValueError`` for a malformed label or one whose second part isn't
    the start year + 1 (e.g. ``"2025-27"`` or the ambiguous ``"2025-25"``).
    """

    m = _LABEL_RE.match(label)
    if m is None:
        raise ValueError(
            f"malformed tax-year label {label!r}; expected 'YYYY-YY' "
            "(e.g. '2025-26')"
        )
    start_year = int(m.group(1))
    end_suffix = int(m.group(2))
    # The label's second part is the last two digits of start_year + 1.
    expected_suffix = (start_year + 1) % 100
    if end_suffix != expected_suffix:
        raise ValueError(
            f"ambiguous tax-year label {label!r}; the second part must be "
            f"the start year + 1 (expected {start_year}-{expected_suffix:02d})"
        )
    return date(start_year, 4, 6), date(start_year + 1, 4, 5)


def date_to_tax_year(on_date: date) -> str:
    """Return the ``"YYYY-YY"`` label of the tax year containing ``on_date``.

    ``date(2026, 1, 10)`` → ``"2025-26"`` (still before 6 Apr 2026);
    ``date(2026, 4, 6)`` → ``"2026-27"``.
    """

    # On or after 6 April the tax year starts in the current calendar
    # year; before that it started the previous calendar year.
    start_year = on_date.year if on_date >= date(on_date.year, 4, 6) else on_date.year - 1
    return f"{start_year}-{(start_year + 1) % 100:02d}"

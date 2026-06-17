#!/usr/bin/env python3
"""Fetch real benchmark levels into ``data/benchmarks.csv``.

Source: Yahoo Finance monthly closes of **GBP accumulating** UCITS ETFs.
An accumulating ETF reinvests income into its price, so its GBP price *is*
a total-return index — directly comparable to the mandate's holdings-based
GBP return. Each monthly close is labelled at its **month-end** date so the
``benchmark`` report's as-of sampling maps it one-to-one onto the mandate's
month-start snapshot dates (an end-of-August close ↔ the 1-September
statement). A 60/40 blend is constructed from the global-equity and
global-bond monthly returns.

Run from the repo root: ``python3 scripts/fetch_benchmarks.py``. Re-run to
refresh. Network access required (Yahoo's public chart endpoint). These are
public market levels — no personal data — so the file is committed, like
``data/fx/hmrc-monthly-average.csv``.
"""

from __future__ import annotations

import calendar
import json
import urllib.request
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Yahoo's monthly bar timestamp is the month START in *exchange-local*
# (London) time; reading it as UTC shifts British-Summer-Time months back a
# day → the wrong month. Use Europe/London so each bar lands in its month.
_LONDON = ZoneInfo("Europe/London")

_CHART = (
    "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    "?period1=1625097600&period2=1788000000&interval=1mo"  # 2021-07 .. ~2026-09
)

# ticker -> display name. All GBP accumulating UCITS ETFs on the LSE.
TICKERS = {
    "VWRP.L": "FTSE All-World (Global eq)",
    "VUAG.L": "S&P 500 (US)",
    "SWLD.L": "MSCI World (Developed)",
    "VUKG.L": "FTSE 100 (UK)",
    "VAGP.L": "Global Agg Bond (GBP-h)",
}
EQUITY = "FTSE All-World (Global eq)"
BOND = "Global Agg Bond (GBP-h)"
BLEND = "Global 60/40"

OUT = Path(__file__).resolve().parent.parent / "data" / "benchmarks.csv"


def _month_end(ts: int) -> date:
    d = datetime.fromtimestamp(ts, tz=_LONDON).date()  # bar's month, London
    last = calendar.monthrange(d.year, d.month)[1]
    return date(d.year, d.month, last)


def _fetch(ticker: str) -> dict[date, float]:
    req = urllib.request.Request(
        _CHART.format(ticker=ticker), headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310 (https only)
        payload = json.load(resp)
    res = payload["chart"]["result"][0]
    meta = res["meta"]
    if meta.get("currency") != "GBP":
        raise SystemExit(f"{ticker}: expected GBP, got {meta.get('currency')}")
    timestamps = res["timestamp"]
    adjclose = res["indicators"]["adjclose"][0]["adjclose"]
    today = datetime.now(tz=_LONDON).date()
    out: dict[date, float] = {}
    for ts, value in zip(timestamps, adjclose, strict=False):
        if value is None:
            continue
        me = _month_end(ts)
        if me > today:
            continue  # drop the in-progress (incomplete) current month
        out[me] = round(float(value), 4)
    return out


def main() -> None:
    series: dict[str, dict[date, float]] = {
        name: _fetch(ticker) for ticker, name in TICKERS.items()
    }

    # 60/40 blend from equity + bond monthly returns, rebased to 100.
    eq, bd = series[EQUITY], series[BOND]
    blend: dict[date, float] = {}
    level, prev = 100.0, None
    for d in sorted(set(eq) & set(bd)):
        if prev is not None:
            r_eq = eq[d] / eq[prev] - 1.0
            r_bd = bd[d] / bd[prev] - 1.0
            level *= 1.0 + 0.6 * r_eq + 0.4 * r_bd
        blend[d] = round(level, 4)
        prev = d
    series[BLEND] = blend

    names = [BLEND, *TICKERS.values()]  # blend first — the headline yardstick
    all_dates = sorted({d for s in series.values() for d in s})

    lines = [
        "# Benchmark levels — GBP total-return, month-end. Regenerate with",
        "# scripts/fetch_benchmarks.py. Source: Yahoo Finance monthly closes",
        "# of GBP accumulating UCITS ETFs (acc price = total return) —",
        "# FTSE All-World=VWRP.L, S&P 500=VUAG.L, MSCI World=SWLD.L,",
        "# FTSE 100=VUKG.L, Global Agg Bond GBP-hedged=VAGP.L. Global 60/40 is",
        "# 60% FTSE All-World + 40% Global Agg by monthly return, rebased 100.",
        "date," + ",".join(names),
    ]
    for d in all_dates:
        cells = [d.isoformat()]
        for name in names:
            v = series[name].get(d)
            cells.append(f"{v}" if v is not None else "")
        lines.append(",".join(cells))

    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(
        f"Wrote {OUT} — {len(all_dates)} months, {len(names)} benchmarks "
        f"({all_dates[0]} .. {all_dates[-1]})"
    )


if __name__ == "__main__":
    main()

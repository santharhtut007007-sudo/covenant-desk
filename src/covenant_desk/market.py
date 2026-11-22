"""Market stress signals from FRED (no API key needed), cached on disk.

Company EBITDA is reported with a lag and cannot warn of a sudden shock. Market prices can:
in March 2020 credit spreads and the VIX jumped weeks before the Q2 EBITDA collapse.
"""

from __future__ import annotations

import io
from datetime import date

import httpx
import pandas as pd

from covenant_desk import config

FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={series}"


def fred_series(series: str, refresh: bool = False) -> pd.Series:
    """Daily FRED series as a float Series indexed by date (missing days dropped)."""
    cache = config.RAW_DIR / "fred" / f"{series}.csv"
    if refresh or not cache.exists():
        cache.parent.mkdir(parents=True, exist_ok=True)
        r = httpx.get(FRED_CSV.format(series=series), timeout=30, follow_redirects=True,
                      headers={"User-Agent": "covenant-desk research-script"})
        r.raise_for_status()
        cache.write_bytes(r.content)
    df = pd.read_csv(io.BytesIO(cache.read_bytes()))
    df.columns = ["date", "value"]
    df["date"] = pd.to_datetime(df["date"]).dt.date
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    return df.dropna().set_index("date")["value"]


def signal_at(series: pd.Series, when: date, window_days: int = 30) -> float:
    """Average of the series over the month up to and including `when` (no look-ahead)."""
    start = date.fromordinal(when.toordinal() - window_days)
    window = series[(series.index <= when) & (series.index > start)]
    if window.empty:
        window = series[series.index <= when].tail(1)
    return float(window.mean())


class StressSignal:
    """Ratio of a market stress gauge to its own history, known on a given date.

    ratio(d) = (average over the month to d) / (median of all values before d).
    1.0 is a normal market; 3.0 means stress three times the usual level.
    """

    def __init__(self, series: pd.Series, name: str):
        self.series = series.sort_index()
        self.name = name
        self._cache: dict[date, float] = {}

    def ratio(self, when: date) -> float:
        if when not in self._cache:
            history = self.series[self.series.index < when]
            ref = float(history.median()) if len(history) > 250 else float(self.series.median())
            self._cache[when] = signal_at(self.series, when) / ref
        return self._cache[when]

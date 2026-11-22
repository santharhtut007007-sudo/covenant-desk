"""Turn the SEC 'companyfacts' JSON into clean quarterly numbers.

Two kinds of XBRL facts matter here:

* Instant facts (balance sheet): a value on a date, e.g. debt on 30 Sep 2026.
* Duration facts (income and cash flow statements): a value over a period. Companies
  report these inconsistently. A 10-Q may give the 3-month quarter, only the year-to-date
  (YTD) figure, or both. Q4 is almost never reported on its own; it is hidden inside the
  10-K annual figure.

`quarterly_flow` rebuilds true 3-month figures: it uses a reported 3-month value where one
exists, and otherwise takes the difference of consecutive YTD values (6M - 3M, 9M - 6M,
12M - 9M).
"""

from __future__ import annotations

from datetime import date

import pandas as pd

QUARTER_DAYS = (80, 100)  # a "3-month" period is 80-100 days long


def facts_table(company_facts: dict, concept: str, taxonomy: str = "us-gaap") -> pd.DataFrame:
    """All reported values of one concept, keeping only the latest filing for each period."""
    try:
        units = company_facts["facts"][taxonomy][concept]["units"]
    except KeyError:
        return pd.DataFrame(columns=["start", "end", "val", "filed", "form"])
    rows = next(iter(units.values()))  # USD for money concepts
    df = pd.DataFrame(rows)
    if "start" not in df:
        df["start"] = None
    df["end"] = pd.to_datetime(df["end"]).dt.date
    df["start"] = pd.to_datetime(df["start"]).dt.date
    df = df.sort_values("filed").drop_duplicates(["start", "end"], keep="last")
    return df[["start", "end", "val", "filed", "form"]].reset_index(drop=True)


def instant_series(company_facts: dict, concepts: list[str]) -> pd.Series:
    """Balance-sheet values by date. Earlier concepts in the list take priority."""
    series = pd.Series(dtype="float64")
    for concept in reversed(concepts):
        df = facts_table(company_facts, concept)
        df = df[df["start"].isna()]
        new = pd.Series(df["val"].astype(float).values, index=df["end"])
        series = new.combine_first(series) if not series.empty else new
    return series.sort_index()


def quarterly_flow(company_facts: dict, concepts: list[str]) -> pd.Series:
    """True 3-month values by quarter-end date. Earlier concepts in the list take priority.

    A concept written as "-Name" is sign-flipped, e.g. "-InterestIncomeExpenseNonoperatingNet"
    (net interest, reported as a negative number when it is an expense).
    """
    result = pd.Series(dtype="float64")
    for spec in reversed(concepts):
        sign, concept = (-1.0, spec[1:]) if spec.startswith("-") else (1.0, spec)
        new = sign * _quarterly_from_concept(facts_table(company_facts, concept))
        result = new.combine_first(result) if not result.empty else new
    return result.sort_index()


def _quarterly_from_concept(df: pd.DataFrame) -> pd.Series:
    df = df.dropna(subset=["start"])
    if df.empty:
        return pd.Series(dtype="float64")
    days = df.apply(lambda r: (r["end"] - r["start"]).days, axis=1)

    # 1. Directly reported 3-month values.
    direct = df[(days >= QUARTER_DAYS[0]) & (days <= QUARTER_DAYS[1])]
    quarters: dict[date, float] = dict(zip(direct["end"], direct["val"].astype(float)))

    # 2. Fill gaps by differencing YTD values that share the same start date.
    for _, group in df.groupby("start"):
        group = group.sort_values("end")
        prev_end, prev_val = group.iloc[0]["start"], 0.0
        for _, row in group.iterrows():
            gap = (row["end"] - prev_end).days
            if QUARTER_DAYS[0] <= gap <= QUARTER_DAYS[1] and row["end"] not in quarters:
                quarters[row["end"]] = float(row["val"]) - prev_val
            prev_end, prev_val = row["end"], float(row["val"])

    return pd.Series(quarters).sort_index()


def ltm(quarterly: pd.Series) -> pd.Series:
    """Last-twelve-months sum, only where four consecutive, known quarters are available."""
    q = quarterly.sort_index()
    out = {}
    for i in range(3, len(q)):
        window = q.iloc[i - 3 : i + 1]
        span = (window.index[-1] - window.index[0]).days
        # Four quarter-ends span about 9 months; a missing (NaN) quarter means no LTM figure.
        if 250 <= span <= 290 and window.notna().all():
            out[window.index[-1]] = window.sum()
    return pd.Series(out, dtype="float64")

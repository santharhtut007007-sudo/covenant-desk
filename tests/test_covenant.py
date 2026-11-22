"""Tests for the covenant calculation. No network needed."""

from datetime import date

import pytest

from covenant_desk.covenant import (
    GTX_LEVERAGE_COVENANT,
    CovenantTerms,
    headroom_ebitda_drop,
    leverage_history,
)


def quarter_rows(values, start_year=2025):
    ends = ["03-31", "06-30", "09-30", "12-31"]
    starts = ["01-01", "04-01", "07-01", "10-01"]
    return [
        {"start": f"{start_year}-{starts[i]}", "end": f"{start_year}-{ends[i]}", "val": v,
         "filed": "2026-02-01", "form": "10-K"}
        for i, v in enumerate(values)
    ]


def test_headroom_formula():
    # 2.35x against a 4.70x limit: EBITDA can halve before a breach.
    assert headroom_ebitda_drop(2.35, 4.70) == pytest.approx(0.5)
    assert headroom_ebitda_drop(4.70, 4.70) == pytest.approx(0.0)
    with pytest.raises(ValueError):
        headroom_ebitda_drop(0, 4.70)


def test_leverage_history_end_to_end():
    # EBITDA of 50 a quarter (NI 30 + interest 10 + tax 5 + D&A 5) -> LTM 200. Debt 600 -> 3.0x.
    usd = lambda rows: {"units": {"USD": rows}}
    facts = {"facts": {"us-gaap": {
        "NetIncomeLoss": usd(quarter_rows([30, 30, 30, 30])),
        "InterestExpense": usd(quarter_rows([10, 10, 10, 10])),
        "IncomeTaxExpenseBenefit": usd(quarter_rows([5, 5, 5, 5])),
        "Depreciation": usd(quarter_rows([5, 5, 5, 5])),
        "LongTermDebtNoncurrent": usd([{"end": "2025-12-31", "val": 590, "filed": "2026-02-01", "form": "10-K"}]),
        "LongTermDebtCurrent": usd([{"end": "2025-12-31", "val": 10, "filed": "2026-02-01", "form": "10-K"}]),
    }}}
    terms = CovenantTerms(name="test", metric="gross_total_leverage", max_ratio=4.0,
                          applies_to="all", citations=[])
    row = leverage_history(facts, terms).loc[date(2025, 12, 31)]
    assert row["ltm_ebitda"] == 200
    assert row["gross_debt"] == 600
    assert row["leverage"] == pytest.approx(3.0)
    assert row["ebitda_cushion"] == pytest.approx(0.25)
    assert bool(row["in_compliance"])


def test_gtx_terms_are_cited():
    t = GTX_LEVERAGE_COVENANT
    assert t.max_ratio == 4.70 and t.springing_threshold == 0.35
    assert "4.70 to 1.00" in t.citations[0].quote


def _usd(rows):
    return {"units": {"USD": rows}}


def test_missing_interest_makes_ebitda_unknown_not_zero():
    # Bug guard: a company that stops tagging interest must not get EBITDA without interest.
    from covenant_desk.covenant import EBITDA_COLUMN, ebitda_build

    facts = {"facts": {"us-gaap": {
        "NetIncomeLoss": _usd(quarter_rows([30, 30, 30, 30])),
        "InterestExpense": _usd(quarter_rows([10, 10, 10])),  # Q4 missing
        "IncomeTaxExpenseBenefit": _usd(quarter_rows([5, 5, 5, 5])),
        "Depreciation": _usd(quarter_rows([5, 5, 5, 5])),
    }}}
    build = ebitda_build(facts)
    assert build[EBITDA_COLUMN].iloc[:3].tolist() == [50, 50, 50]
    assert build[EBITDA_COLUMN].isna().iloc[3]


def test_gross_debt_falls_back_to_total_debt_concept():
    from covenant_desk.covenant import gross_debt

    row = lambda end, val: {"end": end, "val": val, "filed": "2026-02-01", "form": "10-K"}
    facts = {"facts": {"us-gaap": {
        # Old years: split into current and non-current.
        "LongTermDebtNoncurrent": _usd([row("2022-12-31", 900)]),
        "LongTermDebtCurrent": _usd([row("2022-12-31", 100)]),
        # Recent years: the company switched to one total-debt concept.
        "LongTermDebt": _usd([row("2022-12-31", 999), row("2025-12-31", 1200)]),
    }}}
    debt = gross_debt(facts)
    assert debt[date(2022, 12, 31)] == 1000  # preferred option wins where available
    assert debt[date(2025, 12, 31)] == 1200  # fallback fills later dates


def test_staleness():
    from covenant_desk.covenant import is_stale

    assert not is_stale(date(2026, 9, 30), date(2026, 11, 23))
    assert is_stale(date(2022, 9, 30), date(2026, 11, 23))


def test_discontinued_operations_gain_is_removed():
    # Like Dana in Q1 2026: net income is inflated by a gain on a business sold.
    from covenant_desk.covenant import EBITDA_COLUMN, ebitda_build

    facts = {"facts": {"us-gaap": {
        "NetIncomeLoss": _usd(quarter_rows([1130, 30, 30, 30])),
        "IncomeLossFromDiscontinuedOperationsNetOfTax": _usd(quarter_rows([1100, 0, 0, 0])),
        "InterestExpense": _usd(quarter_rows([10, 10, 10, 10])),
        "IncomeTaxExpenseBenefit": _usd(quarter_rows([5, 5, 5, 5])),
        "Depreciation": _usd(quarter_rows([5, 5, 5, 5])),
    }}}
    assert ebitda_build(facts)[EBITDA_COLUMN].tolist() == [50, 50, 50, 50]

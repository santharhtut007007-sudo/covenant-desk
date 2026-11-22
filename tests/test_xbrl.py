"""Tests for rebuilding quarterly numbers from messy XBRL facts. No network needed."""

from datetime import date

import pytest

from covenant_desk.xbrl import instant_series, ltm, quarterly_flow


def make_facts(concept: str, rows: list[dict]) -> dict:
    """Build a minimal companyfacts-shaped dict."""
    for i, row in enumerate(rows):
        row.setdefault("filed", f"2026-01-{i + 1:02d}")
        row.setdefault("form", "10-Q")
    return {"facts": {"us-gaap": {concept: {"units": {"USD": rows}}}}}


def test_q4_is_annual_minus_nine_months():
    # Like a real company: 10-Qs report quarters, the 10-K reports only the full year.
    facts = make_facts("NetIncomeLoss", [
        {"start": "2025-01-01", "end": "2025-03-31", "val": 10},
        {"start": "2025-04-01", "end": "2025-06-30", "val": 20},
        {"start": "2025-01-01", "end": "2025-06-30", "val": 30},
        {"start": "2025-07-01", "end": "2025-09-30", "val": 30},
        {"start": "2025-01-01", "end": "2025-09-30", "val": 60},
        {"start": "2025-01-01", "end": "2025-12-31", "val": 100, "form": "10-K"},
    ])
    q = quarterly_flow(facts, ["NetIncomeLoss"])
    assert q[date(2025, 12, 31)] == 40  # 100 - 60
    assert list(q.values) == [10, 20, 30, 40]


def test_quarters_from_year_to_date_only():
    # Cash-flow items (like depreciation) are often reported only year-to-date.
    facts = make_facts("Depreciation", [
        {"start": "2026-01-01", "end": "2026-03-31", "val": 25},
        {"start": "2026-01-01", "end": "2026-06-30", "val": 49},
        {"start": "2026-01-01", "end": "2026-09-30", "val": 73},
    ])
    q = quarterly_flow(facts, ["Depreciation"])
    assert list(q.values) == [25, 24, 24]


def test_restated_value_uses_latest_filing():
    facts = make_facts("NetIncomeLoss", [
        {"start": "2026-01-01", "end": "2026-03-31", "val": 50, "filed": "2026-05-01"},
        {"start": "2026-01-01", "end": "2026-03-31", "val": 45, "filed": "2026-08-01"},
    ])
    assert quarterly_flow(facts, ["NetIncomeLoss"]).iloc[0] == 45


def test_concept_priority_and_fallback():
    facts = make_facts("InterestExpense", [{"start": "2023-01-01", "end": "2023-03-31", "val": 7}])
    facts["facts"]["us-gaap"]["InterestExpenseOperating"] = {"units": {"USD": [
        {"start": "2026-01-01", "end": "2026-03-31", "val": 27, "filed": "2026-05-01", "form": "10-Q"},
    ]}}
    q = quarterly_flow(facts, ["InterestExpenseOperating", "InterestExpense"])
    assert q[date(2023, 3, 31)] == 7  # old concept fills the past
    assert q[date(2026, 3, 31)] == 27  # new concept wins where it exists


def test_ltm_needs_four_consecutive_quarters():
    facts = make_facts("Revenues", [
        {"start": s, "end": e, "val": v}
        for s, e, v in [
            ("2025-01-01", "2025-03-31", 1), ("2025-04-01", "2025-06-30", 2),
            ("2025-07-01", "2025-09-30", 3), ("2025-10-01", "2025-12-31", 4),
            ("2026-04-01", "2026-06-30", 5),  # Q1 2026 missing -> no LTM at Jun-26
        ]
    ])
    result = ltm(quarterly_flow(facts, ["Revenues"]))
    assert result.to_dict() == {date(2025, 12, 31): 10}


def test_instant_series():
    facts = make_facts("LongTermDebtNoncurrent", [
        {"end": "2026-06-30", "val": 1360},
        {"end": "2026-09-30", "val": 1359},
    ])
    s = instant_series(facts, ["LongTermDebtNoncurrent"])
    assert s[date(2026, 9, 30)] == pytest.approx(1359)


def test_sign_flipped_concept():
    facts = make_facts("InterestIncomeExpenseNonoperatingNet", [
        {"start": "2026-01-01", "end": "2026-03-31", "val": -27},
    ])
    q = quarterly_flow(facts, ["InterestExpense", "-InterestIncomeExpenseNonoperatingNet"])
    assert q.iloc[0] == 27

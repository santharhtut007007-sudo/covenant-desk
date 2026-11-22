"""Tests for retrieval, the agent's code checks, and evaluation scoring. No network or LLM needed."""

import pytest

from covenant_desk.agent import (
    ExtractedCovenant,
    ExtractionResult,
    VerifiedCovenant,
    expected_direction,
    expected_family,
    verify,
)
from covenant_desk.evaluate import score_borrower, wilson
from covenant_desk.sections import (
    find_covenant_sections,
    split_definitions,
    split_sections,
)

AGREEMENT = """TABLE OF CONTENTS
SECTION 6.12. Limitation on Liens 140
SECTION 6.13. Consolidated Total Leverage Ratio 141

SECTION 6.01. Indebtedness. The Borrower may incur Indebtedness if the Fixed Charge Coverage Ratio
would be at least 2.00 to 1.00 on a pro forma basis.
SECTION 6.12. Limitation on Liens. The Borrower will not create Liens.
SECTION 6.13. Consolidated Total Leverage Ratio. Solely with respect to the Revolving Facility and
so long as on the last day of any fiscal quarter Revolving Loans outstanding exceeds 35.0% of the
Revolving Commitments, Holdings will not permit the Consolidated Total Leverage Ratio to exceed 4.70 to 1.00.
Section 6.13 (including any defined terms) may be amended with the consent of the Required Revolving Lenders.
SECTION 7.01. Events of Default. If any of the following occurs.
“Consolidated EBITDA” means net income plus interest.
“Revolving Commitments” means the commitments of the revolving lenders.
"""


def covenant(**kw) -> ExtractedCovenant:
    base = {"name": "Consolidated Total Leverage Ratio", "metric": "total_leverage", "direction": "maximum",
                "threshold": 4.70, "threshold_unit": "ratio", "applies_to": "Revolving Facility", "springing": True,
                "section": "6.13",
                "quote": "Holdings will not permit the Consolidated Total Leverage Ratio to exceed 4.70 to 1.00"}
    base.update(kw)
    return ExtractedCovenant(**base)


def test_sections_skip_table_of_contents_and_cross_references():
    secs = split_sections(AGREEMENT)
    assert secs["6.13"].title == "Consolidated Total Leverage Ratio"
    assert "will not permit" in secs["6.13"].text  # body copy, not the TOC line
    defs = split_definitions(AGREEMENT)
    assert set(defs) == {"Consolidated EBITDA", "Revolving Commitments"}


def test_retrieval_prefers_maintenance_covenant_over_incurrence_test():
    top = find_covenant_sections(split_sections(AGREEMENT))
    assert top[0].number == "6.13"


def test_verify_accepts_correct_answer():
    secs = split_sections(AGREEMENT)
    checks = verify(covenant(), AGREEMENT, secs)
    assert all(checks.values()), checks


@pytest.mark.parametrize("change, failing_check", [
    ({"quote": "Holdings shall keep leverage under 4.70"}, "quote_in_document"),
    ({"threshold": 5.0}, "threshold_in_quote"),
    ({"section": "9.99"}, "section_exists"),
    ({"direction": "minimum"}, "direction_consistent"),
    ({"springing": False}, "springing_consistent"),
    ({"metric": "minimum_liquidity"}, "metric_consistent"),
])
def test_verify_catches_each_kind_of_error(change, failing_check):
    checks = verify(covenant(**change), AGREEMENT, split_sections(AGREEMENT))
    assert checks[failing_check] is False
    assert sum(not ok for ok in checks.values()) == 1  # only the intended check fails


def test_direction_and_family_from_wording():
    assert expected_direction("Permit the Loan-to-Value Ratio to be greater than or equal to 0.70") == "maximum"
    assert expected_direction("Permit Free Liquidity to be less than $250,000,000") == "minimum"
    assert expected_direction("shall maintain a Fixed Charge Coverage Ratio of at least 1.0 to 1.0") == "minimum"
    assert expected_family("Permit Free Liquidity to be less than $250,000,000") == {"minimum_liquidity"}
    assert "other" in expected_family("Permit the Loan-to-Value Ratio to be greater than 0.70")


def test_wilson_interval():
    lo, hi = wilson(7, 7)
    assert lo == pytest.approx(0.646, abs=1e-3) and hi == 1.0
    lo, hi = wilson(5, 10)
    assert lo == pytest.approx(0.237, abs=1e-3) and hi == pytest.approx(0.763, abs=1e-3)


def test_scoring_matches_by_section_and_family():
    families = {"leverage": ["total_leverage"], "coverage": ["interest_coverage"],
                "liquidity": ["minimum_liquidity"], "other": ["other"]}
    gold = {"covenant_lite": False, "covenants": [
        {"section": "6.13", "families": ["leverage"], "direction": "maximum", "thresholds": [4.70],
         "springing": True, "quote": "x"}]}
    ok = VerifiedCovenant(covenant=covenant(), checks={"a": True})
    wrong_spring = VerifiedCovenant(covenant=covenant(springing=False), checks={"a": True})
    extra = VerifiedCovenant(covenant=covenant(section="6.01", metric="interest_coverage"), checks={"a": False})

    def result(covs):
        return ExtractionResult(candidate_sections=[], covenants=covs, no_maintenance_covenant=not covs,
                                definitions_requested=[], reasoning="", retried=False, usage={})

    s = score_borrower(result([ok]), gold, families)
    assert s["lite_correct"] and s["matches"][0]["all_fields"] and not s["false_positives"]
    s = score_borrower(result([wrong_spring, extra]), gold, families)
    assert s["matches"][0]["springing"] is False
    assert len(s["false_positives"]) == 1
    s = score_borrower(result([]), gold, families)
    assert s["lite_correct"] is False and s["matches"][0]["found"] is False

"""Covenant definitions and the deterministic compliance calculation.

Design rule for the whole project: an AI agent may *propose* a `CovenantTerms` object
(reading the credit agreement and citing the clause), but every number below is computed by
this plain, tested Python code.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from pydantic import BaseModel, Field

from covenant_desk.xbrl import instant_series, ltm, quarterly_flow


class Citation(BaseModel):
    """Where in the credit agreement a term comes from."""

    document: str  # e.g. "GTX Amended and Restated Credit Agreement (2025-01-31)"
    section: str  # e.g. "Section 6.13"
    quote: str  # exact text from the agreement


class CovenantTerms(BaseModel):
    """One maintenance covenant, as written in the credit agreement."""

    name: str
    metric: str = Field(description="Which ratio is tested, e.g. 'gross_total_leverage'")
    max_ratio: float
    applies_to: str = Field(description="Which facility the test protects, e.g. 'Revolving Facility only'")
    springing_threshold: float | None = Field(
        default=None,
        description="If set, the test applies only when revolver usage exceeds this share "
        "of revolver commitments (a 'springing' covenant).",
    )
    citations: list[Citation]


# ---------------------------------------------------------------------------- GTX terms
# Filled in by hand for now, from the 2025 Amended and Restated Credit Agreement.
# In a later phase the Contract Agent proposes this object and a human approves it.
GTX_LEVERAGE_COVENANT = CovenantTerms(
    name="Consolidated Gross Total Leverage Ratio",
    metric="gross_total_leverage",
    max_ratio=4.70,
    applies_to="Revolving Facility only (term loans are covenant-lite)",
    springing_threshold=0.35,
    citations=[
        Citation(
            document="GTX Amended and Restated Credit Agreement, Restatement Agreement 2025-01-31 (8-K EX-10.1)",
            section="Section 6.13",
            quote=(
                "so long as on the last day of any such fiscal quarter the aggregate amount of "
                "Revolving Loans and Letters of Credit outstanding [...] exceeds 35.0% of the "
                "aggregate amounts of all Revolving Commitments in effect as of such date, "
                "Holdings will not permit the Consolidated Gross Total Leverage Ratio to exceed "
                "4.70 to 1.00."
            ),
        )
    ],
)


class FacilitySnapshot(BaseModel):
    """Revolver usage on a test date. Needed to know whether a springing covenant applies."""

    as_of: date
    revolver_commitment: float
    revolver_outstanding: float  # loans + letters of credit that count toward the test
    citation: Citation

    @property
    def usage(self) -> float:
        return self.revolver_outstanding / self.revolver_commitment


def covenant_is_tested(terms: CovenantTerms, snapshot: FacilitySnapshot | None) -> bool | None:
    """True/False for a springing covenant; True for a normal one; None if usage is unknown."""
    if terms.springing_threshold is None:
        return True
    if snapshot is None:
        return None
    return snapshot.usage > terms.springing_threshold


GTX_FACILITY_Q3_2026 = FacilitySnapshot(
    as_of=date(2026, 9, 30),
    revolver_commitment=630e6,
    revolver_outstanding=0.0,
    citation=Citation(
        document="GTX 10-Q for the quarter ended 2026-09-30 (filed 2026-10-29)",
        section="MD&A, Liquidity and Capital Resources",
        quote="Revolving Facility - borrowings or letters of credit outstanding— —",
    ),
)


def quote_in_document(quote: str, document_text: str) -> bool:
    """Code check that a cited quote really appears in the source document.

    Whitespace and case are normalised. "[...]" in a quote marks an omitted passage, so each
    piece must appear, in order.
    """
    norm = lambda s: " ".join(s.split()).lower()
    text = norm(document_text)
    position = 0
    for piece in (norm(p) for p in quote.split("[...]")):
        found = text.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    return True


# ---------------------------------------------------------------------------- EBITDA build
# Each line maps a clause of the "Consolidated EBITDA" definition to XBRL concepts.
# Earlier concepts take priority when a company switched concepts over time.
#
# required=True: a core line (net income, interest, taxes, D&A). If it is missing for a quarter,
# that quarter's EBITDA is unknown. Treating it as zero would silently overstate leverage.
# required=False: an add-back that is legitimately absent in many quarters (counts as zero).
EBITDA_BUILD: list[tuple[str, str, list[str], bool]] = [
    ("Net income", "Consolidated Net Income", ["NetIncomeLoss"], True),
    ("+ Interest expense", "clause (a)(i)",
     ["InterestExpenseNonoperating", "InterestExpenseOperating", "InterestExpenseDebt",
      "InterestExpense",
      # Last resort: net interest (interest income offsets expense; slightly understates EBITDA).
      "-InterestIncomeExpenseNonoperatingNet"], True),
    ("+ Income taxes", "clause (a)(ii)", ["IncomeTaxExpenseBenefit"], True),
    ("+ Depreciation & amortization", "clause (a)(iii)",
     ["DepreciationDepletionAndAmortization", "DepreciationAndAmortization",
      "DepreciationAmortizationAndAccretionNet", "Depreciation", "DepreciationNonproduction"], True),
    ("+ Impairments (non-cash)", "clause (a)(vi)",
     ["AssetImpairmentCharges", "GoodwillImpairmentLoss"], False),
    ("+ Restructuring charges", "clause (a)(ix)(A)", ["RestructuringCharges"], False),
    # Discontinued operations are excluded (clause (a)(x)): remove their profit, add back losses.
    ("- Discontinued operations", "clause (a)(x)",
     ["-IncomeLossFromDiscontinuedOperationsNetOfTax",
      "-IncomeLossFromDiscontinuedOperationsNetOfTaxAttributableToReportingEntity"], False),
]
EBITDA_COLUMN = "Covenant EBITDA (XBRL proxy)"

# Ways companies report total debt, best first. Each option is a list of concepts to add up.
# The first concept in an option must exist on a date for that option to be used there.
DEBT_OPTIONS: list[list[str]] = [
    ["LongTermDebtNoncurrent", "LongTermDebtCurrent"],
    ["LongTermDebt"],  # total, including the current portion
    ["LongTermDebtAndCapitalLeaseObligations", "LongTermDebtAndCapitalLeaseObligationsCurrent"],
    ["DebtAndCapitalLeaseObligations"],
]


def ebitda_build(company_facts: dict) -> pd.DataFrame:
    """Quarterly covenant-EBITDA components, one column per line of the definition."""
    cols = {label: quarterly_flow(company_facts, concepts) for label, _, concepts, _ in EBITDA_BUILD}
    df = pd.DataFrame(cols).sort_index()
    required = [label for label, _, _, req in EBITDA_BUILD if req]
    optional = [label for label, _, _, req in EBITDA_BUILD if not req]
    df = df.dropna(subset=["Net income"])
    df[optional] = df[optional].fillna(0.0)
    # sum(skipna=False): any missing required line makes the quarter's EBITDA NaN.
    df[EBITDA_COLUMN] = df[required + optional].sum(axis=1, skipna=False)
    return df


def gross_debt(company_facts: dict) -> pd.Series:
    """Total debt by date, using the best reporting option available on each date."""
    result = pd.Series(dtype="float64")
    for option in reversed(DEBT_OPTIONS):
        anchor = instant_series(company_facts, [option[0]])
        if anchor.empty:
            continue
        total = anchor.copy()
        for extra in option[1:]:
            total = total.add(instant_series(company_facts, [extra]), fill_value=0.0).loc[anchor.index]
        result = total.combine_first(result) if not result.empty else total
    return result.sort_index()


def is_stale(as_of: date, today: date, max_days: int = 200) -> bool:
    """A quarter more than about two reporting cycles old should not be shown as current."""
    return (today - as_of).days > max_days


def leverage_history(company_facts: dict, terms: CovenantTerms) -> pd.DataFrame:
    """Quarter-by-quarter covenant test: LTM EBITDA, gross debt, ratio, headroom."""
    q = ebitda_build(company_facts)
    ltm_ebitda = ltm(q[EBITDA_COLUMN])
    debt = gross_debt(company_facts)
    df = pd.DataFrame({"ltm_ebitda": ltm_ebitda, "gross_debt": debt}).dropna()
    df["leverage"] = df["gross_debt"] / df["ltm_ebitda"]
    df["max_leverage"] = terms.max_ratio
    # Headroom = how far EBITDA can fall before the covenant breaks.
    df["ebitda_cushion"] = 1 - df["leverage"] / terms.max_ratio
    df["in_compliance"] = df["leverage"] <= terms.max_ratio
    return df


def headroom_ebitda_drop(leverage: float, max_ratio: float) -> float:
    """Share of EBITDA that can be lost, at constant debt, before leverage hits the limit.

    leverage = D / E. Breach when D / E' = max  ->  E' = D / max  ->  drop = 1 - leverage / max.
    """
    if leverage <= 0:
        raise ValueError("leverage must be positive")
    return 1 - leverage / max_ratio


def as_of(df: pd.DataFrame, when: date) -> pd.Series:
    """Most recent covenant test on or before a date."""
    return df[df.index <= when].iloc[-1]

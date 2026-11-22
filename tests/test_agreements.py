"""Tests for exhibit classification, the agreement in force, and quote checking."""

from datetime import date

from covenant_desk.agreements import ClassifiedDoc, DocKind, agreement_in_force, classify
from covenant_desk.covenant import (
    GTX_FACILITY_Q3_2026,
    GTX_LEVERAGE_COVENANT,
    CovenantTerms,
    covenant_is_tested,
    quote_in_document,
)
from covenant_desk.edgar import FilingDocument

BODY = " filler" * 50 + " CREDIT AGREEMENT"


def test_classify_titles():
    assert classify("EXHIBIT 10.1\nCREDIT AGREEMENT\ndated as of 2018") is DocKind.CREDIT_AGREEMENT
    assert classify("EXHIBIT 10.1\nRESTATEMENT AGREEMENT\ndated 2025" + BODY) is DocKind.RESTATEMENT
    assert classify("EXHIBIT 10.1\nAMENDMENT NO. 2\nTO AMENDED AND RESTATED CREDIT AGREEMENT") is DocKind.AMENDMENT
    assert classify("SENIOR SECURED SUPER-PRIORITY DEBTOR-IN-POSSESSION\nCREDIT AGREEMENT") is DocKind.DIP
    assert classify("EXHIBIT 10.2\nINTERCREDITOR AGREEMENT\namong") is DocKind.OTHER
    # A credit-agreement amendment that later *mentions* a purchase agreement is still an amendment.
    text = "FIRST AMENDMENT TO THE CREDIT AGREEMENT\n" + "x\n" * 20 + "SHARE PURCHASE AGREEMENT"
    assert classify(text) is DocKind.AMENDMENT


def _doc(day: str, kind: DocKind, mentions=()) -> ClassifiedDoc:
    d = FilingDocument(1, f"acc-{day}", "8-K", day, "EX-10.1", "x.htm", "")
    return ClassifiedDoc(d, kind, "", 100, list(mentions))


def test_agreement_in_force_uses_latest_base_and_later_amendments():
    docs = [
        _doc("2018-10-01", DocKind.CREDIT_AGREEMENT),
        _doc("2020-06-12", DocKind.AMENDMENT),  # amends the 2018 agreement: superseded
        _doc("2020-10-09", DocKind.DIP),  # bankruptcy loan: never the base
        _doc("2021-04-30", DocKind.CREDIT_AGREEMENT),
        _doc("2025-01-31", DocKind.RESTATEMENT),
        _doc("2025-08-07", DocKind.AMENDMENT),
        _doc("2026-05-20", DocKind.AMENDMENT, ["...Leverage Ratio..."]),
    ]
    inforce = agreement_in_force(docs)
    assert inforce.base.doc.file_date == "2025-01-31"
    assert [a.doc.file_date for a in inforce.amendments] == ["2025-08-07", "2026-05-20"]
    assert [a.doc.file_date for a in inforce.amendments_touching_covenant] == ["2026-05-20"]


def test_quote_check_handles_spacing_and_ellipsis():
    source = "Holdings will not\npermit the  Ratio to exceed 4.70 to 1.00. Other words here."
    assert quote_in_document("holdings will not permit the ratio", source)
    assert quote_in_document("Holdings will not [...] exceed 4.70 to 1.00", source)
    assert not quote_in_document("exceed 5.00 to 1.00", source)
    assert not quote_in_document("4.70 to 1.00 [...] Holdings", source)  # wrong order


def test_springing_covenant_logic():
    # GTX: revolver undrawn at Q3 2026, so the 35% springing test does not apply.
    assert GTX_FACILITY_Q3_2026.usage == 0
    assert covenant_is_tested(GTX_LEVERAGE_COVENANT, GTX_FACILITY_Q3_2026) is False
    assert covenant_is_tested(GTX_LEVERAGE_COVENANT, None) is None
    drawn = GTX_FACILITY_Q3_2026.model_copy(update={"revolver_outstanding": 300e6})
    assert covenant_is_tested(GTX_LEVERAGE_COVENANT, drawn) is True  # 48% > 35%
    always = CovenantTerms(name="x", metric="m", max_ratio=4, applies_to="all", citations=[])
    assert covenant_is_tested(always, None) is True
    assert GTX_FACILITY_Q3_2026.as_of == date(2026, 9, 30)


def test_conformed_copy_amendment_and_other_titles():
    long_amendment = "AMENDMENT NO. 8 TO CREDIT AND GUARANTY AGREEMENT\n" + "word " * 45_000
    assert classify(long_amendment) is DocKind.AMENDMENT_WITH_CONFORMED_COPY
    assert classify("AMENDMENT NO. 2 TO THIRD AMENDED AND RESTATED LOAN AGREEMENT") is DocKind.AMENDMENT
    assert classify("$500,000,000 REVOLVING CREDIT AND GUARANTY AGREEMENT") is DocKind.CREDIT_AGREEMENT
    assert classify("TRANSACTION SUPPORT AGREEMENT\nCOOPER-STANDARD") is DocKind.OTHER
    assert classify("364-DAY BRIDGE FACILITY AND GUARANTY AGREEMENT") is DocKind.OTHER


def test_conformed_copy_becomes_the_base():
    docs = [
        _doc("2016-06-09", DocKind.CREDIT_AGREEMENT),
        _doc("2025-08-01", DocKind.AMENDMENT_WITH_CONFORMED_COPY),
        _doc("2026-07-10", DocKind.AMENDMENT),
    ]
    inforce = agreement_in_force(docs)
    assert inforce.base.doc.file_date == "2025-08-01"
    assert [a.doc.file_date for a in inforce.amendments] == ["2026-07-10"]

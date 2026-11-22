"""Checks against the real SEC data. Run with: uv run pytest -m live"""

import pytest

from covenant_desk.agreements import agreement_in_force
from covenant_desk.covenant import GTX_FACILITY_Q3_2026, GTX_LEVERAGE_COVENANT, quote_in_document
from covenant_desk.documents import load_text
from covenant_desk.edgar import EdgarClient

pytestmark = pytest.mark.live
GTX_CIK = 1735707


@pytest.fixture(scope="module")
def client():
    return EdgarClient()


def test_ticker_lookup(client):
    assert client.cik_for_ticker("GTX") == GTX_CIK


def test_gtx_restated_credit_agreement_contains_covenant(client):
    docs = client.search_exhibits(GTX_CIK)
    restated = next(d for d in docs if d.file_date == "2025-01-31" and d.doc_type == "EX-10.1")
    text = load_text(client.download(restated))
    # Every quote we rely on must really be in the agreement (spacing normalised).
    for citation in GTX_LEVERAGE_COVENANT.citations:
        assert quote_in_document(citation.quote, text)


def test_gtx_revolver_quote_in_10q(client):
    tenq = next(d for d in client.recent_filings(GTX_CIK, ("10-Q",)) if d.description == "2026-09-30")
    text = load_text(client.download(tenq))
    assert quote_in_document(GTX_FACILITY_Q3_2026.citation.quote, text)


def test_gtx_agreement_in_force(client):
    from covenant_desk.pipeline import classify_exhibits

    inforce = agreement_in_force(classify_exhibits(client, GTX_CIK))
    assert inforce.base.doc.file_date == "2025-01-31"
    assert [a.doc.file_date for a in inforce.amendments] == ["2025-08-07", "2026-05-20"]


def test_answer_key_quotes_are_in_the_filings(client):
    """Every quote in the hand-made answer key must appear in that borrower's agreement in force."""
    import json

    from covenant_desk.evaluate import GOLD_FILE
    from covenant_desk.pipeline import classify_exhibits

    gold = json.loads(GOLD_FILE.read_text())
    for ticker, g in gold["borrowers"].items():
        if not g["covenants"]:
            continue
        inforce = agreement_in_force(classify_exhibits(client, client.cik_for_ticker(ticker)))
        text = load_text(client.download(inforce.base.doc))
        for cov in g["covenants"]:
            assert quote_in_document(cov["quote"], text), (ticker, cov["section"])

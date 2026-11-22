"""Command line entry point: `uv run covenant-desk GTX`."""

from __future__ import annotations

import argparse

from covenant_desk.agreements import agreement_in_force
from covenant_desk.covenant import (
    GTX_FACILITY_Q3_2026,
    GTX_LEVERAGE_COVENANT,
    covenant_is_tested,
    ebitda_build,
    leverage_history,
)
from covenant_desk.edgar import EdgarClient
from covenant_desk.pipeline import classify_exhibits

# Covenant terms we have reviewed so far. Phase 3 replaces this with agent-extracted terms.
KNOWN_TERMS = {"GTX": GTX_LEVERAGE_COVENANT}
KNOWN_FACILITIES = {"GTX": GTX_FACILITY_Q3_2026}


def main() -> None:
    parser = argparse.ArgumentParser(description="Covenant compliance from SEC filings")
    parser.add_argument("ticker", help="e.g. GTX")
    parser.add_argument("--quarters", type=int, default=8, help="how many quarters to show")
    args = parser.parse_args()
    ticker = args.ticker.upper()
    terms = KNOWN_TERMS.get(ticker)
    if terms is None:
        parser.error(f"No reviewed covenant terms for {ticker} yet. Known: {', '.join(KNOWN_TERMS)}")

    client = EdgarClient()
    cik = client.cik_for_ticker(ticker)
    facts = client.company_facts(cik)
    history = leverage_history(facts, terms).tail(args.quarters)
    build = ebitda_build(facts).tail(4)
    inforce = agreement_in_force(classify_exhibits(client, cik))

    print(f"\n{ticker}: agreement in force")
    print(f"  Base: {inforce.base.kind.value} filed {inforce.base.doc.file_date}  {inforce.base.doc.url}")
    for a in inforce.amendments:
        flag = "MENTIONS COVENANT - review" if a.covenant_mentions else "no covenant change found"
        print(f"  + Amendment filed {a.doc.file_date}: {flag}")

    print(f"\n{terms.name}  (max {terms.max_ratio:.2f}x, {terms.applies_to})")
    for c in terms.citations:
        print(f"  Source: {c.document}, {c.section}")
    if terms.springing_threshold:
        print(f"  Springing: tested only when revolver usage > {terms.springing_threshold:.0%} of commitments")
        facility = KNOWN_FACILITIES.get(ticker)
        tested = covenant_is_tested(terms, facility)
        if facility:
            state = "TESTED" if tested else "NOT TESTED this quarter"
            print(f"  Revolver usage at {facility.as_of}: {facility.usage:.0%} of "
                  f"${facility.revolver_commitment / 1e6:,.0f}M -> {state}")
        else:
            print("  Revolver usage unknown -> cannot tell whether the test applies")

    print("\nLast 4 quarters of covenant EBITDA ($M):")
    print((build / 1e6).round(0).to_string())

    print("\nCovenant test by quarter:")
    print(f"  {'Quarter':<12}{'LTM EBITDA':>12}{'Gross debt':>12}{'Leverage':>10}{'Cushion':>10}  Status")
    for when, r in history.iterrows():
        status = "PASS" if r["in_compliance"] else "BREACH"
        print(f"  {when!s:<12}{r['ltm_ebitda'] / 1e6:>11,.0f}M{r['gross_debt'] / 1e6:>11,.0f}M"
              f"{r['leverage']:>9.2f}x{r['ebitda_cushion']:>10.0%}  {status}")
    last = history.iloc[-1]
    print(f"\nEBITDA could fall {last['ebitda_cushion']:.0%} (at today's debt) before the "
          f"{terms.max_ratio:.2f}x limit is reached.")
    print("Note: EBITDA here is a proxy built from XBRL. The agreement allows further add-backs "
          "(e.g. run-rate cost savings) that are not in the filings' XBRL data.\n")


if __name__ == "__main__":
    main()

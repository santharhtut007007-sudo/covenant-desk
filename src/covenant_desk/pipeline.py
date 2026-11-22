"""Run the data pipeline for every borrower in universe.csv and summarise the results."""

from __future__ import annotations

import csv
from dataclasses import asdict, dataclass
from datetime import date

import pandas as pd

from covenant_desk import config
from covenant_desk.agreements import (
    ClassifiedDoc,
    DocKind,
    agreement_in_force,
    classify,
    covenant_mentions,
    heading,
)
from covenant_desk.covenant import EBITDA_COLUMN, ebitda_build, gross_debt, is_stale
from covenant_desk.documents import load_text
from covenant_desk.edgar import EdgarClient
from covenant_desk.xbrl import ltm

UNIVERSE_FILE = config.PROJECT_ROOT / "universe.csv"
# Patterns that suggest an amendment changes a financial covenant (checked by a human or agent).
COVENANT_PATTERNS = [r"Leverage Ratio", r"Coverage Ratio", r"Financial Covenant", r"Relief Period"]


@dataclass
class BorrowerSummary:
    ticker: str
    cik: int | None = None
    exhibits_found: int = 0
    base_agreement_date: str | None = None
    base_agreement_kind: str | None = None
    base_agreement_url: str | None = None
    amendments_since_base: int = 0
    amendments_touching_covenants: int = 0
    latest_quarter: str | None = None
    ltm_ebitda_musd: float | None = None
    gross_debt_musd: float | None = None
    gross_leverage: float | None = None
    stale: bool | None = None
    missing_lines: str | None = None
    error: str | None = None


def load_universe() -> list[dict]:
    with open(UNIVERSE_FILE) as f:
        return list(csv.DictReader(f))


def classify_exhibits(client: EdgarClient, cik: int, since: str = "2016-01-01") -> list[ClassifiedDoc]:
    out = []
    for doc in client.search_exhibits(cik):
        if doc.file_date < since:
            continue
        text = load_text(client.download(doc))
        kind = classify(text)
        mentions = covenant_mentions(text, COVENANT_PATTERNS) if kind is DocKind.AMENDMENT else []
        out.append(ClassifiedDoc(doc, kind, heading(text, 12)[:200], len(text.split()), mentions))
    return out


def run_borrower(client: EdgarClient, ticker: str) -> BorrowerSummary:
    s = BorrowerSummary(ticker=ticker)
    try:
        s.cik = client.cik_for_ticker(ticker)
        docs = classify_exhibits(client, s.cik)
        s.exhibits_found = len(docs)
        try:
            inforce = agreement_in_force(docs)
            s.base_agreement_date = inforce.base.doc.file_date
            s.base_agreement_kind = inforce.base.kind.value
            s.base_agreement_url = inforce.base.doc.url
            s.amendments_since_base = len(inforce.amendments)
            s.amendments_touching_covenants = len(inforce.amendments_touching_covenant)
        except LookupError as e:
            s.error = str(e)

        facts = client.company_facts(s.cik)
        build = ebitda_build(facts)
        ebitda = ltm(build[EBITDA_COLUMN])
        debt = gross_debt(facts)
        joined = pd.DataFrame({"ebitda": ebitda, "debt": debt}).dropna()
        if not joined.empty:
            when, row = joined.index[-1], joined.iloc[-1]
            s.latest_quarter = str(when)
            s.stale = is_stale(when, date.today())
            s.ltm_ebitda_musd = round(row["ebitda"] / 1e6, 1)
            s.gross_debt_musd = round(row["debt"] / 1e6, 1)
            if row["ebitda"] > 0:
                s.gross_leverage = round(row["debt"] / row["ebitda"], 2)
        # Say which line is missing in the most recent quarters, if any.
        recent = build.tail(4)
        s.missing_lines = ", ".join(c for c in recent.columns if recent[c].isna().any()) or None
        if debt.empty or is_stale(debt.index[-1], date.today()):
            s.missing_lines = ", ".join(filter(None, [s.missing_lines, "gross debt"]))
    except Exception as e:  # noqa: BLE001 - one bad borrower should not stop the batch
        s.error = f"{type(e).__name__}: {e}"
    return s


def run_universe() -> pd.DataFrame:
    client = EdgarClient()
    rows = []
    for entry in load_universe():
        summary = run_borrower(client, entry["ticker"])
        print(f"{summary.ticker:6s} exhibits={summary.exhibits_found:3d} "
              f"base={summary.base_agreement_date} ({summary.base_agreement_kind}) "
              f"q={summary.latest_quarter} lev={summary.gross_leverage} stale={summary.stale} "
              f"missing={summary.missing_lines} {summary.error or ''}", flush=True)
        rows.append({**asdict(summary), "sector": entry["sector"],
                     "proxy_note": entry.get("proxy_note") or None})
    df = pd.DataFrame(rows)
    out = config.DATA_DIR / "universe_summary.csv"
    df.to_csv(out, index=False)
    print(f"\nSaved {out}")
    return df


if __name__ == "__main__":
    run_universe()


def load_panel(client: EdgarClient | None = None, include_flagged: bool = False) -> dict[str, dict]:
    """Quarterly covenant-EBITDA and gross debt for each borrower whose proxy is usable."""
    client = client or EdgarClient()
    panel = {}
    for entry in load_universe():
        if entry.get("proxy_note") and not include_flagged:
            continue
        facts = client.company_facts(client.cik_for_ticker(entry["ticker"]))
        panel[entry["ticker"]] = {
            "quarterly_ebitda": ebitda_build(facts)[EBITDA_COLUMN],
            "debt": gross_debt(facts),
            "sector": entry["sector"],
        }
    return panel

"""Small SEC EDGAR client: company lookup, filing search, document download, XBRL facts.

Every response is cached on disk, so re-running the pipeline does not hit the SEC again
and results are reproducible from the cached files.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from pathlib import Path

import httpx

from covenant_desk import config

# The SEC allows at most 10 requests per second. Stay well under it.
_MIN_SECONDS_BETWEEN_REQUESTS = 0.15


@dataclass(frozen=True)
class FilingDocument:
    """One document inside a filing, e.g. the EX-10.1 credit agreement attached to an 8-K."""

    cik: int
    accession: str  # e.g. "0001735707-24-000045"
    form: str  # form of the parent filing, e.g. "8-K"
    file_date: str  # YYYY-MM-DD
    doc_type: str  # e.g. "EX-10.1"
    filename: str  # e.g. "exhibit101.htm"
    description: str

    @property
    def url(self) -> str:
        folder = self.accession.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{self.cik}/{folder}/{self.filename}"


class EdgarClient:
    def __init__(self, user_agent: str | None = None, cache_dir: Path | None = None):
        self.cache_dir = cache_dir or config.RAW_DIR
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._http = httpx.Client(
            headers={"User-Agent": user_agent or config.SEC_USER_AGENT},
            timeout=30,
            follow_redirects=True,
        )
        self._last_request = 0.0

    # ------------------------------------------------------------------ low level

    def _get(self, url: str, params: dict | None = None) -> bytes:
        """GET with on-disk caching and polite rate limiting."""
        key = url + ("?" + json.dumps(params, sort_keys=True) if params else "")
        cache_file = self.cache_dir / "http" / hashlib.sha256(key.encode()).hexdigest()[:32]
        if cache_file.exists():
            return cache_file.read_bytes()

        for attempt in range(4):
            wait = _MIN_SECONDS_BETWEEN_REQUESTS - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            response = self._http.get(url, params=params)
            self._last_request = time.monotonic()
            # SEC servers sometimes return 5xx or 429 under load: back off and retry.
            if response.status_code < 500 and response.status_code != 429:
                break
            time.sleep(2**attempt)
        response.raise_for_status()

        cache_file.parent.mkdir(parents=True, exist_ok=True)
        cache_file.write_bytes(response.content)
        return response.content

    def _get_json(self, url: str, params: dict | None = None) -> dict:
        return json.loads(self._get(url, params))

    # ------------------------------------------------------------------ companies

    def cik_for_ticker(self, ticker: str) -> int:
        tickers = self._get_json("https://www.sec.gov/files/company_tickers.json")
        for row in tickers.values():
            if row["ticker"].upper() == ticker.upper():
                return int(row["cik_str"])
        raise KeyError(f"Ticker {ticker!r} not found in SEC company list")

    # ------------------------------------------------------------------ filings

    def search_exhibits(
        self, cik: int, query: str = '"credit agreement"', doc_prefix: str = "EX-10"
    ) -> list[FilingDocument]:
        """Full-text search one company's filings and keep only exhibits of a given type.

        Credit agreements and their amendments are usually filed as EX-10.x exhibits
        (material contracts) attached to an 8-K, 10-Q or 10-K.
        """
        results: list[FilingDocument] = []
        start = 0
        while True:
            data = self._get_json(
                "https://efts.sec.gov/LATEST/search-index",
                params={"q": query, "ciks": f"{cik:010d}", "from": start},
            )
            hits = data["hits"]["hits"]
            for hit in hits:
                src = hit["_source"]
                doc_type = src.get("file_type") or ""
                if not doc_type.upper().startswith(doc_prefix):
                    continue
                accession, filename = hit["_id"].split(":", 1)
                results.append(
                    FilingDocument(
                        cik=cik,
                        accession=accession,
                        form=src.get("form", ""),
                        file_date=src.get("file_date", ""),
                        doc_type=doc_type,
                        filename=filename,
                        description=src.get("file_description") or "",
                    )
                )
            start += len(hits)
            if not hits or start >= data["hits"]["total"]["value"]:
                break
        # Newest first, and drop duplicates (the same exhibit can match several times).
        unique = {(d.accession, d.filename): d for d in results}
        return sorted(unique.values(), key=lambda d: d.file_date, reverse=True)

    def download(self, doc: FilingDocument) -> Path:
        """Save a filing document under data/raw/<cik>/<accession>/<filename>."""
        target = self.cache_dir / str(doc.cik) / doc.accession / doc.filename
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(self._get(doc.url))
        return target

    def recent_filings(self, cik: int, forms: tuple[str, ...] = ("10-Q", "10-K")) -> list[FilingDocument]:
        """Recent filings of the given forms (newest first), pointing at the main document."""
        data = self._get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json")
        recent = data["filings"]["recent"]
        out = []
        for i, form in enumerate(recent["form"]):
            if form in forms:
                out.append(
                    FilingDocument(
                        cik=cik,
                        accession=recent["accessionNumber"][i],
                        form=form,
                        file_date=recent["filingDate"][i],
                        doc_type=form,
                        filename=recent["primaryDocument"][i],
                        description=recent["reportDate"][i],  # period the report covers
                    )
                )
        return out

    # ------------------------------------------------------------------ XBRL

    def company_facts(self, cik: int) -> dict:
        """All XBRL facts a company has ever reported (the SEC 'companyfacts' API)."""
        return self._get_json(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json")

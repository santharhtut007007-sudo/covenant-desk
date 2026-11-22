"""Work out which credit agreement is in force: the latest base agreement plus later amendments.

A borrower's EX-10 exhibits are a mix of credit agreements, amendments, restatements,
bankruptcy (DIP) loans and unrelated contracts (plan support agreements, purchase agreements).
Rule-based classification here is a first pass; the Contract Agent will take over hard cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum

from covenant_desk.edgar import FilingDocument


class DocKind(StrEnum):
    CREDIT_AGREEMENT = "credit_agreement"  # a new base agreement
    RESTATEMENT = "restatement"  # amends and restates the whole agreement -> new base
    AMENDMENT = "amendment"  # changes parts of the base agreement
    # A long amendment that attaches the whole agreement, as amended, as an annex
    # ("conformed copy"). Many borrowers never refile the base, so this becomes the new base.
    AMENDMENT_WITH_CONFORMED_COPY = "amendment_with_conformed_copy"
    DIP = "dip_facility"  # debtor-in-possession loan during bankruptcy
    OTHER = "other"


# Different names lenders use for the same thing.
CREDIT_AGREEMENT_RE = re.compile(
    r"(CREDIT|LOAN|FINANCING)( AND (GUARANTY|GUARANTEE))? AGREEMENT|FACILITY AGREEMENT"
)
# An amendment this long almost certainly contains the full conformed agreement.
CONFORMED_COPY_MIN_WORDS = 40_000


@dataclass
class ClassifiedDoc:
    doc: FilingDocument
    kind: DocKind
    heading: str
    words: int
    covenant_mentions: list[str] = field(default_factory=list)


def heading(text: str, n_lines: int = 40) -> str:
    """Upper-case opening lines, where the agreement names itself."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    return " ".join(lines[:n_lines]).upper()


def classify(text: str) -> DocKind:
    head = " ".join(heading(text).split())
    # The title is the first few lines; later lines may *mention* other agreements.
    title = " ".join(heading(text, n_lines=12).split())
    if "DEBTOR-IN-POSSESSION" in head or "DEBTOR IN POSSESSION" in head or "DIP CREDIT" in title:
        return DocKind.DIP
    if re.search(r"SUPPORT AGREEMENT|BACKSTOP|PURCHASE AGREEMENT|BRIDGE FACILITY|"
                 r"INTERCREDITOR|TRANSACTION AGREEMENT|INDENTURE|INDEMNIFICATION|"
                 r"INVESTOR RIGHTS|SECURITY AGREEMENT|PLEDGE AGREEMENT", title):
        return DocKind.OTHER
    if "RESTATEMENT AGREEMENT" in head:
        return DocKind.RESTATEMENT
    opening = " ".join(text.split()[:3000]).upper()
    if re.search(r"\bAMENDMENT\b", title) and CREDIT_AGREEMENT_RE.search(opening):
        if len(text.split()) >= CONFORMED_COPY_MIN_WORDS:
            return DocKind.AMENDMENT_WITH_CONFORMED_COPY
        return DocKind.AMENDMENT
    if CREDIT_AGREEMENT_RE.search(title):
        return DocKind.CREDIT_AGREEMENT
    return DocKind.OTHER


def covenant_mentions(text: str, patterns: list[str], width: int = 160) -> list[str]:
    """Snippets of an amendment that mention the covenant, so a human can check them."""
    flat = " ".join(text.split())
    out = []
    for pattern in patterns:
        for m in re.finditer(pattern, flat, re.IGNORECASE):
            out.append(flat[max(0, m.start() - width): m.end() + width])
    return out


@dataclass
class AgreementInForce:
    base: ClassifiedDoc
    amendments: list[ClassifiedDoc]  # oldest first, all filed after the base

    @property
    def amendments_touching_covenant(self) -> list[ClassifiedDoc]:
        return [a for a in self.amendments if a.covenant_mentions]


def agreement_in_force(docs: list[ClassifiedDoc]) -> AgreementInForce:
    """Latest full text of the agreement plus every short amendment filed after it.

    The base is the latest new agreement, restatement, or amendment that attaches a conformed
    copy (never a DIP loan). Later short amendments must be read on top of it.
    """
    ordered = sorted(docs, key=lambda d: d.doc.file_date)
    base_kinds = (DocKind.CREDIT_AGREEMENT, DocKind.RESTATEMENT, DocKind.AMENDMENT_WITH_CONFORMED_COPY)
    bases = [d for d in ordered if d.kind in base_kinds]
    if not bases:
        raise LookupError("No credit agreement found among the exhibits")
    base = bases[-1]
    later = [d for d in ordered
             if d.kind is DocKind.AMENDMENT and d.doc.file_date > base.doc.file_date]
    return AgreementInForce(base=base, amendments=later)

"""Split a credit agreement into numbered sections and defined terms, and find covenant text.

These are the agent's *tools*. A local model cannot read 150,000 words at once, so plain code
narrows the agreement to the few passages that matter, and looks up definitions on request.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# "SECTION 6.13." / "Section 7.11" / "7.11 Financial Covenants." at the start of a line.
SECTION_RE = re.compile(
    r"(?m)^[ \t]*(?:SECTION|Section)[ \t]+(\d{1,2}\.\d{1,2}(?:\.\d{1,2})?)\.?"
    r"|^[ \t]*(\d{1,2}\.\d{1,2})\.?[ \t]+(?=[A-Z][A-Za-z,;'’ ]{3,80}\.)"
)
# “Consolidated EBITDA” means ... (curly or straight quotes)
DEFINITION_RE = re.compile(r"[“\"]([A-Z][^”\"]{1,90})[”\"]\s+(?:means|shall mean|has the meaning)")

# Wording that signals a maintenance financial covenant.
COVENANT_SIGNALS = [
    (r"(?:will|shall) not permit[^.]{0,250}?(?:Ratio|EBITDA|Liquidity|Net Worth|Fixed Charge)", 5),
    ((r"(?:Leverage|Coverage|Fixed Charge Coverage) Ratio[^.]{0,120}?(?:to exceed|to be less than|"
     r"greater than|less than|shall not exceed|shall be at least)"), 4),
    (r"\d\.\d{1,2}\s*(?:to|:)\s*1(?:\.0{1,2})?", 3),
    (r"[Ff]inancial [Cc]ovenant", 2),
    (r"[Mm]inimum (?:Liquidity|Availability|EBITDA)", 3),
    (r"Springing|springing|Covenant Trigger|Test Condition", 2),
]


@dataclass
class Section:
    number: str
    text: str

    @property
    def title(self) -> str:
        """Heading after the number, e.g. "Consolidated Gross Total Leverage Ratio"."""
        rest = re.sub(r"^(?:SECTION|Section)?\s*\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\.?\s*", "", self.text)
        return rest.split(".", 1)[0][:100].strip()


def split_sections(text: str) -> dict[str, Section]:
    """Numbered sections. The table of contents repeats every number, so keep the longest copy."""
    matches = list(SECTION_RE.finditer(text))
    sections: dict[str, Section] = {}
    for i, m in enumerate(matches):
        number = m.group(1) or m.group(2)
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = " ".join(text[m.start():end].split())
        if not _looks_like_heading(body):
            continue  # a cross-reference that happens to start a line, e.g. "Section 6.13 (including..."
        if number not in sections or len(body) > len(sections[number].text):
            sections[number] = Section(number, body)
    return sections


def _looks_like_heading(body: str) -> bool:
    """True if the text after the number starts like a section title: "Financial Covenant. ..." """
    rest = re.sub(r"^(?:SECTION|Section)?\s*\d{1,2}\.\d{1,2}(?:\.\d{1,2})?\.?\s*", "", body)
    return bool(re.match(r"[A-Z\[]", rest)) and not re.match(r"(?:or|and|of|to)\b", rest)


def split_definitions(text: str) -> dict[str, str]:
    """Defined terms -> definition text (until the next definition starts)."""
    flat = " ".join(text.split())
    matches = list(DEFINITION_RE.finditer(flat))
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else min(len(flat), m.start() + 6000)
        body = flat[m.start():end]
        term = m.group(1).strip()
        if term not in out or len(body) > len(out[term]):  # amendments may restate a term
            out[term] = body[:12000]
    return out


TITLE_RE = re.compile(r"Financial Covenant|Leverage Ratio|Coverage Ratio|Fixed Charge|"
                      r"Liquidity|Net Worth|Maintenance Covenant|Loan-to-Value|Collateral Coverage", re.IGNORECASE)
# The canonical maintenance-covenant sentence: "... will not permit the X Ratio ... to exceed 4.70 to 1.00"
STRONG_RE = re.compile(
    r"(?:(?:will|shall) not |\b)permit[^.]{0,300}?(?:Ratio|Liquidity|EBITDA)[^.]{0,300}?"
    r"(?:\d\.\d{1,2}\s*(?:to|:)\s*1|\$\s?[\d,]{6,})", re.IGNORECASE)
CROSS_REF_RE = re.compile(
    r"financial covenants?[”\"]?(?: means the covenants?)?(?: set forth| contained| described)? in "
    r"Section (\d{1,2}\.\d{1,2})", re.IGNORECASE)


def covenant_score(section: Section) -> float:
    head = section.text[:4000]
    score = 0.0
    if TITLE_RE.search(section.title):
        score += 20
    if STRONG_RE.search(head):
        score += 10
    score += min(sum(w * len(re.findall(p, head)) for p, w in COVENANT_SIGNALS), 10)
    if len(section.text.split()) > 3000 and not TITLE_RE.search(section.title):
        score /= 3  # long baskets and definitions mention ratios everywhere
    return score


def find_covenant_sections(sections: dict[str, Section], top: int = 3,
                           max_words: int = 1500) -> list[Section]:
    """Sections most likely to contain a financial maintenance covenant, best first.

    Ranks by heading, the canonical covenant sentence, and cross-references from other
    sections ("the financial covenant set forth in Section 6.09"). Long sections are trimmed
    to `max_words` around the strongest signal so the model's context stays small.
    """
    scores = {n: covenant_score(s) for n, s in sections.items()}
    for s in sections.values():
        for ref in CROSS_REF_RE.findall(s.text):
            if ref in scores:
                scores[ref] += 15
    ranked = sorted((n for n in scores if scores[n] >= 15), key=lambda n: -scores[n])
    return [_trim(sections[n], max_words) for n in ranked[:top]]


def _trim(section: Section, max_words: int) -> Section:
    words = section.text.split()
    if len(words) <= max_words:
        return section
    m = re.search(COVENANT_SIGNALS[0][0], section.text) or re.search(COVENANT_SIGNALS[1][0], section.text)
    if not m:
        return Section(section.number, " ".join(words[:max_words]))
    centre = len(section.text[:m.start()].split())
    start = max(0, centre - max_words // 3)
    return Section(section.number, " ".join(words[start:start + max_words]))


def terms_used(text: str, definitions: dict[str, str]) -> list[str]:
    """Defined terms that appear in a passage (longest first, so specific terms win)."""
    return sorted((t for t in definitions if t in text), key=len, reverse=True)

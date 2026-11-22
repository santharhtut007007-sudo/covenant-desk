"""Contract Agent: reads the covenant passages of a credit agreement and extracts the terms.

The loop
--------
1. Tools (plain code) pick the 1-3 sections most likely to hold a maintenance covenant.
2. The model extracts covenants into a strict schema, quoting the sentence for each one.
   It may ask for up to four defined terms it needs ("Revolving Facility Test Condition").
3. A tool returns those definitions and the model answers again.
4. Code verifies every answer: the quote must be in the agreement word for word, the
   threshold number must appear in the quote, and the section must exist.
5. If a check fails, the model gets the exact error once and may correct itself.
   Anything still failing is marked unverified for a human to review, never silently accepted.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from covenant_desk.covenant import quote_in_document
from covenant_desk.llm import LocalLLM
from covenant_desk.sections import (
    Section,
    find_covenant_sections,
    split_definitions,
    split_sections,
    terms_used,
)

Metric = Literal[
    "total_leverage", "net_leverage", "secured_leverage", "first_lien_leverage",
    "interest_coverage", "fixed_charge_coverage", "minimum_liquidity", "other",
]


class ExtractedCovenant(BaseModel):
    name: str = Field(description="Name used in the agreement, e.g. 'Consolidated Total Leverage Ratio'")
    metric: Metric
    direction: Literal["maximum", "minimum"] = Field(
        description="'maximum' if the ratio must stay below the threshold (leverage), "
        "'minimum' if it must stay above (coverage, liquidity)")
    threshold: float = Field(description="The number in the covenant: 4.70 for '4.70 to 1.00', "
                             "250000000 for '$250,000,000'")
    threshold_unit: Literal["ratio", "usd"]
    step_downs: list[str] = Field(default_factory=list,
                                  description="Later thresholds by period, if the covenant changes over time")
    applies_to: str = Field(description="Which lenders or facilities the covenant protects, "
                            "e.g. 'Revolving Facility only', or 'all loans'")
    springing: bool = Field(description="True if tested only when a condition is met "
                            "(e.g. revolver usage above a percentage)")
    springing_condition: str | None = None
    section: str = Field(description="Section number, e.g. '6.13'")
    quote: str = Field(description="Exact sentence from the text containing the threshold, copied word for word")


class ExtractionReply(BaseModel):
    covenants: list[ExtractedCovenant]
    no_maintenance_covenant: bool = Field(
        description="True if the passages contain no financial maintenance covenant "
        "(only incurrence tests, baskets or definitions)")
    need_definitions: list[str] = Field(
        default_factory=list, description="Up to 4 defined terms you need to read to answer accurately")
    reasoning: str = Field(description="One or two sentences on how you decided")


class VerifiedCovenant(BaseModel):
    covenant: ExtractedCovenant
    checks: dict[str, bool]

    @property
    def verified(self) -> bool:
        return all(self.checks.values())


class ExtractionResult(BaseModel):
    candidate_sections: list[str]
    covenants: list[VerifiedCovenant]
    no_maintenance_covenant: bool
    definitions_requested: list[str]
    reasoning: str
    retried: bool
    usage: dict


SYSTEM = """You are a leveraged-finance credit analyst reading a credit agreement.
Extract only FINANCIAL MAINTENANCE COVENANTS: tests the borrower must pass every quarter or
at all times (maximum leverage ratio, minimum interest or fixed charge coverage, minimum
liquidity). Do NOT extract incurrence tests (ratios that only limit taking on new debt,
paying dividends or making investments), pricing grids, or baskets.

Rules:
- Copy each quote exactly from the text, word for word, including the threshold number.
- If the covenant is tested only when a condition is met (for example revolver usage above a
  percentage), set springing=true and describe the condition.
- If the passages contain no maintenance covenant, return an empty list and
  no_maintenance_covenant=true. Many term loans are "covenant-lite"; that is a valid answer.
- If a defined term changes the meaning and its definition is not shown, list it in
  need_definitions (at most 4).
- The quote must be ONE contiguous piece of the text. Prefer the shortest sentence that names
  the ratio and contains the number. If thresholds are in a table, quote the table row.
- Section: just the number in the brackets, e.g. "6.09".

What counts (examples):
- "Holdings will not permit the Consolidated Total Leverage Ratio to exceed 4.50 to 1.00" ->
  maintenance covenant, metric total_leverage, direction maximum, threshold 4.5.
- "permit Liquidity to be less than $2,000,000,000 at the close of any Business Day" ->
  maintenance covenant, metric minimum_liquidity, direction minimum, threshold 2000000000, usd.
- "the Fixed Charge Coverage Ratio [...] to be less than 1.00 to 1.00; provided that [it] will
  only be tested [during] a Covenant Trigger Period" -> springing=true, fixed_charge_coverage, minimum 1.0.
- "at any time that any Revolving Loans are outstanding, permit the ... Leverage Ratio ... to
  exceed 3.50 to 1.00" -> springing=true (tested only when the revolver is drawn).
- "the Borrower may incur Indebtedness if, on a pro forma basis, the Fixed Charge Coverage
  Ratio would be at least 2.00 to 1.00" -> NOT a maintenance covenant (incurrence test). Skip it.
Coverage and liquidity covenants are "minimum"; leverage covenants are "maximum"."""


def _number_variants(x: float) -> list[str]:
    """Ways a threshold can be written: 4.7 -> '4.70', '4.7'; 250000000 -> '250,000,000'."""
    out = {f"{x:.2f}", f"{x:.1f}", f"{x:g}", f"{x:,.0f}", f"{x:.0f}"}
    if x >= 1e6:
        out.add(f"{x / 1e6:g} million")
    return list(out)


# Wording that means a covenant is tested only when a condition holds.
SPRINGING_CUES = re.compile(
    r"only be tested|Trigger Period|Test Condition|springing|"
    r"at any time that any Revolving Loans are outstanding|"
    r"when the requirements of this Section [\d.]+ apply|"
    r"so long as[^.]{0,400}?exceeds\s+\d+(?:\.\d+)?%",
    re.IGNORECASE,
)


def expected_direction(quote: str) -> str | None:
    """'to exceed' / 'greater than' -> maximum; 'less than' / 'at least' -> minimum."""
    q = quote.lower()
    is_max = bool(re.search(r"exceed|greater than", q))
    is_min = bool(re.search(r"less than|at least|not be lower", q))
    if is_max and not is_min:
        return "maximum"
    if is_min and not is_max:
        return "minimum"
    return None  # ambiguous wording: no opinion


def expected_family(quote: str) -> set[str] | None:
    """Metric types the quote's own wording allows, or None if it does not say."""
    q = quote.lower()
    if "coverage ratio" in q or "to consolidated net interest" in q:
        return {"interest_coverage", "fixed_charge_coverage"}
    if re.search(r"leverage ratio|loan-to-value|to consolidated capital|debt to", q):
        return {"total_leverage", "net_leverage", "secured_leverage", "first_lien_leverage", "other"}
    if "liquidity" in q:
        return {"minimum_liquidity"}
    return None


def verify(cov: ExtractedCovenant, document_text: str, sections: dict[str, Section]) -> dict[str, bool]:
    """Code checks. Each one catches a different way a model answer can be wrong."""
    quote_ok = quote_in_document(cov.quote, document_text)
    number_ok = any(v in cov.quote for v in _number_variants(cov.threshold))
    m = re.search(r"\d{1,2}\.\d{1,2}", cov.section)  # "Section 6.09 (a)" -> "6.09"
    section = sections.get(m.group(0)) if m else None
    direction = expected_direction(cov.quote)
    springing_cue = bool(section and SPRINGING_CUES.search(section.text))
    return {
        "quote_in_document": quote_ok,
        "threshold_in_quote": number_ok,
        "section_exists": section is not None,
        "direction_consistent": direction is None or direction == cov.direction,
        "springing_consistent": section is None or springing_cue == cov.springing,
        "metric_consistent": (fam := expected_family(cov.quote)) is None or cov.metric in fam,
    }


def _passages_prompt(passages: list[Section], definitions: dict[str, str]) -> str:
    blocks = [f"[Section {s.number}]\n{s.text}" for s in passages]
    used = []
    for s in passages:
        used.extend(t for t in terms_used(s.text, definitions) if t not in used)
    return ("Credit agreement passages:\n\n" + "\n\n".join(blocks)
            + "\n\nDefined terms used in these passages (definitions available on request): "
            + ", ".join(used[:60]))


def extract_covenants(document_text: str, llm: LocalLLM | None = None,
                      max_passage_words: int = 1500) -> ExtractionResult:
    llm = llm or LocalLLM()
    sections = split_sections(document_text)
    definitions = split_definitions(document_text)
    passages = find_covenant_sections(sections, max_words=max_passage_words)
    if not passages:
        return ExtractionResult(candidate_sections=[], covenants=[], no_maintenance_covenant=True,
                                definitions_requested=[], reasoning="No candidate covenant section found by retrieval.",
                                retried=False, usage=llm.usage())

    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _passages_prompt(passages, definitions)}]
    reply = llm.chat_json(messages, ExtractionReply)

    # Tool step: look up the definitions the model asked for.
    requested = [t for t in reply.need_definitions[:4] if t in definitions]
    if requested:
        defs_text = "\n\n".join(" ".join(definitions[t].split()[:600]) for t in requested)
        messages += [{"role": "assistant", "content": reply.model_dump_json()},
                     {"role": "user", "content": f"Definitions you asked for:\n\n{defs_text}\n\n"
                      "Now give your final answer. Do not request more definitions."}]
        reply = llm.chat_json(messages, ExtractionReply)

    # Verify, and give the model one chance to fix what failed.
    checked = [VerifiedCovenant(covenant=c, checks=verify(c, document_text, sections)) for c in reply.covenants]
    failures = [v for v in checked if not v.verified]
    retried = False
    if failures:
        retried = True
        errors = "\n".join(
            f"- {v.covenant.name}: failed {[k for k, ok in v.checks.items() if not ok]}" for v in failures)
        messages += [{"role": "assistant", "content": reply.model_dump_json()},
                     {"role": "user", "content": "Some answers failed automatic checks:\n" + errors
                      + "\nHow to fix: quote_in_document -> copy the quote exactly (same words, same "
                      "numbers). threshold_in_quote -> the quote must contain the threshold number. "
                      "section_exists -> give the number shown in brackets. direction_consistent -> "
                      "'exceed'/'greater than' means maximum, 'less than'/'at least' means minimum; "
                      "remember the borrower must NOT do what follows 'will not permit'. "
                      "springing_consistent -> re-read whether the test applies only when a condition "
                      "holds. metric_consistent -> choose the metric that matches the ratio's name "
                      "(Coverage Ratio -> coverage, Leverage / Loan-to-Value -> leverage or other, "
                      "Liquidity -> minimum_liquidity). Return the full corrected answer."}]
        reply = llm.chat_json(messages, ExtractionReply)
        checked = [VerifiedCovenant(covenant=c, checks=verify(c, document_text, sections)) for c in reply.covenants]

    return ExtractionResult(
        candidate_sections=[s.number for s in passages],
        covenants=checked,
        no_maintenance_covenant=reply.no_maintenance_covenant and not reply.covenants,
        definitions_requested=requested,
        reasoning=reply.reasoning,
        retried=retried,
        usage=llm.usage(),
    )


def summarize(result: ExtractionResult) -> str:
    lines = [f"Candidate sections: {', '.join(result.candidate_sections) or 'none'}"]
    if result.no_maintenance_covenant:
        lines.append("No maintenance covenant found (covenant-lite?)")
    for v in result.covenants:
        c = v.covenant
        unit = "x" if c.threshold_unit == "ratio" else " USD"
        status = "VERIFIED" if v.verified else f"UNVERIFIED {[k for k, ok in v.checks.items() if not ok]}"
        lines.append(f"- {c.name} ({c.metric}) {c.direction} {c.threshold:g}{unit}, Section {c.section}, "
                     f"applies to: {c.applies_to}; springing={c.springing}"
                     + (f" ({c.springing_condition})" if c.springing_condition else "") + f"  [{status}]")
        lines.append(f"    quote: \"{c.quote[:220]}\"")
    if result.definitions_requested:
        lines.append(f"Definitions looked up: {', '.join(result.definitions_requested)}")
    lines.append(f"Reasoning: {result.reasoning}")
    lines.append(f"Retried after failed checks: {result.retried}; usage: {result.usage}")
    return "\n".join(lines)


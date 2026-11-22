"""Score the Contract Agent against the answer key: `uv run covenant-desk-eval --model <name>`.

Metrics (each with a Wilson 95% confidence interval, because the sample is small):
- covenant-lite call: does the agent correctly say whether a maintenance covenant exists?
- recall: share of answer-key covenants the agent found (matched by section and family)
- precision: share of the agent's covenants that match one in the answer key
- field accuracy on matched covenants: direction, threshold, springing
- verified => correct: when the code verifier passes a covenant, how often is it fully right?
"""

from __future__ import annotations

import argparse
import json
import math
import re

from covenant_desk import config
from covenant_desk.agent import ExtractionResult, VerifiedCovenant, extract_covenants
from covenant_desk.agreements import agreement_in_force
from covenant_desk.documents import load_text
from covenant_desk.edgar import EdgarClient
from covenant_desk.llm import LocalLLM
from covenant_desk.pipeline import classify_exhibits

GOLD_FILE = config.PROJECT_ROOT / "evaluation" / "gold_covenants.json"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def family_of(metric: str, families: dict[str, list[str]]) -> str:
    return next((f for f, metrics in families.items() if metric in metrics), "other")


def section_of(text: str) -> str | None:
    m = re.search(r"\d{1,2}\.\d{1,2}", text)
    return m.group(0) if m else None


def threshold_ok(value: float, accepted: list[float]) -> bool:
    return any(math.isclose(value, a, rel_tol=1e-3) for a in accepted)


def match(pred: VerifiedCovenant, gold: dict, families: dict) -> bool:
    c = pred.covenant
    return (section_of(c.section) == gold["section"]
            and family_of(c.metric, families) in gold["families"])


def score_borrower(result: ExtractionResult, gold: dict, families: dict) -> dict:
    preds = list(result.covenants)
    rows = []
    unmatched_preds = set(range(len(preds)))
    for g in gold["covenants"]:
        hit = next((i for i in unmatched_preds if match(preds[i], g, families)), None)
        if hit is None:
            rows.append({"gold_section": g["section"], "found": False})
            continue
        unmatched_preds.discard(hit)
        c = preds[hit].covenant
        fields = {
            "direction": c.direction == g["direction"],
            "threshold": threshold_ok(c.threshold, g["thresholds"]),
            "springing": c.springing == g["springing"],
        }
        rows.append({"gold_section": g["section"], "found": True, **fields,
                     "all_fields": all(fields.values()), "verified": preds[hit].verified})
    false_positives = [preds[i] for i in unmatched_preds]
    predicted_lite = len(preds) == 0
    return {
        "lite_correct": predicted_lite == gold["covenant_lite"],
        "matches": rows,
        "false_positives": [{"section": p.covenant.section, "name": p.covenant.name,
                             "verified": p.verified} for p in false_positives],
        "n_pred": len(preds),
    }


def aggregate(per_borrower: dict[str, dict]) -> dict:
    def ratio(k, n):
        lo, hi = wilson(k, n)
        return {"k": k, "n": n, "rate": round(k / n, 3) if n else None, "ci95": [round(lo, 3), round(hi, 3)]}

    lite = [b["lite_correct"] for b in per_borrower.values()]
    matches = [m for b in per_borrower.values() for m in b["matches"]]
    found = [m for m in matches if m["found"]]
    n_pred = sum(b["n_pred"] for b in per_borrower.values())
    fps = [fp for b in per_borrower.values() for fp in b["false_positives"]]
    verified_found = [m for m in found if m["verified"]]
    verified_fps = [fp for fp in fps if fp["verified"]]
    verified_total = len(verified_found) + len(verified_fps)
    verified_correct = sum(m["all_fields"] for m in verified_found)
    return {
        "covenant_lite_call": ratio(sum(lite), len(lite)),
        "recall": ratio(len(found), len(matches)),
        "precision": ratio(len(found), n_pred),
        "direction": ratio(sum(m["direction"] for m in found), len(found)),
        "threshold": ratio(sum(m["threshold"] for m in found), len(found)),
        "springing": ratio(sum(m["springing"] for m in found), len(found)),
        "all_fields_correct": ratio(sum(m["all_fields"] for m in found), len(found)),
        "verified_share": ratio(verified_total, n_pred),
        "verified_and_correct": ratio(verified_correct, verified_total),
    }


def run(model: str) -> dict:
    gold = json.loads(GOLD_FILE.read_text())
    client, llm = EdgarClient(), LocalLLM(model=model)
    per_borrower, outputs = {}, {}
    for ticker, g in gold["borrowers"].items():
        inforce = agreement_in_force(classify_exhibits(client, client.cik_for_ticker(ticker)))
        result = extract_covenants(load_text(client.download(inforce.base.doc)), llm)
        per_borrower[ticker] = score_borrower(result, g, gold["families"])
        outputs[ticker] = result.model_dump(exclude={"usage"})
        print(f"{ticker:5s} lite_ok={per_borrower[ticker]['lite_correct']} "
              f"matches={per_borrower[ticker]['matches']} fps={len(per_borrower[ticker]['false_positives'])}",
              flush=True)
    summary = {"model": model, "usage": llm.usage(), "metrics": aggregate(per_borrower),
               "per_borrower": per_borrower}
    out_dir = config.PROJECT_ROOT / "evaluation"
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", model)
    (out_dir / f"results_{safe}.json").write_text(json.dumps(summary, indent=1, default=str))
    (out_dir / f"outputs_{safe}.json").write_text(json.dumps(outputs, indent=1, default=str))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate the Contract Agent against the answer key")
    parser.add_argument("--model", default=LocalLLM().model, help="Ollama model name")
    args = parser.parse_args()
    summary = run(args.model)
    print(f"\nModel: {summary['model']}   usage: {summary['usage']}")
    for name, m in summary["metrics"].items():
        print(f"  {name:22s} {m['k']:>3}/{m['n']:<3} = {m['rate']}   95% CI {m['ci95']}")


if __name__ == "__main__":
    main()

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))
os.environ["LLM_ENABLED"] = "false"

from app.data import load_records
from app.service import run_query


def faithfulness(result: dict, all_ids: set[str], record_by_id: dict) -> tuple[float, int, int]:
    claims = result.get("answer_evidence", [])
    if not claims:
        return (1.0 if "没有找到足够" in result["answer"] else 0.0, 0 if "没有找到足够" in result["answer"] else 1, 1)
    supported = 0
    for claim in claims:
        ids = claim.get("source_ids", [])
        if not ids or not set(ids) <= all_ids:
            continue
        if len(ids) == 1 and (claim["claim"] != record_by_id[ids[0]].title or claim["claim"] not in result["answer"]):
            continue
        if len(ids) == 2 and "price changed" in claim["claim"].lower():
            a, b = (record_by_id[x] for x in ids)
            if a.value is None or b.value is None or not a.value:
                continue
            pct = round((b.value-a.value)/a.value*100, 2)
            try:
                if float(claim["claim"].split()[-1].rstrip("%")) != pct:
                    continue
            except ValueError:
                continue
            if f"{abs(pct)}%" not in result["answer"]:
                continue
        supported += 1
    return supported/max(1, len(claims)), supported, len(claims)


def evaluate() -> dict:
    records = load_records(ROOT / "storage/demo_data.json")
    by_id = {r.id: r for r in records}
    qas = json.loads((ROOT / "backend/eval/ground_truth.json").read_text(encoding="utf-8"))
    recalls, faith, details = [], [], []
    for qa in qas:
        result = run_query(qa["question"], records)
        returned = {s["id"] for s in result["sources"]}
        relevant = set(qa["relevant_ids"])
        recall = len(returned & relevant)/len(relevant) if relevant else 1.0
        score, supported, total = faithfulness(result, set(by_id), by_id)
        recalls.append(recall); faith.append(score)
        details.append({"question": qa["question"], "recall_at_5": round(recall, 3), "faithfulness": round(score, 3), "relevant_returned": sorted(returned & relevant), "trace_id": result["trace_id"]})
    summary = {"questions": len(qas), "recall_at_5": round(sum(recalls)/max(1,len(recalls)), 4), "answer_faithfulness": round(sum(faith)/max(1,len(faith)), 4), "details": details}
    out = ROOT / "backend/eval/results.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


if __name__ == "__main__":
    result = evaluate()
    print(f"Questions: {result['questions']}\nRecall@5: {result['recall_at_5']:.4f}\nAnswer faithfulness: {result['answer_faithfulness']:.4f}\nSaved: backend/eval/results.json")

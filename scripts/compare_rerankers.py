"""Compare reranker models, with and without colloquial variants in the scored passage.

The variants were added to fix a case where every candidate scored identically. That
fix may have been compensating for a model that was wrong for this corpus — this script
tells the two apart instead of guessing.
"""

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cs_assistant.config import Settings  # noqa: E402
from cs_assistant.sources import rerank as rerank_mod  # noqa: E402
from cs_assistant.sources.policy import PolicyIndex  # noqa: E402

MODELS = [
    ("MiniLM", "cross-encoder/ms-marco-MiniLM-L-6-v2"),
    ("BGE-v2-m3", "BAAI/bge-reranker-v2-m3"),
]

CASES = json.loads((Path(__file__).parent.parent / "tests/retrieval_cases.json").read_text())


def passage_without_variants(hit: dict) -> str:
    return f"{hit['title']} > {hit['heading_path']}\n{hit['text']}"


def evaluate(index: PolicyIndex, model_name: str, use_variants: bool) -> dict:
    original = rerank_mod._passage
    if not use_variants:
        rerank_mod._passage = passage_without_variants

    reranker = rerank_mod.Reranker(model_name=model_name)
    hits_at_1 = hits_at_3 = 0
    spreads: list[float] = []
    started = time.monotonic()

    try:
        for case in CASES:
            candidates = index._hybrid(case["query"], locale=case["locale"], limit=20)
            ranked = reranker.rerank(case["query"], candidates, top_k=3)
            files = [h["source_file"] for h in ranked]

            if files[:1] == [case["expect_file"]]:
                hits_at_1 += 1
            if case["expect_file"] in files:
                hits_at_3 += 1

            # A model with no signal for a query returns near-identical scores; that
            # failure is invisible in the ranking alone.
            scores = [h.get("rerank_score", 0.0) for h in ranked]
            spreads.append(max(scores) - min(scores) if scores else 0.0)
    finally:
        rerank_mod._passage = original

    n = len(CASES)
    return {
        "top1": hits_at_1 / n,
        "top3": hits_at_3 / n,
        "spread": sum(spreads) / len(spreads),
        "seconds": time.monotonic() - started,
    }


def main() -> None:
    index = PolicyIndex(Settings.from_env())

    baseline_hits = 0
    for case in CASES:
        hits = index._hybrid(case["query"], locale=case["locale"], limit=20)
        if hits and hits[0]["source_file"] == case["expect_file"]:
            baseline_hits += 1
    print(f"hybrid only              top1={baseline_hits / len(CASES):.0%}\n")

    print(f"{'model':<12} {'variants':<9} {'top1':>6} {'top3':>6} {'spread':>8} {'time':>7}")
    print("-" * 54)
    for label, model_name in MODELS:
        for use_variants in (True, False):
            r = evaluate(index, model_name, use_variants)
            print(
                f"{label:<12} {str(use_variants):<9} {r['top1']:>5.0%} {r['top3']:>6.0%} "
                f"{r['spread']:>8.2f} {r['seconds']:>6.1f}s"
            )


if __name__ == "__main__":
    main()

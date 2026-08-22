"""Cross-encoder reranking.

Hybrid retrieval merges two candidate lists; it does not judge relevance. A chunk about
appeals scores highly for "why was my article marked duplicate" because it shares word
forms with the query, not because it answers it. Bi-encoders cannot tell the difference:
they embed query and document separately, so nothing ever compares them directly.

A cross-encoder reads the pair together and scores how well this passage answers this
question. That is what separates the three policy documents that all mention "rejected
decisions" from the one that defines duplicates.
"""

import logging
from functools import lru_cache

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "BAAI/bge-reranker-v2-m3"


@lru_cache(maxsize=2)
def _load(model_name: str):
    """Load once per process. First call downloads ~80MB."""
    from sentence_transformers import CrossEncoder

    return CrossEncoder(model_name, max_length=512)


def _passage(hit: dict) -> str:
    """What the cross-encoder scores against.

    The heading path matters — "Payout Timing > Holidays" is part of what makes a
    passage answer a question. So do the colloquial variants: publishers ask "where is
    my money" while the document says "settlement is issued on a T+7 basis". Without
    the variants the model sees a register mismatch and rates every policy equally
    irrelevant, which is worse than no reranking at all.
    """
    parts = [f"{hit['title']} > {hit['heading_path']}", hit["text"]]
    if variants := hit.get("query_variants"):
        parts.append("Common phrasings: " + "; ".join(variants))
    return "\n".join(parts)


class Reranker:
    def __init__(self, model_name: str = DEFAULT_MODEL, enabled: bool = True):
        self.model_name = model_name
        self.enabled = enabled

    def rerank(self, query: str, hits: list[dict], top_k: int = 5) -> list[dict]:
        """Score each (query, chunk) pair and keep the best.

        Falls back to retrieval order if the model is unavailable — a slightly worse
        ordering beats failing a support conversation over a ranking refinement.
        """
        if not self.enabled or not hits:
            return hits[:top_k]

        try:
            model = _load(self.model_name)
        except Exception as exc:
            logger.warning("reranker unavailable, using retrieval order: %s", exc)
            return hits[:top_k]

        pairs = [(query, _passage(hit)) for hit in hits]

        try:
            scores = model.predict(pairs)
        except Exception as exc:
            logger.warning("rerank failed, using retrieval order: %s", exc)
            return hits[:top_k]

        for hit, score in zip(hits, scores):
            hit["rerank_score"] = float(score)
            # Keep the retrieval score under its own name so a bad rerank is diagnosable.
            hit["retrieval_score"] = hit.get("score")

        return sorted(hits, key=lambda h: h["rerank_score"], reverse=True)[:top_k]

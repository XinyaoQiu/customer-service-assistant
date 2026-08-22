"""Policy retrieval: markdown source, Weaviate index.

The index is a rebuildable derivative. Changing the embedding model or the chunking
strategy never risks the authoritative content, which stays in the markdown files.
"""

import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import weaviate
import weaviate.classes as wvc
from langchain_google_genai import GoogleGenerativeAIEmbeddings

from ..config import Settings
from .rerank import Reranker


@dataclass
class PolicyChunk:
    policy_id: str
    title: str
    heading_path: str
    text: str
    effective_from: str
    locales: list[str]
    query_variants: list[str]
    source_file: str

    @property
    def embed_text(self) -> str:
        """What gets embedded, and why the heading path is part of it.

        A chunk reading "deferred to the next business day" carries no retrieval signal
        alone. Prefixed with "Payout Timing > Holidays" it does.

        Colloquial variants ride along for retrieval only — publishers say "where's my
        money", the document says "settlement cycle". Generation still uses the
        authoritative text, so answers stay traceable to a published page.
        """
        parts = [f"{self.title} > {self.heading_path}", self.text]
        if self.query_variants:
            parts.append("Also asked as: " + "; ".join(self.query_variants))
        return "\n".join(parts)


def _parse_front_matter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    _, fm, body = raw.split("---", 2)

    meta: dict = {}
    current_list: str | None = None
    for line in fm.strip().splitlines():
        if line.startswith("  - "):
            if current_list:
                meta.setdefault(current_list, []).append(line[4:].strip().strip('"'))
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip(), value.strip()
        if not value:
            current_list = key
            meta[key] = []
        elif value.startswith("[") and value.endswith("]"):
            meta[key] = [v.strip() for v in value[1:-1].split(",") if v.strip()]
            current_list = None
        else:
            meta[key] = value.strip('"')
            current_list = None
    return meta, body


def load_chunks(policy_dir: Path) -> list[PolicyChunk]:
    """Split on heading hierarchy, not character count.

    Policy documents have semantic boundaries built in. Fixed-size splitting cuts a
    single rule in half, and half a rule is worse than no rule.
    """
    chunks: list[PolicyChunk] = []

    for path in sorted(policy_dir.glob("*.md")):
        meta, body = _parse_front_matter(path.read_text())
        sections = re.split(r"^## ", body, flags=re.MULTILINE)

        for section in sections:
            section = section.strip()
            if not section:
                continue
            heading, _, text = section.partition("\n")
            text = text.strip()
            if not text:
                continue

            chunks.append(
                PolicyChunk(
                    policy_id=meta.get("policy_id", path.stem),
                    title=meta.get("title", path.stem),
                    heading_path=heading.strip(),
                    text=text,
                    effective_from=meta.get("effective_from", "1970-01-01"),
                    locales=meta.get("locales", ["en"]),
                    query_variants=meta.get("query_variants", []),
                    source_file=path.name,
                )
            )
    return chunks


class PolicyIndex:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.collection_name = settings.policy_collection
        self._embeddings = GoogleGenerativeAIEmbeddings(
            model=settings.embedding_model,
            google_api_key=settings.google_api_key,
        )
        self.reranker = Reranker(
            model_name=settings.rerank_model, enabled=settings.rerank_enabled
        )

    def _client(self):
        return weaviate.connect_to_local(
            host=self.settings.weaviate_host,
            port=self.settings.weaviate_port,
            grpc_port=self.settings.weaviate_grpc_port,
        )

    def rebuild(self, policy_dir: Path) -> int:
        """Full idempotent rebuild.

        A few dozen chunks rebuild in seconds, which removes a whole class of
        incremental-sync deletion bugs. The collection is dropped and recreated rather
        than diffed.
        """
        chunks = load_chunks(policy_dir)
        client = self._client()
        try:
            if client.collections.exists(self.collection_name):
                client.collections.delete(self.collection_name)

            client.collections.create(
                name=self.collection_name,
                vectorizer_config=wvc.config.Configure.Vectorizer.none(),
                properties=[
                    wvc.config.Property(name="policy_id", data_type=wvc.config.DataType.TEXT),
                    wvc.config.Property(name="title", data_type=wvc.config.DataType.TEXT),
                    wvc.config.Property(name="heading_path", data_type=wvc.config.DataType.TEXT),
                    wvc.config.Property(name="text", data_type=wvc.config.DataType.TEXT),
                    wvc.config.Property(name="effective_from", data_type=wvc.config.DataType.TEXT),
                    wvc.config.Property(name="locales", data_type=wvc.config.DataType.TEXT_ARRAY),
                    wvc.config.Property(
                        name="query_variants", data_type=wvc.config.DataType.TEXT_ARRAY
                    ),
                    wvc.config.Property(name="source_file", data_type=wvc.config.DataType.TEXT),
                ],
            )

            vectors = self._embeddings.embed_documents([c.embed_text for c in chunks])
            collection = client.collections.get(self.collection_name)
            with collection.batch.dynamic() as batch:
                for chunk, vector in zip(chunks, vectors):
                    batch.add_object(
                        properties={
                            "policy_id": chunk.policy_id,
                            "title": chunk.title,
                            "heading_path": chunk.heading_path,
                            "text": chunk.text,
                            "effective_from": chunk.effective_from,
                            "locales": chunk.locales,
                            "query_variants": chunk.query_variants,
                            "source_file": chunk.source_file,
                        },
                        vector=vector,
                    )
            return len(chunks)
        finally:
            client.close()

    def search(
        self, query: str, *, locale: str = "en", limit: int = 5, candidates: int = 20
    ) -> list[dict]:
        """Hybrid retrieval, then cross-encoder rerank.

        Policy IDs and product names are exact tokens that dense retrieval blurs;
        colloquial phrasing is what dense retrieval is for. Both matter, so both run —
        but fusing two candidate lists says nothing about whether a passage answers the
        question. Retrieval casts a wide net (`candidates`); the reranker decides which
        few reach the prompt.

        Effective-date and locale filtering happen in the query, never as a post-filter
        and never as a prompt instruction: an expired policy must not reach the prompt
        at all.
        """
        hits = self._hybrid(query, locale=locale, limit=candidates)
        return self.reranker.rerank(query, hits, top_k=limit)

    def _hybrid(self, query: str, *, locale: str, limit: int) -> list[dict]:
        vector = self._embeddings.embed_query(query)
        today = date.today().isoformat()

        client = self._client()
        try:
            collection = client.collections.get(self.collection_name)
            response = collection.query.hybrid(
                query=query,
                vector=vector,
                alpha=0.5,
                limit=limit,
                filters=(
                    wvc.query.Filter.by_property("effective_from").less_or_equal(today)
                    & wvc.query.Filter.by_property("locales").contains_any([locale])
                ),
                return_metadata=wvc.query.MetadataQuery(score=True),
            )
            return [
                {
                    "policy_id": o.properties["policy_id"],
                    "title": o.properties["title"],
                    "heading_path": o.properties["heading_path"],
                    "text": o.properties["text"],
                    "query_variants": o.properties.get("query_variants", []),
                    "source_file": o.properties["source_file"],
                    "score": o.metadata.score,
                }
                for o in response.objects
            ]
        finally:
            client.close()

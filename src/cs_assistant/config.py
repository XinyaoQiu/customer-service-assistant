"""Settings.

Chat and embedding providers are configured separately because they were separately
chosen in the original system — Claude for conversation, OpenAI for embeddings. Model
strings route through `init_chat_model`, so switching provider is a config change.
"""

import os

from pydantic import BaseModel


class Settings(BaseModel):
    # "provider:model" — anthropic:claude-…, google_genai:gemini-…, openai:gpt-…
    chat_model: str = "google_genai:gemini-flash-latest"
    chat_model_deep: str = "google_genai:gemini-flash-latest"
    embedding_model: str = "models/gemini-embedding-001"

    google_api_key: str | None = None
    anthropic_api_key: str | None = None
    openai_api_key: str | None = None

    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3307
    mysql_user: str = "cs"
    mysql_password: str = "cs"
    mysql_db: str = "publisher"

    weaviate_host: str = "127.0.0.1"
    weaviate_port: int = 8080
    weaviate_grpc_port: int = 50051
    policy_collection: str = "PolicyChunk"

    policy_dir: str = "policies"

    # Retrieval casts wide, the reranker narrows. Hybrid fusion merges candidate lists
    # but cannot tell whether a passage answers the question.
    # Below this, the corpus has nothing on the question. Measured on this corpus:
    # covered questions score 0.72-0.99, uncovered ones score exactly 0.0. Returning
    # weak matches anyway is how an assistant answers confidently from a passage that
    # does not address the question.
    rerank_floor: float = 0.1
    rerank_enabled: bool = True
    # Multilingual. An English-only reranker (ms-marco-MiniLM) scored 0/4 on Spanish
    # phrasings absent from query_variants, landing on unrelated documents every time —
    # for a publisher base with many non-native English speakers that path is simply
    # broken, which outweighs this model being slower.
    rerank_model: str = "BAAI/bge-reranker-v2-m3"
    # Trimmed from 20: the reranker dominates latency, and a publisher is waiting.
    retrieval_candidates: int = 10

    # Two clarifying turns, then a human. A loop of clarifying questions is the most
    # frustrating failure mode in support chat, so the cap is enforced rather than left
    # to the model's judgment.
    max_clarifications: int = 2
    # Turns of history the agent sees. Enough for "the second one" and follow-up
    # questions; bounded so a long session does not grow the prompt without limit.
    record_turns: bool = True
    history_turns: int = 6
    agent_max_rounds: int = 4
    # Measured: retrieval ~0.8s and one model call ~0.9s, but a three-round agent
    # measured at 37s end to end. The bound exists to stop runaway loops, not to force
    # a fast answer — cutting off correct work produces an escalation nobody needed.
    agent_timeout_seconds: float = 90.0

    @property
    def mysql_dsn(self) -> dict:
        return {
            "host": self.mysql_host,
            "port": self.mysql_port,
            "user": self.mysql_user,
            "password": self.mysql_password,
            "database": self.mysql_db,
            "charset": "utf8mb4",
        }

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            chat_model=os.getenv("CHAT_MODEL", "google_genai:gemini-flash-latest"),
            chat_model_deep=os.getenv("CHAT_MODEL_DEEP", "google_genai:gemini-flash-latest"),
            embedding_model=os.getenv("EMBEDDING_MODEL", "models/gemini-embedding-001"),
            google_api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
            anthropic_api_key=os.getenv("ANTHROPIC_API_KEY"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
            mysql_port=int(os.getenv("MYSQL_PORT", "3307")),
            weaviate_host=os.getenv("WEAVIATE_HOST", "127.0.0.1"),
            weaviate_port=int(os.getenv("WEAVIATE_PORT", "8080")),
            policy_dir=os.getenv("POLICY_DIR", "policies"),
            rerank_enabled=os.getenv("RERANK_ENABLED", "1").lower() not in ("0", "false"),
            rerank_model=os.getenv("RERANK_MODEL", "BAAI/bge-reranker-v2-m3"),
        )

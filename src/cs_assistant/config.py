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

    # Two clarifying turns, then a human. A loop of clarifying questions is the most
    # frustrating failure mode in support chat, so the cap is enforced rather than left
    # to the model's judgment.
    max_clarifications: int = 2
    agent_max_rounds: int = 4
    agent_timeout_seconds: float = 15.0

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
        )

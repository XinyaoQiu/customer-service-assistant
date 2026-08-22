# customer-service-assistant

A support assistant for NewsBreak publishers. It answers questions about article review
status, reach, and platform policy — and hands the rest to a person.

Design: [docs/tech-design.md](docs/tech-design.md).

## How it works

```
publisher message
      ↓
route_intent            rules first, model for the tail
      ↓
 ┌────────────┬──────────────┬─────────────┬───────────┐
 status        distribution   policy        high risk
 (MySQL)       (MySQL)        (Weaviate)    (no lookup)
      └────────────┴──────────────┘              ↓
                   ↓                        interrupt →
              compose                       human takes over
        disposition table decides                ↓
        what may be said                    ticket + reply
```

Three ideas run through this:

**Routing is a security boundary, not a convenience.** The intent decides which tools the
turn gets. The policy path holds no database tools at all, so a policy question is
structurally incapable of reading article records regardless of how the conversation is
steered.

**Identity is injected, never model-supplied.** `publisher_id` arrives through the
graph's runtime context. The tool executor strips whatever the model puts in an injected
argument and substitutes the trusted value — verified, not assumed.

**What may be said is code.** The disposition table decides whether a rejection reason can
be disclosed. Anti-abuse findings, monetization holds, and account restrictions always go
to a human: any specific account of *why* content was flagged is useful to whoever is
evading detection, and the person asking may be that person.

## Setup

```bash
docker compose up -d          # MySQL and Weaviate
uv sync

export GOOGLE_API_KEY=...     # or ANTHROPIC_API_KEY / OPENAI_API_KEY, see below
uv run cs-assistant init-db   # schema + sample publishers and articles
uv run cs-assistant index     # build the policy index
```

| Variable | Purpose |
|---|---|
| `GOOGLE_API_KEY` | Chat and embeddings |
| `CHAT_MODEL` | `provider:model`, e.g. `anthropic:claude-...`, `google_genai:gemini-...` |
| `EMBEDDING_MODEL` | Defaults to `models/gemini-embedding-001` |
| `RERANK_MODEL` | Defaults to `BAAI/bge-reranker-v2-m3` |
| `MYSQL_HOST` / `MYSQL_PORT` | Defaults to `127.0.0.1:3307` |
| `WEAVIATE_HOST` / `WEAVIATE_PORT` | Defaults to `127.0.0.1:8080` |

Chat and embedding providers are configured separately because they were separately
chosen: Claude for conversation, OpenAI for embeddings. Model strings route through
`init_chat_model`, so switching provider is a config change.

## Usage

```bash
uv run cs-assistant chat --publisher pub_001
uv run cs-assistant ask --publisher pub_002 "why was my recipe article rejected?"
uv run cs-assistant search "where is my money"       # inspect retrieval directly
```

Sample accounts: `pub_001` (English, mixed statuses), `pub_002` (Spanish, includes an
anti-abuse rejection that must escalate), `pub_003` (no articles).

## Retrieval

```
markdown policy  →  chunk on headings  →  generate colloquial phrasings  →  embed
                                                                             ↓
query  →  hybrid (BM25 + vector, date and locale filtered)  →  rerank  →  top 5
```

**Documents carry no retrieval metadata.** A help-centre page has a title and a body.
Colloquial phrasings — "where is my money" against a document that says "settlement" —
are generated during indexing and stored as index properties. Writing them into the
markdown made the corpus fit the evaluation set, which is how retrieval appeared to score
92% before anything was reranked.

**Reranking is not optional here.** Hybrid fusion merges two candidate lists; it does not
judge whether a passage answers the question. `appeals.md` outranked `originality.md` for
"why was my article marked duplicate" purely on shared vocabulary.

**The reranker is multilingual by necessity.** An English-only cross-encoder
(`ms-marco-MiniLM`) scored 0/4 on Spanish phrasings absent from the generated variants,
landing on unrelated documents every time. `bge-reranker-v2-m3` scored 3/4.

## Tests

```bash
uv run pytest
```

Disposition tests always run. Retrieval tests need Weaviate and an API key and skip
without them.

## Known limitations

**Latency is dominated by model queueing, not by this code.** The same three-round agent
was measured at 14.8s, 38.0s, 76.6s and 125.5s across four identical runs — same six
messages each time. Retrieval is ~0.8s and a single model call ~0.9s; the variance is
server-side queueing on a free-tier key. The 90s per-path budget exists to bound runaway
loops, not to make replies fast.

**No conversational memory across intents.** A message covering two topics ("why wasn't
my article published, and when do I get paid") routes to one path and answers half.

**Sample data throughout.** The publisher database and policy corpus are fixtures, not a
production dataset.

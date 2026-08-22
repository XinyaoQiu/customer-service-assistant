# customer-service-assistant

A support assistant for NewsBreak publishers. It answers questions about article review
status, reach, and platform policy — and hands the rest to a person.

Design: [docs/tech-design.md](docs/tech-design.md).

## How it works

```
publisher message
      ↓
triage        rules: does this need a person to act?      ─── yes ──┐
      ↓ no    (and prefetch their articles meanwhile)               │
   agent      one agent, five tools                                 │
      ↓       it may also ask for a human ──────────────────────────┤
    reply                                                    escalate
                                                    interrupt → ticket
```

**A review outcome is a code and a message, like an HTTP status.** The database stores
`duplicate_content`; the backend owns the sentence "This article closely matches content
already published on the platform." The assistant reads the code, looks up the message,
and says it. Someone whose work was rejected is owed an explanation — withholding it only
makes them wait for a human to repeat it.

**What it cannot say, it was never given.** Thresholds live in the review service's
configuration, not in the database and not in this system. "You exceeded the daily upload
limit" is answerable; "the limit is 40" is not, because the number is nowhere in the
assistant's reach. That is a stronger guarantee than instructing a model to stay quiet,
which a determined publisher can talk around.

**Identity is injected, never model-supplied.** `publisher_id` arrives through the
graph's runtime context. The tool executor strips whatever the model puts in an injected
argument and substitutes the trusted value — verified against the installed library, not
assumed.

**Rules decide what needs a person; the model decides everything else.** Account
restrictions, payment holds, and a direct request for a human skip the agent, because
those need someone to *act* and no explanation substitutes. Which tools to call, how many
times, and how to word the answer are the model's.

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

## Latency

Turns stream progress as they run:

```
you > why has my article not been published yet?
  Reading your message…
  Checking your articles…

You currently have a few recent articles that have not been published yet: …
```

Three things cut the work rather than hide it:

**Routing and the article lookup run together.** Both database paths open with the same
query and it costs nothing, so it overlaps the classification call instead of following
it. The result is handed to the agent, which saves it an entire opening round — the
single largest win, since a round is a model round-trip.

**Retrieval overlaps its two network calls**, embedding and connection setup: 0.8s → 0.38s.

**Agents are built once**, at graph construction. Constructing one binds tool schemas to
the model, and repeating that per turn added latency to every reply.

### Known limitation

**What remains is model queueing, not this code.** Before these changes, the same
three-round agent measured 14.8s, 38.0s, 76.6s and 125.5s across four identical runs —
same six messages each time. A single model call is ~0.9s and retrieval ~0.38s; the
variance is server-side queueing on a free-tier key. Expect 5-15s on a paid endpoint.
The 90s per-path budget bounds runaway loops; it is not a latency target.

**No conversational memory across intents.** A message covering two topics ("why wasn't
my article published, and when do I get paid") routes to one path and answers half.

**Sample data throughout.** The publisher database and policy corpus are fixtures, not a
production dataset.

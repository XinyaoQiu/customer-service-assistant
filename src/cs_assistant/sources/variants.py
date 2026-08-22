"""Generating colloquial phrasings at index time.

Publishers ask "where is my money"; the document says "settlement is issued on a T+7
basis". Nothing in those two sentences overlaps lexically, and they are far enough apart
that embeddings alone often miss.

These phrasings are index metadata, not content. A help-centre page carries a title and
a body, never a list of ways people might ask about it, so the markdown stays clean and
this runs during indexing.

In production the better source is the support queue — what publishers actually typed,
clustered by which policy resolved the ticket. Deriving them from the policy text is the
cold-start substitute for a system that has no traffic yet.
"""

import json
import logging

logger = logging.getLogger(__name__)

_PROMPT = """A publisher writes to support. Below is one section of a help-centre policy.

List the ways a publisher would actually phrase a question that this section answers.

- Use the words publishers use, not the words the policy uses. If the policy says
  "settlement", they say "my money" or "getting paid".
- Include informal and frustrated phrasings, since people contact support when
  something has gone wrong.
- Write half in English and half in Spanish.
- Only phrasings this section genuinely answers. A phrasing that fits a different
  policy makes retrieval worse, not better.
- 6 to 10 items.

Policy: {title}
Section: {heading}

{text}"""

_SCHEMA = {
    "type": "object",
    "properties": {"phrasings": {"type": "array", "items": {"type": "string"}}},
    "required": ["phrasings"],
}


def generate(chat_model, title: str, heading: str, text: str) -> list[str]:
    """Derive phrasings for one chunk. Returns [] on failure.

    Indexing must not fail because variant generation did — retrieval still works
    without them, just less well on colloquial queries.
    """
    prompt = _PROMPT.format(title=title, heading=heading, text=text)
    try:
        response = chat_model.with_structured_output(_SCHEMA).invoke(prompt)
    except Exception as exc:
        logger.warning("variant generation failed for %s > %s: %s", title, heading, exc)
        return []

    phrasings = response.get("phrasings", []) if isinstance(response, dict) else []
    # Cap the length: a paragraph-long "phrasing" is a summary, and summaries dilute
    # the signal the short colloquial forms provide.
    return [p.strip() for p in phrasings if p and len(p) < 120][:10]

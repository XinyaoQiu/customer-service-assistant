"""Conversation state and the trusted context.

The split matters. State is what the conversation accumulates and what the model can
influence; context is what the server knows about who is asking. `publisher_id` lives in
context, so no prompt injection and no tool-call forgery can move it — the tool executor
strips model-supplied values for injected arguments and substitutes the trusted ones.
"""

from dataclasses import dataclass
from operator import add
from typing import Annotated, Any

from typing_extensions import TypedDict

from .routing import Confidence, Intent


@dataclass
class PublisherContext:
    """Server-side facts about the requester. Never derived from message content."""

    publisher_id: str
    locale: str = "en"
    tier: str = "standard"
    display_name: str = ""


class ConversationState(TypedDict, total=False):
    """What the conversation carries between turns.

    Kept flat and JSON-serializable so a checkpointer can persist it, and so a stored
    conversation can be read months later without the code that produced it.
    """

    messages: Annotated[list[dict], add]

    intent: str
    intent_confidence: str
    routing_reason: str

    # The articles just offered for disambiguation. "The second one" resolves against
    # this list, so the model picks an index rather than inventing an article id.
    pending_articles: list[dict]
    subject_article_id: str | None

    # Enforced in code: a loop of clarifying questions is the most frustrating failure
    # mode in support chat, so the count is state, not a judgment call.
    clarification_count: int

    findings: list[str]
    policy_hits: list[dict]

    escalated: bool
    escalation_ticket: str | None
    escalation_reason: str | None

    reply: str
    turn: int


def initial_state(message: str) -> ConversationState:
    return {
        "messages": [{"role": "user", "content": message}],
        "clarification_count": 0,
        "turn": 1,
        "escalated": False,
    }


def last_user_message(state: ConversationState) -> str:
    for message in reversed(state.get("messages", [])):
        if message.get("role") == "user":
            return message.get("content", "")
    return ""


def needs_escalation(state: ConversationState) -> bool:
    """Whether this conversation has run out of automated options."""
    return (
        state.get("intent") == Intent.HIGH_RISK.value
        or state.get("intent_confidence") == Confidence.LOW.value
        or state.get("escalated", False)
    )


def as_transcript(state: ConversationState, limit: int = 20) -> str:
    """Conversation history for a human picking up the handoff."""
    lines = []
    for message in state.get("messages", [])[-limit:]:
        role = "Publisher" if message.get("role") == "user" else "Assistant"
        lines.append(f"{role}: {message.get('content', '')}")
    return "\n".join(lines)


def summarize_articles(articles: list[dict]) -> list[dict[str, Any]]:
    """Trim article rows to what a reply needs.

    Narrower than the query returns, so a field added to the table later does not
    silently start appearing in prompts.
    """
    return [
        {
            "article_id": a["article_id"],
            "title": a["title"],
            "status": a.get("status"),
            "reason_code": a.get("reason_code"),
            "submitted_at": str(a.get("submitted_at", "")),
        }
        for a in articles
    ]

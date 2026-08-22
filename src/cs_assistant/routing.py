"""Intent routing.

The classification decides which tools the conversation gets, so it is the security
boundary as much as a convenience: the policy path is handed no database tools at all,
which makes a policy question structurally incapable of reading article records.

Routing is by confidence, not just by label. The error costs are asymmetric — reading a
policy question as a refund request is far worse than the reverse — so the tuned
parameter is the threshold, not the accuracy.
"""

import re
from enum import Enum

from pydantic import BaseModel


class Intent(str, Enum):
    STATUS = "status"
    DISTRIBUTION = "distribution"
    POLICY = "policy"
    HIGH_RISK = "high_risk"
    UNKNOWN = "unknown"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Routing(BaseModel):
    intent: Intent
    confidence: Confidence
    decided_by: str
    reasoning: str = ""


# Anything touching money, account standing, or abuse enforcement routes to high risk on
# weak signal. That threshold is deliberately trigger-happy: the cost of escalating a
# routine question is one extra human touch, and the cost of the reverse is an assistant
# discussing an enforcement action it must not discuss.
_HIGH_RISK_PATTERNS = [
    r"\b(banned|suspend\w*|terminat\w*|disabl\w*)\b.{0,30}\b(account|profile)\b",
    r"\baccount\b.{0,30}\b(banned|suspend\w*|restricted|penal\w*)\b",
    r"\b(spam|bot|fake|fraud|cheat\w*|abus\w*)\b",
    r"\b(payment|payout|earnings?|revenue|monetiz\w*)\b.{0,40}\b(hold|frozen|blocked|withheld|missing)\b",
    r"\b(demonetiz\w*|strike|violation)\b",
    r"\bcuenta\b.{0,30}\b(suspendida|bloqueada|cerrada)\b",
    r"\bpago\b.{0,30}\b(retenido|bloqueado|congelado)\b",
]

_STATUS_PATTERNS = [
    r"\b(article|post|story|draft)\b.{0,40}\b(publish\w*|pending|review\w*|reject\w*|approv\w*)\b",
    r"\bwhy\b.{0,30}\b(isn.t|not|hasn.t)\b.{0,20}\b(publish\w*|live|up)\b",
    r"\b(status|still waiting|under review)\b",
    r"\b(art[íi]culo|publicaci[óo]n)\b.{0,40}\b(revisi[óo]n|rechaz\w*|publicad\w*)\b",
]

_DISTRIBUTION_PATTERNS = [
    r"\b(views?|impressions?|reach|traffic|readers?)\b.{0,30}\b(low|drop\w*|down|fell|no)\b",
    r"\b(no|few|fewer)\b.{0,20}\b(views?|impressions?|readers?)\b",
    r"\bwhy\b.{0,40}\b(views?|impressions?|reach)\b",
    r"\b(vistas|alcance|lectores)\b.{0,30}\b(bajaron|pocas?|menos)\b",
]

_POLICY_PATTERNS = [
    r"\bwhat (counts|qualifies) as\b",
    r"\b(policy|policies|rule|guidelines?|requirements?)\b",
    r"\bhow (do|can) i\b.{0,30}\b(appeal|republish|license|cite)\b",
    r"\bam i allowed\b",
    r"\b(pol[íi]tica|reglas?|requisitos?)\b",
]


def _matches(text: str, patterns: list[str]) -> bool:
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def classify_by_rules(message: str) -> Routing | None:
    """Keyword layer: free, exact, covers head traffic.

    High risk is checked first and on its own. A message can look like a status
    question and still be about an enforcement action, and in that overlap the
    conservative reading has to win.
    """
    if _matches(message, _HIGH_RISK_PATTERNS):
        return Routing(
            intent=Intent.HIGH_RISK,
            confidence=Confidence.HIGH,
            decided_by="rules",
            reasoning="matched an enforcement, abuse, or payment-hold pattern",
        )

    hits = [
        (Intent.STATUS, _matches(message, _STATUS_PATTERNS)),
        (Intent.DISTRIBUTION, _matches(message, _DISTRIBUTION_PATTERNS)),
        (Intent.POLICY, _matches(message, _POLICY_PATTERNS)),
    ]
    matched = [intent for intent, hit in hits if hit]

    # Two intents matching is not a decision. Fall through to the model rather than
    # picking the first one.
    if len(matched) == 1:
        return Routing(
            intent=matched[0],
            confidence=Confidence.HIGH,
            decided_by="rules",
            reasoning="matched exactly one intent pattern",
        )
    return None


_SCHEMA = {
    "type": "object",
    "properties": {
        "intent": {
            "type": "string",
            "enum": [i.value for i in Intent],
        },
        "confidence": {"type": "string", "enum": [c.value for c in Confidence]},
        "reasoning": {"type": "string"},
    },
    "required": ["intent", "confidence", "reasoning"],
}

_SYSTEM = """Classify what a publisher is asking support about.

- status: where their article is in the review process — pending, rejected, published,
  scheduled — or why it has not gone live.
- distribution: how much reach an article got. Views, impressions, readers.
- policy: what the rules are. Originality, licensing, payout timing, how to appeal.
  General questions, not about one specific article of theirs.
- high_risk: account standing, enforcement, abuse or spam findings, withheld payments.
  Anything where the answer would touch why an account was actioned.
- unknown: not clearly any of these.

Route to high_risk on weak signal. Wrongly escalating a routine question costs one
human reply; wrongly answering an enforcement question is an incident.

Set confidence low when the message could plausibly be two of these. Low confidence is
handled by asking one clarifying question, which is cheap and often correct."""


def classify(message: str, chat_model=None) -> Routing:
    """Rules first, model for the tail.

    The model layer only runs when the rules abstain, which keeps the common case free
    and makes the expensive path the exception.
    """
    if routing := classify_by_rules(message):
        return routing

    if chat_model is None:
        return Routing(
            intent=Intent.UNKNOWN,
            confidence=Confidence.LOW,
            decided_by="rules",
            reasoning="no rule matched and no model available",
        )

    try:
        result = chat_model.with_structured_output(_SCHEMA).invoke(
            [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": message}]
        )
    except Exception as exc:
        # Classification failure must not answer the question anyway. Unknown routes to
        # a clarifying question or a human.
        return Routing(
            intent=Intent.UNKNOWN,
            confidence=Confidence.LOW,
            decided_by="llm",
            reasoning=f"classification failed: {exc}",
        )

    return Routing(
        intent=Intent(result.get("intent", "unknown")),
        confidence=Confidence(result.get("confidence", "low")),
        decided_by="llm",
        reasoning=result.get("reasoning", ""),
    )

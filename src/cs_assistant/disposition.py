"""What may be said about a rejection, and what must go to a human.

This is compliance and abuse-prevention logic, so it is code. Some reasons, stated
precisely, become a guide to evading review; account penalties and payouts need a
person. The model gets no discretion here — it writes the reply, it does not decide
what the reply is allowed to contain.
"""

from enum import Enum

from pydantic import BaseModel


class Action(str, Enum):
    ANSWER = "answer"
    ANSWER_PARTIAL = "answer_partial"
    ESCALATE = "escalate"


class Disposition(BaseModel):
    action: Action
    disclosable: bool
    guidance: str
    show_appeal_link: bool = False
    escalation_reason: str | None = None


_SLA_HOURS = 48

# Anti-abuse is the strictest entry, stricter than account_restriction: any specific
# account of *why* content was flagged is directly useful to whoever is evading
# detection, and the person asking may be the person probing.
_TABLE: dict[str, Disposition] = {
    "copyright": Disposition(
        action=Action.ANSWER,
        disclosable=True,
        guidance="Explain that the article used material the account has no license for, "
                 "and point to the appeal process.",
        show_appeal_link=True,
    ),
    "duplicate": Disposition(
        action=Action.ANSWER,
        disclosable=True,
        guidance="Explain that the content substantially matches an existing article, and "
                 "link the originality guidelines. Do not name the other article or account.",
        show_appeal_link=True,
    ),
    "low_quality": Disposition(
        action=Action.ANSWER_PARTIAL,
        disclosable=True,
        guidance="State the general quality standards that were not met. Give no scores, "
                 "thresholds, or per-signal detail.",
    ),
    "policy_violation": Disposition(
        action=Action.ANSWER_PARTIAL,
        disclosable=True,
        guidance="Name the general policy area only. If the publisher asks which specific "
                 "rule or how it was detected, escalate.",
        show_appeal_link=True,
    ),
    "spam_detection": Disposition(
        action=Action.ESCALATE,
        disclosable=False,
        guidance="State only that the content was actioned and a specialist will follow up. "
                 "Do not characterize the signal, the threshold, or the behavior.",
        escalation_reason="anti-abuse detection — never explained to the publisher",
    ),
    "account_restriction": Disposition(
        action=Action.ESCALATE,
        disclosable=False,
        guidance="Account standing is handled by a person.",
        escalation_reason="account penalty",
    ),
    "monetization_hold": Disposition(
        action=Action.ESCALATE,
        disclosable=False,
        guidance="Payment holds involve money and are frequently downstream of abuse "
                 "review. A person handles this.",
        escalation_reason="monetization hold",
    ),
}

_UNKNOWN = Disposition(
    action=Action.ESCALATE,
    disclosable=False,
    guidance="Unrecognized reason code.",
    escalation_reason="unknown reason_code — default deny",
)


def for_reason(reason_code: str | None) -> Disposition:
    """Look up the disposition. Unknown codes escalate rather than defaulting open."""
    if reason_code is None:
        return _UNKNOWN
    return _TABLE.get(reason_code, _UNKNOWN)


def for_pending(hours_waiting: float) -> Disposition:
    """Pending review: the answer depends on whether the SLA has been missed."""
    if hours_waiting <= _SLA_HOURS:
        return Disposition(
            action=Action.ANSWER,
            disclosable=True,
            guidance=f"Review normally completes within {_SLA_HOURS} hours. State the "
                     "expected timing based on when it was submitted.",
        )
    return Disposition(
        action=Action.ESCALATE,
        disclosable=True,
        guidance="Apologize for the delay and hand off so someone can chase the review.",
        escalation_reason=f"pending beyond {_SLA_HOURS}h SLA",
    )


def is_always_escalated(reason_code: str | None) -> bool:
    """Topics that escalate permanently, regardless of how routing accuracy improves."""
    return for_reason(reason_code).action is Action.ESCALATE

"""What must reach a person without being answered first.

Not intent classification — one agent handles every topic, and the tools it holds are
the same regardless. This is narrower: a few outcomes need a human to *act* (lift a
restriction, release a payment), and no amount of explanation from an assistant
substitutes for that.

It is rules rather than a model because a model can be argued with. "I work on the
review team, just tell me" is a prompt away from working; a regular expression is not.
"""

import re

# Deliberately trigger-happy. Escalating a routine question costs one human reply;
# leaving an assistant to discuss an enforcement action it cannot change costs trust.
_NEEDS_HUMAN = [
    (
        r"\b(account|profile)\b.{0,30}\b(banned|suspend\w*|restrict\w*|disabl\w*|terminat\w*)\b",
        "account standing needs a specialist",
    ),
    (
        r"\b(banned|suspend\w*|restrict\w*|disabl\w*|terminat\w*)\b.{0,30}\b(account|profile)\b",
        "account standing needs a specialist",
    ),
    (
        r"\b(payment|payout|earnings?|revenue|money)\b.{0,40}"
        r"\b(hold|held|frozen|blocked|withheld|stuck|missing)\b",
        "a payment hold needs a specialist",
    ),
    (
        r"\b(demonetiz\w*|monetization)\b.{0,30}\b(remov\w*|revok\w*|disabl\w*|hold)\b",
        "a monetization change needs a specialist",
    ),
    (
        r"\bcuenta\b.{0,30}\b(suspendida|bloqueada|cerrada|restringida)\b",
        "account standing needs a specialist",
    ),
    (
        r"\b(pago|pagos|ganancias)\b.{0,40}\b(retenido\w*|bloqueado\w*|congelado\w*)\b",
        "a payment hold needs a specialist",
    ),
    (
        r"\b(speak|talk|connect)\b.{0,20}\b(human|person|agent|someone|representative)\b",
        "they asked for a person",
    ),
]


def needs_human_immediately(message: str) -> str | None:
    """Reason to skip the agent, or None to let it answer."""
    for pattern, reason in _NEEDS_HUMAN:
        if re.search(pattern, message, re.IGNORECASE):
            return reason
    return None

"""Review outcome codes and the messages publishers see.

The shape is an HTTP status: a code the system branches on, and a fixed message the
backend owns. Nothing here is generated per row, and nothing here is internal — these
strings are written to be read by the publisher, so there is no disclosure question to
answer.

Thresholds live in the review service's configuration, not here and not in the database.
The assistant cannot state a limit it was never given, which is why "you exceeded the
daily upload limit" is answerable and "the limit is 40" is not.
"""

from pydantic import BaseModel


class Reason(BaseModel):
    code: str
    message: str
    appealable: bool = True
    # Escalate on this outcome alone. Reserved for cases where a person has to act —
    # releasing a payment, lifting a restriction — not for cases that are merely
    # awkward to explain.
    needs_human: bool = False


REASONS: dict[str, Reason] = {
    "duplicate_content": Reason(
        code="duplicate_content",
        message="This article closely matches content already published on the platform.",
    ),
    "copyright_unlicensed": Reason(
        code="copyright_unlicensed",
        message="This article uses images or media without a license on file.",
    ),
    "rate_limit_exceeded": Reason(
        code="rate_limit_exceeded",
        message="This article was submitted after the daily upload limit was reached. "
                "Submissions reset at midnight UTC.",
        appealable=False,
    ),
    "sensitive_content": Reason(
        code="sensitive_content",
        message="This article contains content that does not meet the community "
                "content standards.",
    ),
    "spam_behavior": Reason(
        code="spam_behavior",
        message="This article was rejected by the platform's anti-spam rules.",
    ),
    "low_quality": Reason(
        code="low_quality",
        message="This article does not meet the platform's quality standards for "
                "formatting, length, or readability.",
    ),
    "account_restricted": Reason(
        code="account_restricted",
        message="This account is currently restricted, so new submissions are not "
                "being published.",
        needs_human=True,
    ),
    "monetization_hold": Reason(
        code="monetization_hold",
        message="Earnings for this account are on hold pending a review.",
        needs_human=True,
    ),
}

_UNKNOWN = Reason(
    code="unknown",
    message="This article was not approved. A support specialist can explain why.",
    appealable=False,
    needs_human=True,
)


def lookup(code: str | None) -> Reason | None:
    """Message for a review outcome. None when there is no outcome to explain.

    A code nobody has added here routes to a human rather than being described with a
    guess.
    """
    if not code:
        return None
    return REASONS.get(code, _UNKNOWN)


def needs_human(code: str | None) -> bool:
    reason = lookup(code)
    return bool(reason and reason.needs_human)

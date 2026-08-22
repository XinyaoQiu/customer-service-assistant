"""The assistant's tools.

`publisher_id` is never a tool parameter. It arrives through `ToolRuntime`, which the
tool executor fills from the trusted context after stripping whatever the model tried to
put there — verified, not assumed. Cross-tenant access is unavailable rather than
forbidden.

One agent holds all of these. Splitting them across per-intent agents added no isolation
that `create_agent` does not already provide, and cost the ability to answer a message
that spans two topics.
"""

from . import reasons

from langchain_core.tools import tool
from langgraph.prebuilt import ToolRuntime

from .config import Settings
from .sources.business_db import BusinessDB
from .sources.policy import PolicyIndex

_settings = Settings.from_env()
_db = BusinessDB(_settings.mysql_dsn)
_policy: PolicyIndex | None = None


def _policy_index() -> PolicyIndex:
    """Built lazily: the reranker loads a model, and the status path never needs it."""
    global _policy
    if _policy is None:
        _policy = PolicyIndex(_settings)
    return _policy


def warm_up() -> None:
    """Load anything with a slow first use, before a conversation starts."""
    _policy_index().reranker.warm()


@tool
def find_recent_articles(runtime: ToolRuntime, limit: int = 5) -> list[dict]:
    """List this publisher's recent articles with their review status.

    Use this first when someone asks about an article without saying which one.
    Returns every status, not only rejections — an article sitting in review is the
    most common reason someone writes in.
    """
    articles = _db.find_recent_articles(runtime.context.publisher_id, limit=limit)
    return [_render_article(a) for a in articles]


def _render_article(row: dict) -> dict:
    """Shape one article for the agent.

    The review outcome arrives as a code and leaves as the message publishers are meant
    to read — the same split as an HTTP status and its reason phrase.
    """
    reason = reasons.lookup(row.get("reason_code"))
    rendered = {
        "article_id": row["article_id"],
        "title": row["title"],
        "status": row["status"],
        "submitted_at": str(row["submitted_at"]),
    }
    if reason:
        rendered["reason"] = reason.message
        rendered["reason_code"] = reason.code
        rendered["appealable"] = reason.appealable
    return rendered


@tool
def get_article_status(article_id: str, runtime: ToolRuntime) -> dict:
    """Review status for one article the publisher owns."""
    article = _db.get_article(runtime.context.publisher_id, article_id)
    if not article:
        return {"error": "No such article on this account."}

    rendered = _render_article(article)
    if article["reviewed_at"]:
        rendered["reviewed_at"] = str(article["reviewed_at"])
    return rendered


@tool
def get_article_reach(article_id: str, runtime: ToolRuntime) -> dict:
    """Impressions and clicks for one article, against this account's own average.

    Reports what the article did. It cannot report why the ranking system placed it
    that way — that is not retrievable, and describing it would amount to a guide for
    gaming distribution.
    """
    stats = _db.get_article_stats(runtime.context.publisher_id, article_id)
    if not stats:
        return {"error": "No reach data for that article on this account."}

    baseline = stats.get("baseline") or {}
    avg_impressions = float(baseline.get("avg_impressions") or 0)
    return {
        "article_id": stats["article_id"],
        "title": stats["title"],
        "impressions": stats["impressions"],
        "clicks": stats["clicks"],
        "account_average_impressions": round(avg_impressions),
        "vs_account_average": (
            f"{stats['impressions'] / avg_impressions:.0%}" if avg_impressions else "n/a"
        ),
        "measured_at": str(stats["measured_at"]),
        "note": "Ranking signals are not available and must not be inferred.",
    }


@tool
def search_policy(query: str, runtime: ToolRuntime) -> list[dict]:
    """Search published help-centre policy.

    Returns passages with their source page. Answer from these rather than from memory,
    and cite the page so the publisher can read it themselves.
    """
    hits = _policy_index().search(query, locale=runtime.context.locale, limit=4)
    return [
        {
            "title": h["title"],
            "section": h["heading_path"],
            "text": h["text"],
            "source": h["source_file"],
            "relevance": round(h.get("rerank_score", 0.0), 3),
        }
        for h in hits
    ]


@tool
def request_escalation(summary: str, runtime: ToolRuntime) -> str:
    """Hand the conversation to a human when you cannot resolve it.

    Use this when the answer needs an action you cannot take, when the publisher asks
    for detail you were not given, or when they ask for a person. Say what you already
    told them in the summary so they do not have to repeat themselves.
    """
    return f"ESCALATION_REQUESTED: {summary}"


ALL_TOOLS = [
    find_recent_articles,
    get_article_status,
    get_article_reach,
    search_policy,
    request_escalation,
]

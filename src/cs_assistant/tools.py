"""Tools, grouped by which path may use them.

`publisher_id` is never a tool parameter. It arrives through `ToolRuntime`, which the
tool executor fills from the trusted context after stripping whatever the model tried to
put there. Cross-tenant access is therefore not forbidden — it is unavailable.

The groupings are the second half of the same idea: the policy path is handed no
database tools, so a policy question cannot read article records however the
conversation is steered.
"""

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
    return [
        {
            "article_id": a["article_id"],
            "title": a["title"],
            "status": a["status"],
            "reason_code": a["reason_code"],
            "submitted_at": str(a["submitted_at"]),
            "appealable": bool(a["appealable"]),
        }
        for a in articles
    ]


@tool
def get_article_status(article_id: str, runtime: ToolRuntime) -> dict:
    """Review status for one article the publisher owns.

    Returns the reason code, never the reviewer's internal note.
    """
    article = _db.get_article(runtime.context.publisher_id, article_id)
    if not article:
        return {"error": "No such article on this account."}
    return {
        "article_id": article["article_id"],
        "title": article["title"],
        "status": article["status"],
        "reason_code": article["reason_code"],
        "submitted_at": str(article["submitted_at"]),
        "reviewed_at": str(article["reviewed_at"]) if article["reviewed_at"] else None,
        "appealable": bool(article["appealable"]),
    }


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


STATUS_TOOLS = [find_recent_articles, get_article_status, search_policy]
DISTRIBUTION_TOOLS = [find_recent_articles, get_article_reach, search_policy]
POLICY_TOOLS = [search_policy]
# High risk gets none: that path escalates without looking anything up.

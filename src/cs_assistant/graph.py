"""The conversation graph.

One agent with one tool set. Code decides two things around it: what goes straight to a
human without being answered, and when the conversation has run out of automated
options. Everything between — which tools to call, how many times, what to say — is the
model's.

An earlier version split this into per-intent agents. That added no isolation beyond
what `create_agent` already gives (an agent cannot call a tool it was not handed) and
cost the ability to answer a message spanning two topics.
"""

import concurrent.futures
import logging

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.config import get_config, get_stream_writer
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from . import reasons, tools
from .config import Settings
from .routing import needs_human_immediately
from .sources.business_db import BusinessDB
from .state import (
    ConversationState,
    PublisherContext,
    as_transcript,
    last_user_message,
    summarize_articles,
)

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are a support assistant for NewsBreak publishers.

You can look up their articles and review outcomes, their reach numbers, and published
help-centre policy. Use the tools rather than answering from memory.

When an article was rejected, the tools give you the reason as a sentence written for
publishers. Tell them that reason plainly — someone whose work was rejected is owed an
explanation, and withholding it only makes them wait for a human to say the same thing.

What you do not have is the detail behind the reason: thresholds, scores, which specific
article something matched, how a signal was computed. Say you do not have those details
and offer to connect them with someone who can look further.

Never explain why the ranking system gave an article the reach it did. Report
impressions and clicks and how they compare to the account's own average; you cannot
explain the ranker, and a plausible-sounding explanation of it becomes a guide for
gaming distribution.

Call request_escalation when you cannot resolve something, when they ask for a person,
or when the answer needs an action you cannot take.

Be brief. Publishers write to support because something went wrong, not to read."""


def build_graph(settings: Settings | None = None, checkpointer=None, warm: bool = True):
    settings = settings or Settings.from_env()
    db = BusinessDB(settings.mysql_dsn)

    if warm:
        # Pay the reranker's load cost at startup, not inside someone's first turn.
        tools.warm_up()

    model = init_chat_model(settings.chat_model_deep, api_key=settings.google_api_key)
    # Built once: constructing an agent binds tool schemas to the model, and repeating
    # that per turn adds latency to every reply.
    agent = create_agent(model=model, tools=tools.ALL_TOOLS, system_prompt=SYSTEM_PROMPT)

    def _emit(message: str) -> None:
        """Report progress to whoever is streaming. A no-op when nobody is."""
        try:
            if writer := get_stream_writer():
                writer({"progress": message})
        except Exception:
            pass

    def _text_of(content) -> str:
        """Pull the prose out of a model response.

        Content arrives as blocks, and the blocks carry reasoning signatures — long
        base64 that would otherwise be shown verbatim to a publisher.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p).strip()
        return str(content)

    def triage(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        """Decide whether this goes to the agent at all, and prefetch while deciding.

        Account restrictions and payment holds need a person to act, so they skip the
        agent — a rule match rather than a model judgment, because a model can be talked
        out of a judgment.
        """
        message = last_user_message(state)
        _emit("Reading your message…")

        if reason := needs_human_immediately(message):
            return {"escalation_reason": reason}

        # The lookup is local and nearly free, and almost every article question starts
        # with it. Running it here means the agent opens with the data in hand instead
        # of spending a round fetching it.
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                articles = pool.submit(
                    db.find_recent_articles, runtime.context.publisher_id, 5
                ).result(timeout=3)
        except Exception as exc:
            log.warning("prefetch failed: %s", exc)
            articles = []

        return {"pending_articles": summarize_articles(articles)} if articles else {}

    def assist(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        """Run the agent.

        Wall-clock is enforced with a thread: a `timeout` in the graph config is
        accepted and then ignored — measured, a 2s node ran to completion under
        `timeout=0.2` — so relying on it would leave a publisher waiting unbounded.
        """
        _emit("Looking that up…")

        prompt = last_user_message(state)
        if prefetched := state.get("pending_articles"):
            listing = "\n".join(
                f"- {a['article_id']}: {a['title']} ({a.get('status')})"
                for a in prefetched
            )
            prompt = (
                f"{prompt}\n\n[Their five most recent articles, already retrieved:\n"
                f"{listing}\nCall find_recent_articles again only if you need more.]"
            )

        # The agent sees the conversation, not just the latest line. "Why was the second
        # one rejected?" is unanswerable without the turn that listed them, and support
        # conversations are made of exactly those references.
        history = [
            {"role": m["role"], "content": m["content"]}
            for m in state.get("messages", [])[-settings.history_turns * 2 :]
            if m.get("content")
        ]
        conversation = history[:-1] + [{"role": "user", "content": prompt}]

        # Whatever the agent establishes lands here as it goes, so a timeout can hand
        # back partial work instead of discarding it. A publisher who gets half an
        # answer plus a handoff is better off than one who gets only the handoff.
        progress: dict = {}

        def run():
            result = agent.invoke(
                {"messages": conversation},
                config={"recursion_limit": settings.agent_max_rounds * 2},
                context=runtime.context,
            )
            progress["result"] = result
            return result

        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = pool.submit(run)
        try:
            result = future.result(timeout=settings.agent_timeout_seconds)
        except concurrent.futures.TimeoutError:
            log.warning("agent exceeded %.0fs", settings.agent_timeout_seconds)
            # Returning is the point of the budget. Python cannot interrupt a thread,
            # so it is left to finish and discarded — waiting for it here is what turned
            # a 90s budget into a 225s reply.
            pool.shutdown(wait=False)
            return {"escalation_reason": "the lookup took too long"}
        except Exception as exc:
            log.warning("agent failed: %s", exc)
            pool.shutdown(wait=False)
            partial = _partial_reply(progress.get("result"))
            update = {"escalation_reason": "the lookup failed"}
            if partial:
                update["reply"] = partial
            return update
        finally:
            pool.shutdown(wait=False)

        update: dict = {}
        for message in result.get("messages", []):
            content = str(getattr(message, "content", ""))
            if getattr(message, "type", None) == "tool" and content.startswith(
                "ESCALATION_REQUESTED:"
            ):
                update["escalation_reason"] = content.split(":", 1)[1].strip()

        reply = ""
        for message in reversed(result.get("messages", [])):
            if getattr(message, "type", None) == "ai" and message.content:
                reply = _text_of(message.content)
                break

        # An outcome needing a person overrides what the agent chose to say: it can
        # explain the reason, but it cannot lift a restriction or release a hold.
        for article in state.get("pending_articles", []):
            if reasons.needs_human(article.get("reason_code")):
                update["escalation_reason"] = "this needs a specialist to act on"
                update["subject_article_id"] = article["article_id"]

        if reply:
            update["reply"] = reply
            if "escalation_reason" not in update:
                update["messages"] = [{"role": "assistant", "content": reply}]
        return update

    def _partial_reply(result) -> str:
        """The last thing the agent said, if it said anything before failing."""
        if not result:
            return ""
        for message in reversed(result.get("messages", [])):
            if getattr(message, "type", None) == "ai" and message.content:
                return _text_of(message.content)
        return ""

    def escalate(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        """Hand off to a human.

        Everything before the interrupt is read-only. Resuming re-runs a node from its
        start, so a ticket created above the interrupt would be created twice.
        """
        reason = state.get("escalation_reason") or "could not be resolved automatically"
        article_id = state.get("subject_article_id")
        thread = get_config().get("configurable", {}).get("thread_id", "")

        _emit("Passing this to a specialist…")
        interrupt({
            "escalation": {
                "publisher_id": runtime.context.publisher_id,
                "reason": reason,
                "article_id": article_id,
                "message": last_user_message(state),
            }
        })

        # Deterministic across replays of the same turn, distinct across conversations.
        # Without the thread id, every first turn shares a key and the second publisher
        # to escalate silently receives the first one's ticket.
        key = f"{thread}:{state.get('turn', 1)}:{reason}"
        ticket = db.create_escalation(
            publisher_id=runtime.context.publisher_id,
            publisher_message=last_user_message(state),
            idempotency_key=key,
            article_id=article_id,
            transcript=as_transcript(state),
        )

        # Whatever the agent established is still worth saying: being escalated and
        # having to start over is the frustrating part.
        prefix = f"{state['reply']}\n\n" if state.get("reply") else ""
        reply = (
            f"{prefix}I've passed this to a specialist who can take it further. "
            f"Your reference is {ticket}."
        )
        return {
            "escalated": True,
            "escalation_ticket": ticket,
            "reply": reply,
            "messages": [{"role": "assistant", "content": reply}],
        }

    def after(state: ConversationState) -> str:
        return "escalate" if state.get("escalation_reason") else END

    graph = StateGraph(ConversationState, context_schema=PublisherContext)
    graph.add_node("triage", triage)
    graph.add_node("assist", assist)
    graph.add_node("escalate", escalate)

    graph.add_edge(START, "triage")
    graph.add_conditional_edges(
        "triage", lambda s: "escalate" if s.get("escalation_reason") else "assist"
    )
    graph.add_conditional_edges("assist", after)
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())

"""The conversation graph.

Routing decides which tools a turn gets, agents decide how to use them, and code decides
what may be said. Those three are deliberately different mechanisms: the first is a
security boundary, the second wants a model's judgment, and the third is compliance and
cannot be delegated to something a conversation can talk around.
"""

import concurrent.futures
import logging

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from . import disposition, tools
from .config import Settings
from .routing import Confidence, Intent, classify
from .sources.business_db import BusinessDB
from .state import (
    ConversationState,
    PublisherContext,
    as_transcript,
    last_user_message,
    summarize_articles,
)

log = logging.getLogger(__name__)

_AGENT_SYSTEM = {
    Intent.STATUS: """You help publishers understand where their article stands.

Start by finding their recent articles unless they named one. If several could match,
list them and ask which — that is far better than asking them to describe it again.

Report the status and the reason code you find. Never speculate about a reason you did
not retrieve. If the tools return nothing, say so plainly.""",
    Intent.DISTRIBUTION: """You help publishers understand how an article performed.

Report the numbers: impressions, clicks, how they compare to the account's own average.

Do not explain why the ranking system placed an article where it did. You do not have
that information, and a plausible-sounding explanation of ranking is the artifact that
gets screenshotted and treated as documentation for gaming distribution. General
practices from the help centre are fine; per-article ranking narratives are not.""",
    Intent.POLICY: """You answer questions about published platform policy.

Search the help centre and answer from what comes back, citing the page. If the
retrieved passages do not cover the question, say that rather than filling the gap from
memory — a confident wrong answer about payouts or rules is worse than a handoff.""",
}


def build_graph(settings: Settings | None = None, checkpointer=None, warm: bool = True):
    settings = settings or Settings.from_env()
    db = BusinessDB(settings.mysql_dsn)

    if warm:
        # Pay the reranker's load cost at startup rather than inside someone's first turn.
        tools.warm_up()

    def model(deep: bool = False):
        name = settings.chat_model_deep if deep else settings.chat_model
        return init_chat_model(name, api_key=settings.google_api_key)

    def route(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        message = last_user_message(state)
        routing = classify(message, chat_model=model())
        return {
            "intent": routing.intent.value,
            "intent_confidence": routing.confidence.value,
            "routing_reason": routing.reasoning,
        }

    def _run_agent(state: ConversationState, runtime: Runtime[PublisherContext],
                   intent: Intent, toolset: list) -> dict:
        """Run one path's agent inside its own tool set.

        Two budgets, because they bound different things. `recursion_limit` caps tool
        rounds. Wall-clock is enforced here with a thread and a timeout: a `timeout` in
        the graph config is accepted and then ignored — measured, a 2s node ran to
        completion under `timeout=0.2` — so relying on it would leave a publisher
        waiting with no bound at all.
        """
        agent = create_agent(
            model=model(deep=True),
            tools=toolset,
            system_prompt=_AGENT_SYSTEM[intent],
        )

        def run():
            return agent.invoke(
                {"messages": [{"role": "user", "content": last_user_message(state)}]},
                config={"recursion_limit": settings.agent_max_rounds * 2},
                context=runtime.context,
            )

        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
                result = pool.submit(run).result(timeout=settings.agent_timeout_seconds)
        except concurrent.futures.TimeoutError:
            log.warning("%s agent exceeded %.0fs", intent.value, settings.agent_timeout_seconds)
            return {
                "escalated": False,
                "escalation_reason": f"{intent.value} lookup timed out",
                "reply": (
                    "Sorry — that took longer than expected on my side. "
                    "Let me pass this to someone who can look into it."
                ),
            }
        except Exception as exc:
            log.warning("%s agent failed: %s", intent.value, exc)
            return {"escalated": False, "escalation_reason": f"{intent.value} lookup failed"}

        findings, articles, policy_hits = [], [], []
        for message in result.get("messages", []):
            if getattr(message, "type", None) == "tool":
                findings.append(str(message.content)[:1500])
            for call in getattr(message, "tool_calls", []) or []:
                if call["name"] == "find_recent_articles":
                    articles = _articles_from(result)

        answer = ""
        for message in reversed(result.get("messages", [])):
            if getattr(message, "type", None) == "ai" and message.content:
                answer = _text_of(message.content)
                break

        update: dict = {"findings": findings, "reply": answer}
        if articles:
            update["pending_articles"] = summarize_articles(articles)
        if policy_hits:
            update["policy_hits"] = policy_hits
        return update

    def _text_of(content) -> str:
        """Pull the prose out of a model response.

        Content arrives as blocks rather than a string, and the blocks carry reasoning
        signatures — long base64 that would be shown verbatim to a publisher.
        """
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts = [
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            ]
            return "\n".join(p for p in parts if p).strip()
        return str(content)

    def _articles_from(result) -> list[dict]:
        import json

        for message in result.get("messages", []):
            if getattr(message, "type", None) == "tool" and message.name == "find_recent_articles":
                try:
                    payload = json.loads(message.content)
                    return payload if isinstance(payload, list) else []
                except (json.JSONDecodeError, TypeError):
                    return []
        return []

    def status_agent(state, runtime):
        return _run_agent(state, runtime, Intent.STATUS, tools.STATUS_TOOLS)

    def distribution_agent(state, runtime):
        return _run_agent(state, runtime, Intent.DISTRIBUTION, tools.DISTRIBUTION_TOOLS)

    def policy_agent(state, runtime):
        return _run_agent(state, runtime, Intent.POLICY, tools.POLICY_TOOLS)

    def clarify(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        """Ask one question. The cap is enforced by the router, not here."""
        count = state.get("clarification_count", 0) + 1
        prompt = (
            "A publisher wrote in and it is unclear what they need. Ask one short "
            "clarifying question. Do not guess at an answer.\n\n"
            f"Their message: {last_user_message(state)}"
        )
        try:
            question = model().invoke(prompt).content
        except Exception:
            question = (
                "Could you tell me a bit more about what you need help with — "
                "an article, your reach, or a policy question?"
            )
        return {
            "clarification_count": count,
            "reply": question,
            "messages": [{"role": "assistant", "content": question}],
        }

    def escalate(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        """Hand off to a human.

        Everything before the interrupt is read-only. Resuming re-runs a node from its
        start, so a ticket created up here would be created twice — the payload is built
        before, and the write happens strictly after.
        """
        reason = state.get("escalation_reason") or _reason_for(state)
        article_id = state.get("subject_article_id")
        payload = {
            "publisher_id": runtime.context.publisher_id,
            "reason": reason,
            "article_id": article_id,
            "message": last_user_message(state),
        }

        decision = interrupt({"escalation": payload})

        # Deterministic across replays, so the second attempt finds the first ticket
        # instead of opening another.
        key = f"{runtime.context.publisher_id}:{state.get('turn', 1)}:{reason}"
        ticket = db.create_escalation(
            publisher_id=runtime.context.publisher_id,
            publisher_message=last_user_message(state),
            idempotency_key=key,
            article_id=article_id,
            reason_code=state.get("intent"),
            transcript=as_transcript(state),
        )

        note = (decision or {}).get("note") if isinstance(decision, dict) else None
        reply = (
            "I've passed this to a specialist who can look at it properly. "
            f"Your reference is {ticket}."
        )
        return {
            "escalated": True,
            "escalation_ticket": ticket,
            "escalation_reason": reason,
            "reply": note or reply,
            "messages": [{"role": "assistant", "content": note or reply}],
        }

    def _reason_for(state: ConversationState) -> str:
        if state.get("intent") == Intent.HIGH_RISK.value:
            return "high-risk topic — account standing, enforcement, or payment"
        if state.get("intent_confidence") == Confidence.LOW.value:
            return "intent unclear after clarification"
        return "assistant could not resolve the question"

    def compose(state: ConversationState, runtime: Runtime[PublisherContext]) -> dict:
        """Apply the disposition table to whatever the agent produced.

        The agent wrote a reply from what it retrieved; this decides whether that reply
        is allowed to go out. A rejection reason the table marks as non-disclosable
        turns into an escalation here regardless of how the agent phrased it.
        """
        reply = state.get("reply", "")
        for article in state.get("pending_articles", []):
            code = article.get("reason_code")
            # A null reason_code is the normal case for anything not rejected — pending,
            # published, scheduled. Only a rejection carries a code, so only a rejection
            # gets looked up. Treating "no code" as an unknown code would escalate every
            # ordinary status question.
            if article.get("status") != "rejected" or not code:
                continue
            if disposition.is_always_escalated(code):
                d = disposition.for_reason(code)
                return {
                    "escalation_reason": d.escalation_reason,
                    "subject_article_id": article["article_id"],
                    "reply": "",
                }
        return {"messages": [{"role": "assistant", "content": reply}]} if reply else {}

    def after_route(state: ConversationState) -> str:
        intent = state.get("intent")
        confidence = state.get("intent_confidence")

        if intent == Intent.HIGH_RISK.value:
            return "escalate"
        if confidence == Confidence.LOW.value or intent == Intent.UNKNOWN.value:
            # Two clarifying turns, then a person. Enforced here so no prompt can talk
            # its way into a third.
            if state.get("clarification_count", 0) >= settings.max_clarifications:
                return "escalate"
            return "clarify"
        return {
            Intent.STATUS.value: "status_agent",
            Intent.DISTRIBUTION.value: "distribution_agent",
            Intent.POLICY.value: "policy_agent",
        }.get(intent, "clarify")

    def after_compose(state: ConversationState) -> str:
        return "escalate" if state.get("escalation_reason") and not state.get("escalated") else END

    graph = StateGraph(ConversationState, context_schema=PublisherContext)
    graph.add_node("route", route)
    graph.add_node("status_agent", status_agent)
    graph.add_node("distribution_agent", distribution_agent)
    graph.add_node("policy_agent", policy_agent)
    graph.add_node("clarify", clarify)
    graph.add_node("escalate", escalate)
    graph.add_node("compose", compose)

    graph.add_edge(START, "route")
    graph.add_conditional_edges("route", after_route)
    for node in ("status_agent", "distribution_agent", "policy_agent"):
        graph.add_edge(node, "compose")
    graph.add_conditional_edges("compose", after_compose)
    graph.add_edge("clarify", END)
    graph.add_edge("escalate", END)

    return graph.compile(checkpointer=checkpointer or InMemorySaver())

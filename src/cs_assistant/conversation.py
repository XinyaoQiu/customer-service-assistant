"""Driving the graph across turns.

Callers should not have to know that an escalation pauses the graph. This wraps the
interrupt handshake so a turn either returns a reply or reports that a human now owns
the conversation.
"""

from collections.abc import Iterator
from dataclasses import dataclass, field

from langgraph.types import Command

from .config import Settings
from .graph import build_graph
from .state import PublisherContext, initial_state


@dataclass
class Turn:
    reply: str
    intent: str = ""
    escalated: bool = False
    ticket: str | None = None
    escalation_reason: str | None = None
    state: dict = field(default_factory=dict)


class Conversation:
    """One publisher's session. State lives in the checkpointer, keyed by thread_id."""

    def __init__(self, publisher_id: str, *, locale: str = "en", thread_id: str | None = None,
                 settings: Settings | None = None, graph=None):
        self.settings = settings or Settings.from_env()
        self.graph = graph or build_graph(self.settings)
        self.context = PublisherContext(publisher_id=publisher_id, locale=locale)
        self.thread_id = thread_id or f"{publisher_id}-session"
        self._started = False

    @property
    def _config(self) -> dict:
        return {"configurable": {"thread_id": self.thread_id}}

    def send(self, message: str) -> Turn:
        """One turn. Resolves an escalation pause before returning."""
        payload = (
            initial_state(message)
            if not self._started
            else {"messages": [{"role": "user", "content": message}]}
        )
        self._started = True

        result = self.graph.invoke(payload, config=self._config, context=self.context)

        # An escalation pauses at the interrupt so a human can take over. Nothing here
        # decides on their behalf — resuming with no note lets the handoff complete and
        # the ticket be created.
        if "__interrupt__" in result:
            result = self.graph.invoke(
                Command(resume={"note": None}), config=self._config, context=self.context
            )

        return Turn(
            reply=result.get("reply", ""),
            intent=result.get("intent", ""),
            escalated=bool(result.get("escalated")),
            ticket=result.get("escalation_ticket"),
            escalation_reason=result.get("escalation_reason"),
            state=result,
        )

    def stream(self, message: str) -> Iterator[tuple[str, str]]:
        """Yield ("progress"|"reply", text) as the turn proceeds.

        Total time is unchanged; what changes is that the publisher sees the assistant
        working rather than a still cursor. During a slow turn that is the difference
        between waiting and wondering whether it broke.
        """
        payload = (
            initial_state(message)
            if not self._started
            else {"messages": [{"role": "user", "content": message}]}
        )
        self._started = True

        interrupted = False
        seen: set[str] = set()

        def progress(text: str):
            """Suppress repeats.

            Resuming an interrupt re-runs the node from its start, so every progress
            line before the interrupt is emitted twice. Harmless for the ticket, which
            is idempotent, but the publisher would see the message duplicated.
            """
            if text not in seen:
                seen.add(text)
                return True
            return False

        for mode, chunk in self.graph.stream(
            payload, config=self._config, context=self.context,
            stream_mode=["custom", "updates"],
        ):
            if mode == "custom" and "progress" in chunk:
                if progress(chunk["progress"]):
                    yield "progress", chunk["progress"]
            elif mode == "updates":
                interrupted = interrupted or any(
                    node == "__interrupt__" for node in chunk
                )

        # The escalation pause happens mid-stream; resuming completes the handoff.
        state = self.graph.get_state(self._config)
        if interrupted or (state and state.next):
            for mode, chunk in self.graph.stream(
                Command(resume={"note": None}), config=self._config,
                context=self.context, stream_mode=["custom", "updates"],
            ):
                if mode == "custom" and "progress" in chunk:
                    if progress(chunk["progress"]):
                        yield "progress", chunk["progress"]
            state = self.graph.get_state(self._config)

        reply = (state.values.get("reply") if state else "") or ""
        yield "reply", reply

    def history(self) -> list[dict]:
        snapshot = self.graph.get_state(self._config)
        return snapshot.values.get("messages", []) if snapshot else []

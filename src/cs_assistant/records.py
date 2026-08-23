"""Conversation records.

One row per turn: what was asked, what tools ran, what was answered, how long it took.
Separate from the conversation state, which exists to serve the next turn and is
discarded when the session ends.

The near-term use is not metrics — with no traffic there is nothing to aggregate. It is
replay: when a reply is wrong, being able to read back which tools ran and what they
returned beats asking the publisher to reproduce it.
"""

import json
import logging
from datetime import datetime

from .sources.business_db import BusinessDB

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS turns (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    created_at      DATETIME NOT NULL,
    thread_id       VARCHAR(128) NOT NULL,
    publisher_id    VARCHAR(32) NOT NULL,
    turn            INT NOT NULL,
    message         TEXT NOT NULL,
    reply           TEXT,
    tools_used      TEXT,
    escalated       BOOLEAN NOT NULL DEFAULT FALSE,
    escalation_reason VARCHAR(255),
    ticket_id       VARCHAR(64),
    duration_ms     INT,
    -- Filled in by a person later. NULL means nobody judged this turn, which is not
    -- the same as it being right — an accuracy figure that improves when nobody checks
    -- is not an accuracy figure.
    verdict         VARCHAR(16),
    verdict_note    TEXT,
    INDEX idx_thread (thread_id, turn),
    INDEX idx_time (created_at)
)
"""


class TurnRecorder:
    def __init__(self, db: BusinessDB, enabled: bool = True):
        self.db = db
        self.enabled = enabled

    def ensure_schema(self) -> None:
        if not self.enabled:
            return
        try:
            with self.db._connect() as conn, conn.cursor() as cur:
                cur.execute(SCHEMA)
        except Exception as exc:
            log.warning("turn recording unavailable: %s", exc)
            self.enabled = False

    def record(
        self,
        *,
        thread_id: str,
        publisher_id: str,
        turn: int,
        message: str,
        reply: str,
        tools_used: list[str] | None = None,
        escalated: bool = False,
        escalation_reason: str | None = None,
        ticket_id: str | None = None,
        duration_ms: int | None = None,
    ) -> int | None:
        """Store one turn. Failures are logged and swallowed.

        A conversation must not break because its telemetry did.
        """
        if not self.enabled:
            return None
        try:
            with self.db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO turns (
                        created_at, thread_id, publisher_id, turn, message, reply,
                        tools_used, escalated, escalation_reason, ticket_id, duration_ms
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    """,
                    (
                        datetime.now(), thread_id, publisher_id, turn, message, reply,
                        json.dumps(tools_used or []), escalated, escalation_reason,
                        ticket_id, duration_ms,
                    ),
                )
                return cur.lastrowid
        except Exception as exc:
            log.warning("failed to record turn: %s", exc)
            return None

    def replay(self, thread_id: str) -> list[dict]:
        """Every turn of one conversation, for working out what went wrong."""
        try:
            with self.db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "SELECT * FROM turns WHERE thread_id = %s ORDER BY turn", (thread_id,)
                )
                return cur.fetchall()
        except Exception as exc:
            log.warning("replay failed: %s", exc)
            return []

    def recent(self, limit: int = 20) -> list[dict]:
        try:
            with self.db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT id, created_at, thread_id, publisher_id, turn,
                           LEFT(message, 60) AS message, escalated, duration_ms, verdict
                      FROM turns ORDER BY created_at DESC LIMIT %s
                    """,
                    (limit,),
                )
                return cur.fetchall()
        except Exception as exc:
            log.warning("query failed: %s", exc)
            return []

    def set_verdict(self, turn_id: int, verdict: str, note: str | None = None) -> bool:
        try:
            with self.db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    "UPDATE turns SET verdict = %s, verdict_note = %s WHERE id = %s",
                    (verdict, note, turn_id),
                )
            return True
        except Exception as exc:
            log.warning("failed to set verdict: %s", exc)
            return False

    def stats(self, days: int = 30) -> dict:
        """Aggregates over recorded turns.

        Unreviewed turns are counted as unreviewed, never as correct.
        """
        try:
            with self.db._connect() as conn, conn.cursor() as cur:
                cur.execute(
                    """
                    SELECT COUNT(*)                                    AS turns,
                           COUNT(DISTINCT thread_id)                   AS conversations,
                           SUM(escalated)                              AS escalated,
                           SUM(verdict IS NOT NULL)                    AS reviewed,
                           SUM(verdict = 'wrong')                      AS wrong,
                           ROUND(AVG(duration_ms))                     AS avg_ms
                      FROM turns
                     WHERE created_at > NOW() - INTERVAL %s DAY
                    """,
                    (days,),
                )
                return cur.fetchone() or {}
        except Exception as exc:
            log.warning("stats failed: %s", exc)
            return {}

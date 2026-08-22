"""MySQL access.

Two rules hold here rather than in a prompt:

`reason_detail` is never selected by the publisher-facing queries. Not selected, not
returned, not available to be leaked — a prompt instruction is not a security boundary,
and both publisher messages and retrieved content are injection vectors.

`publisher_id` always arrives as an argument from the request context. No query takes it
from anything the model produced, which makes cross-tenant reads structurally impossible
rather than merely forbidden.
"""

import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

import pymysql
from pymysql.cursors import DictCursor

SCHEMA = Path(__file__).parent / "schema.sql"
SEED = Path(__file__).parent / "seed.sql"


def _split_statements(sql: str) -> list[str]:
    """Split on statement boundaries, ignoring semicolons inside string literals.

    Reviewer notes are prose and contain punctuation; splitting naively on ";" cuts an
    INSERT in half and produces a syntax error that reads like bad data.
    """
    statements, current, in_string = [], [], False
    for char in sql:
        if char == "'":
            in_string = not in_string
        if char == ";" and not in_string:
            if stmt := "".join(current).strip():
                statements.append(stmt)
            current = []
        else:
            current.append(char)
    if stmt := "".join(current).strip():
        statements.append(stmt)
    return [s for s in statements if not s.startswith("--")]


class BusinessDB:
    def __init__(self, dsn: dict):
        self.dsn = dsn

    @contextmanager
    def _connect(self):
        conn = pymysql.connect(**self.dsn, cursorclass=DictCursor, autocommit=False)
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def init_schema(self, seed: bool = False) -> None:
        statements = _split_statements(SCHEMA.read_text())
        if seed:
            statements += _split_statements(SEED.read_text())
        with self._connect() as conn, conn.cursor() as cur:
            for stmt in statements:
                cur.execute(stmt)

    def find_recent_articles(self, publisher_id: str, limit: int = 5) -> list[dict]:
        """Recent articles regardless of status.

        Status is a returned field, not part of the tool's identity. A
        `find_recent_rejected_articles` would return nothing for someone whose article
        is sitting in `pending_review` — and "you have no rejected articles" is a wrong
        answer to "why hasn't my article published?". Pending is the common case.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.article_id, a.title, a.submitted_at, a.published_at, a.category,
                       r.status, r.reason_code, r.reviewed_at, r.appealable
                  FROM articles a
                  LEFT JOIN article_reviews r ON r.article_id = a.article_id
                 WHERE a.publisher_id = %s
                 ORDER BY a.submitted_at DESC
                 LIMIT %s
                """,
                (publisher_id, limit),
            )
            return cur.fetchall()

    def get_article(self, publisher_id: str, article_id: str) -> dict | None:
        """One article, scoped to its owner so another account's id returns nothing."""
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT a.article_id, a.title, a.submitted_at, a.published_at, a.category,
                       r.status, r.reason_code, r.reviewed_at, r.appealable
                  FROM articles a
                  LEFT JOIN article_reviews r ON r.article_id = a.article_id
                 WHERE a.publisher_id = %s AND a.article_id = %s
                """,
                (publisher_id, article_id),
            )
            return cur.fetchone()

    def get_article_stats(self, publisher_id: str, article_id: str) -> dict | None:
        """Reach figures for one article, with the account's own baseline for comparison.

        Returns impressions and clicks — never ranking signals or their weights, which
        would amount to a manual for gaming distribution.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                """
                SELECT s.article_id, s.impressions, s.clicks, s.measured_at, a.title
                  FROM article_stats s
                  JOIN articles a ON a.article_id = s.article_id
                 WHERE a.publisher_id = %s AND s.article_id = %s
                """,
                (publisher_id, article_id),
            )
            row = cur.fetchone()
            if not row:
                return None

            cur.execute(
                """
                SELECT AVG(s.impressions) AS avg_impressions,
                       AVG(s.clicks)      AS avg_clicks,
                       COUNT(*)           AS n
                  FROM article_stats s
                  JOIN articles a ON a.article_id = s.article_id
                 WHERE a.publisher_id = %s
                """,
                (publisher_id,),
            )
            row["baseline"] = cur.fetchone()
            return row

    def reason_detail_for_agent(self, article_id: str) -> str | None:
        """Reviewer notes, for the escalation payload only.

        Named so its one legitimate caller is obvious. The human agent picking up the
        ticket needs this; it must never reach a publisher-facing prompt.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT reason_detail FROM article_reviews WHERE article_id = %s",
                (article_id,),
            )
            row = cur.fetchone()
            return row["reason_detail"] if row else None

    def create_escalation(
        self,
        *,
        publisher_id: str,
        publisher_message: str,
        idempotency_key: str,
        article_id: str | None = None,
        reason_code: str | None = None,
        transcript: str | None = None,
    ) -> str:
        """Open a ticket, or return the existing one for this key.

        Resuming an interrupt re-runs the node from its start, so this can be called
        twice for one escalation. The unique key turns the second call into a lookup.
        """
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT ticket_id FROM escalations WHERE idempotency_key = %s",
                (idempotency_key,),
            )
            if existing := cur.fetchone():
                return existing["ticket_id"]

            detail = self.reason_detail_for_agent(article_id) if article_id else None
            ticket_id = f"ESC-{uuid.uuid4().hex[:12]}"
            cur.execute(
                """
                INSERT INTO escalations (
                    ticket_id, publisher_id, article_id, reason_code, reason_detail,
                    publisher_message, transcript, created_at, idempotency_key
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    ticket_id, publisher_id, article_id, reason_code, detail,
                    publisher_message, transcript, datetime.now(), idempotency_key,
                ),
            )
            return ticket_id

    def get_escalation(self, ticket_id: str) -> dict | None:
        with self._connect() as conn, conn.cursor() as cur:
            cur.execute("SELECT * FROM escalations WHERE ticket_id = %s", (ticket_id,))
            return cur.fetchone()

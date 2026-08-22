"""Retrieval and disposition tests.

Retrieval tests need Weaviate and an embedding key; they skip without them. The
disposition tests are pure and always run — they guard the rules that decide what a
publisher may be told.
"""

import json
import os
from pathlib import Path

import pytest

from cs_assistant.config import Settings
from cs_assistant.disposition import Action, for_pending, for_reason, is_always_escalated
from cs_assistant.sources.policy import load_chunks

CASES = json.loads((Path(__file__).parent / "retrieval_cases.json").read_text())
POLICY_DIR = Path(__file__).parent.parent / "policies"

needs_stack = pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"), reason="needs embeddings and Weaviate"
)


class TestDisposition:
    """These decide what may be said, so they are code and they are tested."""

    def test_anti_abuse_always_escalates(self):
        # Explaining why content was flagged hands the person evading detection a
        # feedback signal — and the person asking may be that person.
        d = for_reason("spam_detection")
        assert d.action is Action.ESCALATE
        assert d.disclosable is False

    def test_monetization_hold_always_escalates(self):
        assert for_reason("monetization_hold").action is Action.ESCALATE

    def test_account_restriction_always_escalates(self):
        assert for_reason("account_restriction").action is Action.ESCALATE

    def test_unknown_reason_code_defaults_to_escalation(self):
        # A reason code nobody has classified must not fall through to "answer freely".
        assert for_reason("some_new_code_added_next_quarter").action is Action.ESCALATE
        assert for_reason(None).action is Action.ESCALATE

    def test_copyright_is_answerable_with_appeal(self):
        d = for_reason("copyright")
        assert d.action is Action.ANSWER
        assert d.show_appeal_link

    def test_pending_within_sla_answers_with_timing(self):
        assert for_pending(hours_waiting=12).action is Action.ANSWER

    def test_pending_past_sla_escalates(self):
        assert for_pending(hours_waiting=96).action is Action.ESCALATE

    def test_permanently_escalating_topics(self):
        for code in ("spam_detection", "monetization_hold", "account_restriction"):
            assert is_always_escalated(code)
        assert not is_always_escalated("copyright")


class TestChunking:
    def test_chunks_split_on_headings(self):
        chunks = load_chunks(POLICY_DIR)
        assert len(chunks) > 10
        assert all(c.heading_path for c in chunks)

    def test_embed_text_carries_heading_path(self):
        # "deferred to the next business day" is meaningless alone; prefixed with
        # "Payout Timing > Holidays" it is retrievable.
        chunk = next(c for c in load_chunks(POLICY_DIR) if "Holidays" in c.heading_path)
        assert "Payout Timing" in chunk.embed_text
        assert chunk.text in chunk.embed_text

    def test_variants_ride_along_for_retrieval(self):
        chunk = next(c for c in load_chunks(POLICY_DIR) if c.query_variants)
        assert chunk.query_variants[0] in chunk.embed_text

    def test_every_policy_declares_an_effective_date(self):
        # Entries without one have neither date filtering nor decay protection.
        for chunk in load_chunks(POLICY_DIR):
            assert chunk.effective_from != "1970-01-01", chunk.source_file


@needs_stack
class TestRetrievalQuality:
    @pytest.fixture(scope="class")
    def index(self):
        from cs_assistant.sources.policy import PolicyIndex

        idx = PolicyIndex(Settings.from_env())
        idx.rebuild(POLICY_DIR)
        return idx

    def test_labeled_cases_land_on_the_right_document(self, index):
        misses = [
            c["query"]
            for c in CASES
            if index.search(c["query"], locale=c["locale"], limit=1)[0]["source_file"]
            != c["expect_file"]
        ]
        assert not misses, f"wrong document for: {misses}"

    def test_spanish_phrasings_absent_from_variants(self, index):
        """Cross-lingual semantics, not string overlap.

        Phrasings that appear verbatim in query_variants prove nothing — an
        English-only reranker matched those and still scored 0/4 here.
        """
        unseen = [
            ("todavía no recibí mi dinero del mes pasado", "settlement.md"),
            ("puedo publicar un artículo que ya salió en otro sitio", "originality.md"),
            ("mi articulo lleva tres dias esperando aprobacion", "review-process.md"),
        ]
        for query, expected in unseen:
            top = index.search(query, locale="es", limit=1)
            assert top[0]["source_file"] == expected, f"{query} -> {top[0]['source_file']}"

    def test_rerank_discriminates(self, index):
        """Near-identical scores mean the model has no signal for this query.

        The ranking still returns five results, so this failure is invisible unless the
        score spread is checked.
        """
        hits = index.search("where is my money", limit=5)
        scores = [h["rerank_score"] for h in hits]
        assert max(scores) - min(scores) > 0.01

    def test_vocabulary_overlap_does_not_win(self, index):
        """appeals.md shares wording with this query but does not answer it.

        Asserted on the final ranking only. An earlier version also required raw hybrid
        to get this wrong, which turned a fixed bug into a test failure once indexing
        the variants let BM25 rank it correctly on its own.
        """
        top = index.search("why was my article marked duplicate", limit=1)
        assert top[0]["source_file"] == "originality.md"

    def test_appeals_still_wins_its_own_question(self, index):
        """Guards against over-correcting: appeals.md must not be pushed down globally."""
        top = index.search("how do I appeal a rejection", limit=1)
        assert top[0]["source_file"] == "appeals.md"

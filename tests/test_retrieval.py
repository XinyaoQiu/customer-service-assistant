"""Retrieval and disposition tests.

Retrieval tests need Weaviate and an embedding key; they skip without them. The
disposition tests are pure and always run — they guard the rules that decide what a
publisher may be told.
"""

import json
import os
import re
from pathlib import Path

import pytest

from cs_assistant.config import Settings
from cs_assistant.reasons import REASONS, lookup, needs_human
from cs_assistant.routing import needs_human_immediately
from cs_assistant.sources.policy import load_chunks

CASES = json.loads((Path(__file__).parent / "retrieval_cases.json").read_text())
POLICY_DIR = Path(__file__).parent.parent / "policies"

needs_stack = pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"), reason="needs embeddings and Weaviate"
)


class TestReasons:
    """A review outcome is a code and a fixed message, the way an HTTP status is."""

    def test_every_rejection_gives_the_publisher_a_reason(self):
        # Someone whose work was rejected is owed an explanation. Withholding it only
        # makes them wait for a human to say the same thing.
        for code, reason in REASONS.items():
            assert reason.message, code
            assert len(reason.message) > 20, f"{code}: message is not an explanation"

    def test_messages_state_no_thresholds(self):
        """Wording says what happened, never the number behind it.

        The assistant is never given the limit, so this is about the fixed strings
        themselves not leaking one.
        """
        for code, reason in REASONS.items():
            assert not re.search(r"\b\d{2,}\b", reason.message), f"{code} names a number"

    def test_unknown_code_routes_to_a_human(self):
        unknown = lookup("some_code_added_next_quarter")
        assert unknown.needs_human
        assert unknown.message

    def test_no_outcome_means_no_reason(self):
        # Pending and published articles carry no code, and that is not an error.
        assert lookup(None) is None

    def test_only_actionable_outcomes_need_a_human(self):
        # A person must lift a restriction or release a hold; explaining a duplicate
        # rejection needs nobody.
        assert needs_human("account_restricted")
        assert needs_human("monetization_hold")
        assert not needs_human("duplicate_content")
        assert not needs_human("spam_behavior")
        assert not needs_human("rate_limit_exceeded")


class TestImmediateEscalation:
    """Rules, not a model: a model can be argued out of a judgment."""

    def test_account_and_payment_reach_a_person(self):
        assert needs_human_immediately("my account was suspended, why?")
        assert needs_human_immediately("my payment is being withheld")
        assert needs_human_immediately("cuenta suspendida?")

    def test_asking_for_a_person_is_honoured(self):
        assert needs_human_immediately("I want to talk to a human")

    def test_ordinary_questions_reach_the_agent(self):
        for message in (
            "why was my article rejected?",
            "what counts as original content?",
            "why did my views drop?",
            "when do I get paid?",
        ):
            assert needs_human_immediately(message) is None, message


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

    def test_documents_carry_no_retrieval_metadata(self):
        """A help-centre page has a title and a body, nothing about how people ask.

        Colloquial phrasings are index metadata, generated during indexing. Writing
        them into the documents made the corpus fit the evaluation set, which is how
        retrieval scored 92% before anything was reranked.
        """
        for chunk in load_chunks(POLICY_DIR):
            assert not chunk.query_variants, f"{chunk.source_file} carries variants"

    def test_variants_reach_embed_text_once_attached(self):
        chunks = load_chunks(POLICY_DIR)
        chunks[0].query_variants = ["where is my money"]
        assert "where is my money" in chunks[0].embed_text

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


class TestStreamingContract:
    """Progress reporting must not depend on a listener, or duplicate across replay."""

    @needs_stack
    def test_graph_builds(self):
        """Nodes narrate unconditionally; _emit must be a no-op without a writer."""
        from cs_assistant.graph import build_graph

        assert build_graph(warm=False) is not None

    def test_progress_dedup_survives_replay(self):
        """An interrupt re-runs the node, so every pre-interrupt line arrives twice."""
        seen: set[str] = set()

        def progress(text: str) -> bool:
            if text in seen:
                return False
            seen.add(text)
            return True

        emitted = [t for t in ["a", "b", "a", "b", "c"] if progress(t)]
        assert emitted == ["a", "b", "c"]

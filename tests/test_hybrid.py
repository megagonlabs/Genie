"""Unit Tests for Hybrid Search Mathematics.

This test suite verifies the core mathematical integrity of the ranking
algorithms (BM25, Cosine Similarity, and RRF) that power the RAG engine.
It ensures that vectors are compared correctly and that keyword searches
properly apply term-frequency and document-length penalties.
"""

import unittest
from models.memory import MemoryItem, MemoryScope
from storages.hybrid import cosine_similarity, tokenize, bm25_lite_score, reciprocal_rank_fusion


class TestHybridSearchMath(unittest.TestCase):
    """Test cases for the isolated mathematical search functions."""

    def setUp(self):
        """Sets up a standardized test corpus before every test."""
        # A long document containing the target keywords
        self.doc_fox_long = MemoryItem(id="doc1", scope=MemoryScope.GLOBAL, content="The quick brown fox jumps over the lazy dog.")

        # A much shorter document containing the same target keywords
        self.doc_fox_short = MemoryItem(id="doc2", scope=MemoryScope.GLOBAL, content="A fast brown fox leaps.")

        # Completely unrelated noise
        self.doc_unrelated = MemoryItem(id="doc3", scope=MemoryScope.GLOBAL, content="To bake a cake, you need flour and sugar.")

        self.corpus = [self.doc_fox_long, self.doc_fox_short, self.doc_unrelated]

    def test_tokenizer_cleans_text(self):
        """Verifies that the tokenizer correctly normalizes strings for lexical search."""
        # Arrange
        raw_text = "Hello, World! AI-Agents are: FAST."

        # Act
        tokens = tokenize(raw_text)

        # Assert: Everything should be lowercase and stripped of punctuation
        self.assertEqual(tokens, ["hello", "world", "aiagents", "are", "fast"])

    def test_cosine_similarity_identical_vectors(self):
        """Verifies that two identical vectors yield a perfect similarity score of 1.0."""
        # Arrange
        vec_a = [1.0, 0.0, 0.5]
        vec_b = [1.0, 0.0, 0.5]

        # Act
        score = cosine_similarity(vec_a, vec_b)

        # Assert (using AlmostEqual to account for minor floating-point math quirks)
        self.assertAlmostEqual(score, 1.0, places=5)

    def test_cosine_similarity_orthogonal_vectors(self):
        """Verifies that completely unrelated (perpendicular) vectors yield 0.0."""
        # Arrange: An X-axis vector and a Y-axis vector share no overlap
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]

        # Act
        score = cosine_similarity(vec_a, vec_b)

        # Assert
        self.assertEqual(score, 0.0)

    def test_cosine_similarity_edge_cases(self):
        """Verifies the math function doesn't crash on bad data inputs."""
        # Act & Assert
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)  # Zero magnitude vector
        self.assertEqual(cosine_similarity([1.0], [1.0, 2.0]), 0.0)  # Mismatched dimensions
        self.assertEqual(cosine_similarity([], []), 0.0)  # Empty arrays

    def test_bm25_scoring_exact_matches(self):
        """Verifies the BM25 lexical ranking algorithm."""
        # Arrange
        query = "brown fox"

        # Act
        scores = bm25_lite_score(query, self.corpus)

        # Assert 1: The unrelated cake document should get zero points
        self.assertEqual(scores[self.doc_unrelated.id], 0.0)

        # Assert 2: Both fox documents should get positive scores
        self.assertGreater(scores[self.doc_fox_long.id], 0.0)
        self.assertGreater(scores[self.doc_fox_short.id], 0.0)

        # Assert 3: THE BM25 LENGTH PENALTY.
        # Because doc2 is much shorter, "brown fox" makes up a larger percentage
        # of its total words. Therefore, it mathematically must outrank doc1.
        self.assertGreater(scores[self.doc_fox_short.id], scores[self.doc_fox_long.id])

    def test_bm25_empty_query(self):
        """Verifies BM25 safely handles empty string queries."""
        # Act
        scores = bm25_lite_score("", self.corpus)

        # Assert
        self.assertTrue(all(score == 0.0 for score in scores.values()))

    def test_reciprocal_rank_fusion(self):
        """Verifies the RRF algorithm properly merges and smooths conflicting rankings."""
        # Arrange: Two algorithms totally disagree on the rankings
        vector_ranked = [self.doc_unrelated, self.doc_fox_long, self.doc_fox_short]
        keyword_ranked = [self.doc_fox_short, self.doc_fox_long, self.doc_unrelated]

        # Act: Fuse them together
        fused_results = reciprocal_rank_fusion([vector_ranked, keyword_ranked], k=60)

        # Assert
        self.assertEqual(len(fused_results), 3)

        # Ensure we actually get a MemoryItem object out of the top slot
        top_item = fused_results[0]
        self.assertIsNotNone(top_item.id)


if __name__ == '__main__':
    unittest.main()

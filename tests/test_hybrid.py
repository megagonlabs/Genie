import unittest
from models.memory import MemoryItem, MemoryScope
from storage.hybrid import cosine_similarity, tokenize, bm25_lite_score, reciprocal_rank_fusion


class TestHybridSearchMath(unittest.TestCase):

    def setUp(self):
        self.doc_fox_long = MemoryItem(id="doc1", scope=MemoryScope.GLOBAL, content="The quick brown fox jumps over the lazy dog.")
        self.doc_fox_short = MemoryItem(id="doc2", scope=MemoryScope.GLOBAL, content="A fast brown fox leaps.")
        self.doc_unrelated = MemoryItem(id="doc3", scope=MemoryScope.GLOBAL, content="To bake a cake, you need flour and sugar.")
        self.corpus = [self.doc_fox_long, self.doc_fox_short, self.doc_unrelated]

    def test_tokenizer_cleans_text(self):
        raw_text = "Hello, World! AI-Agents are: FAST."
        tokens = tokenize(raw_text)
        self.assertEqual(tokens, ["hello", "world", "aiagents", "are", "fast"])

    def test_cosine_similarity_identical_vectors(self):
        vec_a = [1.0, 0.0, 0.5]
        vec_b = [1.0, 0.0, 0.5]
        score = cosine_similarity(vec_a, vec_b)
        self.assertAlmostEqual(score, 1.0, places=5)

    def test_cosine_similarity_orthogonal_vectors(self):
        vec_a = [1.0, 0.0]
        vec_b = [0.0, 1.0]
        score = cosine_similarity(vec_a, vec_b)
        self.assertEqual(score, 0.0)

    def test_cosine_similarity_edge_cases(self):
        self.assertEqual(cosine_similarity([0.0, 0.0], [1.0, 1.0]), 0.0)  # Zero magnitude
        self.assertEqual(cosine_similarity([1.0], [1.0, 2.0]), 0.0)  # Mismatched length
        self.assertEqual(cosine_similarity([], []), 0.0)  # Empty

    def test_bm25_scoring_exact_matches(self):
        query = "brown fox"
        scores = bm25_lite_score(query, self.corpus)
        self.assertEqual(scores[self.doc_unrelated.id], 0.0)
        self.assertGreater(scores[self.doc_fox_long.id], 0.0)
        self.assertGreater(scores[self.doc_fox_short.id], 0.0)
        self.assertGreater(scores[self.doc_fox_short.id], scores[self.doc_fox_long.id])

    def test_bm25_empty_query(self):
        scores = bm25_lite_score("", self.corpus)
        self.assertTrue(all(score == 0.0 for score in scores.values()))

    def test_reciprocal_rank_fusion(self):
        vector_ranked = [self.doc_unrelated, self.doc_fox_long, self.doc_fox_short]
        keyword_ranked = [self.doc_fox_short, self.doc_fox_long, self.doc_unrelated]
        fused_results = reciprocal_rank_fusion([vector_ranked, keyword_ranked], k=60)
        self.assertEqual(len(fused_results), 3)
        top_item = fused_results[0]
        self.assertIsNotNone(top_item.id)


if __name__ == '__main__':
    unittest.main()

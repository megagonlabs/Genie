"""Hybrid Search Mathematics and Ranking Algorithms.

This module provides the core mathematical functions required to execute
a true Hybrid Search without relying on massive external database dependencies
(like Elasticsearch or Pinecone). It implements Semantic Vector scoring (Cosine),
Lexical Keyword scoring (Okapi BM25), and list merging (Reciprocal Rank Fusion).
"""

import math
import string
from collections import Counter
from typing import List, Dict
from models.memory import MemoryItem


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    """Calculates the cosine similarity between two high-dimensional vectors.

    This measures the angle between two embedding vectors. A score of 1.0
    means identical direction (highly similar semantic meaning), while 0.0
    means orthogonal (unrelated).

    Parameters:
        vec1: The first vector (e.g., the embedded search query).
        vec2: The second vector (e.g., the embedded memory item).

    Returns:
        A float representing the similarity score between 0.0 and 1.0.
    """
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0

    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    mag1 = math.sqrt(sum(a * a for a in vec1))
    mag2 = math.sqrt(sum(b * b for b in vec2))

    if mag1 == 0 or mag2 == 0:
        return 0.0

    return dot_product / (mag1 * mag2)


def tokenize(text: str) -> List[str]:
    """Normalizes and splits a string into a list of basic keyword tokens.

    Converts text to lowercase and strips all punctuation. This is a
    lightweight precursor to the BM25 lexical search algorithm.
    """
    if not isinstance(text, str):
        text = str(text)
    text = text.lower().translate(str.maketrans('', '', string.punctuation))
    return text.split()


def bm25_lite_score(query: str, corpus: List[MemoryItem], k1: float = 1.5, b: float = 0.75) -> Dict[str, float]:
    """Calculates the Okapi BM25 lexical relevance score for the corpus.

    BM25 is the industry standard for keyword search. It improves upon simple
    TF-IDF by factoring in document length (preventing long documents from
    unfairly dominating) and term frequency saturation (mentioning a word 10
    times isn't 10x better than mentioning it once).

    Parameters:
        query: The raw string the user or agent is searching for.
        corpus: The isolated list of MemoryItems to search through.
        k1: Controls term frequency saturation (standard default is 1.5).
        b: Controls how much document length penalizes the score (default 0.75).

    Returns:
        A dictionary mapping MemoryItem IDs to their float BM25 score.
    """
    query_tokens = tokenize(query)
    if not query_tokens or not corpus:
        return {item.id: 0.0 for item in corpus}

    # Pre-compute tokenized representations for the entire corpus
    doc_tokens_map = {item.id: tokenize(str(item.content)) for item in corpus}
    doc_lengths = {doc_id: len(tokens) for doc_id, tokens in doc_tokens_map.items()}

    # Calculate the average document length (avgdl)
    avgdl = sum(doc_lengths.values()) / len(doc_lengths) if doc_lengths else 1.0
    N = len(corpus)

    # --- Step 1: Calculate Inverse Document Frequency (IDF) ---
    # Words that appear in almost every document (like "the" or "is") get a low score.
    # Rare, highly specific words get a massive score multiplier.
    idf: Dict[str, float] = {}
    for token in set(query_tokens):
        doc_count = sum(1 for tokens in doc_tokens_map.values() if token in tokens)
        idf[token] = math.log(1 + (N - doc_count + 0.5) / (doc_count + 0.5))

    # --- Step 2: Calculate specific BM25 score for each document ---
    scores: Dict[str, float] = {}
    for item in corpus:
        doc_id = item.id
        tokens = doc_tokens_map[doc_id]
        term_freqs = Counter(tokens)
        doc_len = doc_lengths[doc_id]
        score = 0.0

        for token in query_tokens:
            if token not in term_freqs:
                continue

            tf = term_freqs[token]

            # The BM25 algorithm formula
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * (doc_len / avgdl))
            score += idf[token] * (numerator / denominator)

        scores[doc_id] = score

    return scores


def reciprocal_rank_fusion(ranked_lists: List[List[MemoryItem]], k: int = 60) -> List[MemoryItem]:
    """Fuses multiple ranked lists into a single consensus ranking.

    RRF is the standard algorithm for combining Vector Search results with
    BM25 Keyword Search results. It doesn't look at the raw scores (which are
    incompatible scales), it only looks at the absolute ranking position.

    Formula: RRF_Score = 1 / (k + rank_in_list)

    Parameters:
        ranked_lists: A list containing lists of MemoryItems, where each inner
                      list is already sorted best-to-worst by a specific algorithm.
        k: A smoothing constant (standard default is 60) that prevents the #1
           item in one list from entirely dominating the final outcome.

    Returns:
        A single, newly sorted list of MemoryItems representing the best
        combined results across all search algorithms.
    """
    rrf_scores: Dict[str, float] = {}
    items_lookup: Dict[str, MemoryItem] = {}

    for ranked_list in ranked_lists:
        # Enumerate starts at 1 so the top rank mathematically works in the formula
        for rank, item in enumerate(ranked_list, start=1):
            items_lookup[item.id] = item
            # If an item appears in multiple lists, its RRF fractions are added together
            rrf_scores[item.id] = rrf_scores.get(item.id, 0.0) + (1.0 / (k + rank))

    # Sort the items by their final fused RRF score in descending order
    sorted_item_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    return [items_lookup[item_id] for item_id in sorted_item_ids]

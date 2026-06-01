import math
import string
from collections import Counter
from typing import List, Dict
from models.memory import MemoryItem


def cosine_similarity(vec1: List[float], vec2: List[float]) -> float:
    if not vec1 or not vec2 or len(vec1) != len(vec2):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec1, vec2))
    mag1 = math.sqrt(sum(a * a for a in vec1))
    mag2 = math.sqrt(sum(b * b for b in vec2))
    if mag1 == 0 or mag2 == 0:
        return 0.0
    return dot_product / (mag1 * mag2)


def tokenize(text: str) -> List[str]:
    if not isinstance(text, str):
        text = str(text)
    text = text.lower().translate(str.maketrans('', '', string.punctuation))
    return text.split()


def bm25_lite_score(query: str, corpus: List[MemoryItem], k1: float = 1.5, b: float = 0.75) -> Dict[str, float]:
    query_tokens = tokenize(query)
    if not query_tokens or not corpus:
        return {item.id: 0.0 for item in corpus}
    doc_tokens_map = {item.id: tokenize(str(item.content)) for item in corpus}
    doc_lengths = {doc_id: len(tokens) for doc_id, tokens in doc_tokens_map.items()}
    avgdl = sum(doc_lengths.values()) / len(doc_lengths) if doc_lengths else 1.0
    N = len(corpus)
    idf: Dict[str, float] = {}
    for token in set(query_tokens):
        doc_count = sum(1 for tokens in doc_tokens_map.values() if token in tokens)
        idf[token] = math.log(1 + (N - doc_count + 0.5) / (doc_count + 0.5))
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
            numerator = tf * (k1 + 1)
            denominator = tf + k1 * (1 - b + b * (doc_len / avgdl))
            score += idf[token] * (numerator / denominator)
        scores[doc_id] = score
    return scores


def reciprocal_rank_fusion(ranked_lists: List[List[MemoryItem]], k: int = 60) -> List[MemoryItem]:
    rrf_scores: Dict[str, float] = {}
    items_lookup: Dict[str, MemoryItem] = {}
    for ranked_list in ranked_lists:
        for rank, item in enumerate(ranked_list, start=1):
            items_lookup[item.id] = item
            rrf_scores[item.id] = rrf_scores.get(item.id, 0.0) + (1.0 / (k + rank))
    sorted_item_ids = sorted(rrf_scores.keys(), key=lambda x: rrf_scores[x], reverse=True)
    return [items_lookup[item_id] for item_id in sorted_item_ids]

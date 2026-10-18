"""BM25, dense cosine retrieval and reciprocal-rank fusion."""
from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

TOKEN_RE = re.compile(r"[A-Za-z0-9]+|[\u3400-\u9fff]")


def tokenize(text: str) -> list[str]:
    return [match.group(0).casefold() for match in TOKEN_RE.finditer(text)]


def bm25_scores(query: str, chunks: list[dict[str, Any]], *, k1: float = 1.5, b: float = 0.75) -> list[tuple[int, float]]:
    qterms = set(tokenize(query))
    docs = [tokenize(chunk["text"]) for chunk in chunks]
    if not docs or not qterms:
        return []
    avgdl = sum(map(len, docs)) / len(docs) or 1.0
    frequencies = Counter(term for doc in docs for term in set(doc))
    scores = []
    for i, doc in enumerate(docs):
        counts = Counter(doc)
        score = 0.0
        for term in qterms:
            tf = counts[term]
            if not tf:
                continue
            df = frequencies[term]
            idf = math.log(1 + (len(docs) - df + 0.5) / (df + 0.5))
            score += idf * tf * (k1 + 1) / (tf + k1 * (1 - b + b * len(doc) / avgdl))
        if score > 0:
            scores.append((i, score))
    return sorted(scores, key=lambda pair: (-pair[1], chunks[pair[0]]["id"]))


def cosine(a: list[float], b: list[float]) -> float:
    if len(a) != len(b) or not a:
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def dense_scores(query_vector: list[float], chunks: list[dict[str, Any]], vectors: dict[str, list[float]]) -> list[tuple[int, float]]:
    scored = [(i, cosine(query_vector, vectors.get(chunk["id"], []))) for i, chunk in enumerate(chunks)]
    return sorted(((i, score) for i, score in scored if score > 0), key=lambda pair: (-pair[1], chunks[pair[0]]["id"]))


def reciprocal_rank_fusion(rankings: dict[str, list[tuple[int, float]]], *, k: int = 60, limit: int = 10) -> list[dict[str, Any]]:
    if k <= 0 or limit <= 0:
        raise ValueError("k and limit must be positive")
    merged: dict[int, dict[str, Any]] = {}
    for source, ranking in rankings.items():
        seen = set()
        for rank, (item_index, score) in enumerate(ranking, start=1):
            if item_index in seen:
                continue
            seen.add(item_index)
            entry = merged.setdefault(item_index, {"index": item_index, "rrf_score": 0.0, "sources": {}, "ranks": {}})
            entry["rrf_score"] += 1.0 / (k + rank)
            entry["sources"][source] = score
            entry["ranks"][source] = rank
    return sorted(merged.values(), key=lambda item: (-item["rrf_score"], item["index"]))[:limit]

"""Exact and conservative TF-IDF-style matching using only the standard library."""

from __future__ import annotations

import math
from collections import Counter

from aura_cad.cache.models import CachedPart, MatchCandidate, PartSignature
from aura_cad.config import PART_CACHE_FUZZY_THRESHOLD


class PartMatcher:
    """Rank exact signatures first, then feature-token cosine similarity."""

    def __init__(self, threshold: float = PART_CACHE_FUZZY_THRESHOLD) -> None:
        self.threshold = max(0.0, min(1.0, float(threshold)))

    def rank(
        self, signature: PartSignature, parts: list[CachedPart]
    ) -> list[MatchCandidate]:
        exact = [
            MatchCandidate(part, 1.0, True)
            for part in parts
            if part.signature == signature.value
        ]
        if exact:
            return sorted(exact, key=lambda match: match.part.last_used_at, reverse=True)
        # Add future deterministic match strategies here, before thresholding.
        scores = self._fuzzy_scores(signature, parts)
        candidates = [
            MatchCandidate(part, score)
            for part, score in scores
            if score >= self.threshold
        ]
        return sorted(candidates, key=lambda match: match.confidence, reverse=True)

    def _fuzzy_scores(
        self, signature: PartSignature, parts: list[CachedPart]
    ) -> list[tuple[CachedPart, float]]:
        documents = [set(part.tags) | set(part.object_family.split("_")) for part in parts]
        query = set(signature.tags) | set(signature.object_family.split("_"))
        counts = Counter(token for document in documents for token in document)
        total = max(1, len(documents))
        idf = {
            token: math.log((total + 1) / (count + 1)) + 1.0
            for token, count in counts.items()
        }
        return [
            (part, self._cosine(query, document, idf, signature.object_family, part))
            for part, document in zip(parts, documents)
        ]

    @staticmethod
    def _cosine(query, document, idf, family, part) -> float:
        if not query or not document:
            return 0.0
        overlap = query & document
        numerator = sum(idf.get(token, 1.0) ** 2 for token in overlap)
        left = sum(idf.get(token, 1.0) ** 2 for token in query)
        right = sum(idf.get(token, 1.0) ** 2 for token in document)
        score = numerator / math.sqrt(left * right) if left and right else 0.0
        if family != part.object_family:
            score *= 0.55
        return min(1.0, score)

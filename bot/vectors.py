"""Простой векторный поиск: эмбеддинги хранятся в SQLite, поиск — косинус в numpy.

Для личного бота (тысячи-десятки тысяч фрагментов) это быстро и не требует
отдельного сервиса вроде Qdrant.
"""

from typing import Protocol

import numpy as np


class Embedder(Protocol):
    async def embed(self, model: str, texts: list[str]) -> list[list[float]]: ...


def to_blob(vector: list[float]) -> bytes:
    """Нормализует вектор и упаковывает в байты (float32)."""
    arr = np.asarray(vector, dtype=np.float32)
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.tobytes()


def from_blob(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def top_k(query: list[float], matrix: np.ndarray, k: int, min_score: float) -> list[tuple[int, float]]:
    """Индексы и косинусная близость k ближайших строк matrix (строки нормализованы)."""
    if matrix.size == 0:
        return []
    q = from_blob(to_blob(query))
    if q.shape[0] != matrix.shape[1]:
        return []  # сменили модель эмбеддингов — старые векторы несовместимы
    scores = matrix @ q
    order = np.argsort(-scores)[:k]
    return [(int(i), float(scores[i])) for i in order if scores[i] >= min_score]

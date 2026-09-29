"""Долговременная память: факты о пользователе, которые бот вспоминает по смыслу."""

import asyncio
import json
import logging

import numpy as np

from .db import Database
from .llm import LLMError, OllamaClient
from .vectors import from_blob, to_blob, top_k

log = logging.getLogger(__name__)

DUPLICATE_SCORE = 0.9
MIN_TEXT_FOR_EXTRACTION = 20
MAX_FACT_LEN = 300

EXTRACT_PROMPT = (
    "Ты извлекаешь долговременные факты о пользователе из его сообщения. "
    "Факт — это устойчивая информация: имя, чем занимается, проекты, уровень, "
    "предпочтения, аллергии, техника, цели. НЕ факты: вопросы, временные просьбы, "
    "содержимое кода, общие знания. Каждый факт — короткое предложение от третьего "
    'лица ("Пользователь ..."). Верни JSON: {"facts": ["..."]}. '
    'Если фактов нет — {"facts": []}.'
)


class MemoryStore:
    def __init__(self, db: Database, llm: OllamaClient, embed_model: str) -> None:
        self._db = db
        self._llm = llm
        self._embed_model = embed_model
        # Фоновое извлечение фактов по одному, чтобы не отнимать GPU у ответов
        self._extract_lock = asyncio.Lock()

    async def _user_matrix(self, user_id: int) -> tuple[list[int], list[str], np.ndarray]:
        async with self._db.conn.execute(
            "SELECT id, text, embedding FROM memories WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            return [], [], np.empty((0, 0), dtype=np.float32)
        vectors = [from_blob(r[2]) for r in rows]
        dim = vectors[-1].shape[0]
        keep = [i for i, v in enumerate(vectors) if v.shape[0] == dim]
        return (
            [rows[i][0] for i in keep],
            [rows[i][1] for i in keep],
            np.vstack([vectors[i] for i in keep]),
        )

    async def add(self, user_id: int, text: str) -> int | None:
        """Сохраняет факт. None — если почти такой же уже есть."""
        text = text.strip()[:MAX_FACT_LEN]
        if not text:
            return None
        [vector] = await self._llm.embed(self._embed_model, [text])
        _, _, matrix = await self._user_matrix(user_id)
        if top_k(vector, matrix, 1, DUPLICATE_SCORE):
            return None
        cur = await self._db.conn.execute(
            "INSERT INTO memories (user_id, text, embedding) VALUES (?, ?, ?)",
            (user_id, text, to_blob(vector)),
        )
        await self._db.conn.commit()
        return cur.lastrowid

    async def list_facts(self, user_id: int) -> list[tuple[int, str]]:
        async with self._db.conn.execute(
            "SELECT id, text FROM memories WHERE user_id = ? ORDER BY id", (user_id,)
        ) as cur:
            return [(r[0], r[1]) for r in await cur.fetchall()]

    async def delete(self, user_id: int, memory_id: int) -> bool:
        cur = await self._db.conn.execute(
            "DELETE FROM memories WHERE id = ? AND user_id = ?", (memory_id, user_id)
        )
        await self._db.conn.commit()
        return cur.rowcount > 0

    async def clear(self, user_id: int) -> int:
        cur = await self._db.conn.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
        await self._db.conn.commit()
        return cur.rowcount

    async def search(self, user_id: int, query: str, k: int, min_score: float) -> list[str]:
        _, texts, matrix = await self._user_matrix(user_id)
        if not texts:
            return []
        [vector] = await self._llm.embed(self._embed_model, [query])
        return [texts[i] for i, _ in top_k(vector, matrix, k, min_score)]

    async def extract_and_store(self, user_id: int, user_text: str, model: str) -> list[str]:
        """Просит модель вытащить факты из сообщения пользователя и сохраняет новые."""
        if len(user_text) < MIN_TEXT_FOR_EXTRACTION:
            return []
        async with self._extract_lock:
            try:
                raw = await self._llm.chat(
                    model,
                    [
                        {"role": "system", "content": EXTRACT_PROMPT},
                        {"role": "user", "content": user_text[:4000]},
                    ],
                    json_mode=True,
                )
                facts = _parse_facts(raw)
                saved = []
                for fact in facts:
                    if await self.add(user_id, fact) is not None:
                        saved.append(fact)
                if saved:
                    log.info("memory: user %s +%d facts", user_id, len(saved))
                return saved
            except LLMError as exc:
                log.warning("memory extraction failed: %s", exc)
                return []


def _parse_facts(raw: str) -> list[str]:
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    facts = data.get("facts", []) if isinstance(data, dict) else data
    if not isinstance(facts, list):
        return []
    return [f.strip() for f in facts if isinstance(f, str) and f.strip()][:5]

"""Личные документы пользователя: у каждого своя маленькая база знаний."""

import asyncio
from dataclasses import dataclass

import numpy as np

from .db import Database
from .knowledge import Passage, chunk_text, read_document_bytes
from .llm import OllamaClient
from .vectors import from_blob, to_blob, top_k

SUPPORTED = (".pdf", ".md", ".txt")
MAX_DOCS_PER_USER = 50
MAX_CHUNKS_PER_DOC = 2000


class DocError(Exception):
    pass


@dataclass(frozen=True)
class DocInfo:
    id: int
    name: str
    chunks: int
    created_at: str


class PersonalDocs:
    def __init__(self, db: Database, llm: OllamaClient, embed_model: str) -> None:
        self._db = db
        self._llm = llm
        self._embed_model = embed_model

    async def add(self, user_id: int, name: str, data: bytes) -> DocInfo:
        docs = await self.list_docs(user_id)
        if len(docs) >= MAX_DOCS_PER_USER:
            raise DocError(f"Максимум {MAX_DOCS_PER_USER} документов — удали лишние через /docs")
        try:
            text = await asyncio.to_thread(read_document_bytes, name, data)
        except Exception as exc:  # битый PDF и т.п.
            raise DocError(f"Не удалось прочитать файл: {exc}") from exc
        chunks = chunk_text(text)[:MAX_CHUNKS_PER_DOC]
        if not chunks:
            raise DocError("В документе не нашлось текста (скан без текстового слоя?)")
        vectors = await self._llm.embed(self._embed_model, chunks)
        conn = self._db.conn
        cur = await conn.execute(
            "INSERT INTO docs (user_id, name, chunks) VALUES (?, ?, ?)", (user_id, name, len(chunks))
        )
        doc_id = int(cur.lastrowid)
        await conn.executemany(
            "INSERT INTO doc_chunks (doc_id, text, embedding) VALUES (?, ?, ?)",
            [(doc_id, c, to_blob(v)) for c, v in zip(chunks, vectors, strict=True)],
        )
        await conn.commit()
        return DocInfo(doc_id, name, len(chunks), "")

    async def list_docs(self, user_id: int) -> list[DocInfo]:
        rows = await self._db._fetchall(
            "SELECT id, name, chunks, created_at FROM docs WHERE user_id = ? ORDER BY id", (user_id,)
        )
        return [DocInfo(*r) for r in rows]

    async def delete(self, user_id: int, doc_id: int) -> bool:
        cur = await self._db._exec("DELETE FROM docs WHERE id = ? AND user_id = ?", (doc_id, user_id))
        return cur.rowcount > 0

    async def search(self, user_id: int, query: str, k: int, min_score: float) -> list[Passage]:
        rows = await self._db._fetchall(
            "SELECT d.name, c.text, c.embedding FROM doc_chunks c JOIN docs d ON d.id = c.doc_id "
            "WHERE d.user_id = ?",
            (user_id,),
        )
        if not rows:
            return []
        vectors = [from_blob(r[2]) for r in rows]
        dim = vectors[0].shape[0]
        keep = [i for i, v in enumerate(vectors) if v.shape[0] == dim]
        matrix = np.vstack([vectors[i] for i in keep])
        [vector] = await self._llm.embed(self._embed_model, [query])
        return [
            Passage(rows[keep[i]][0], rows[keep[i]][1], score)
            for i, score in top_k(vector, matrix, k, min_score)
        ]

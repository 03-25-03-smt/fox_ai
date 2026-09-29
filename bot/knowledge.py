"""База знаний (RAG): Norm, subjects, заметки. Файлы лежат в папке knowledge/."""

import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .db import Database
from .llm import OllamaClient
from .vectors import from_blob, to_blob, top_k

log = logging.getLogger(__name__)

SUPPORTED = {".md", ".txt", ".pdf"}
CHUNK_SIZE = 1000
CHUNK_OVERLAP = 150


@dataclass
class IndexStats:
    indexed: int = 0
    skipped: int = 0
    removed: int = 0
    chunks: int = 0
    failed: int = 0


@dataclass(frozen=True)
class Passage:
    source: str
    text: str
    score: float


def chunk_text(text: str, size: int = CHUNK_SIZE, overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Режет текст на куски ~size символов по абзацам, с перекрытием."""
    text = re.sub(r"[ \t]+", " ", text)
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    for para in paragraphs:
        while len(para) > size:  # слишком длинный абзац режем принудительно
            head, para = para[:size], para[size - overlap :]
            if current:
                chunks.append(current)
                current = ""
            chunks.append(head)
        if current and len(current) + len(para) + 2 > size:
            chunks.append(current)
            current = current[-overlap:] + "\n\n" + para if overlap else para
        else:
            current = f"{current}\n\n{para}" if current else para
    if current:
        chunks.append(current)
    return chunks


def read_document(path: Path) -> str:
    return read_document_bytes(path.name, path.read_bytes())


def read_document_bytes(name: str, data: bytes) -> str:
    if name.lower().endswith(".pdf"):
        import io

        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        return "\n\n".join(page.extract_text() or "" for page in reader.pages)
    return data.decode("utf-8", errors="replace")


class KnowledgeBase:
    def __init__(self, db: Database, llm: OllamaClient, embed_model: str, root: str) -> None:
        self._db = db
        self._llm = llm
        self._embed_model = embed_model
        self._root = Path(root)
        self._cache: tuple[list[str], list[str], np.ndarray] | None = None
        self._lock = asyncio.Lock()

    def _files(self) -> list[Path]:
        if not self._root.is_dir():
            return []
        return sorted(
            p for p in self._root.rglob("*")
            if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith(".")
        )

    async def reindex(self) -> IndexStats:
        """Индексирует новые/изменённые файлы, удаляет пропавшие."""
        async with self._lock:
            stats = IndexStats()
            conn = self._db.conn
            async with conn.execute("SELECT path, sha256, embed_model FROM kb_files") as cur:
                known = {r[0]: (r[1], r[2]) for r in await cur.fetchall()}

            seen: set[str] = set()
            for path in self._files():
                rel = path.relative_to(self._root).as_posix()
                seen.add(rel)
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                if known.get(rel) == (digest, self._embed_model):
                    stats.skipped += 1
                    continue
                try:
                    text = await asyncio.to_thread(read_document, path)
                    chunks = chunk_text(text)
                    vectors = await self._llm.embed(self._embed_model, chunks) if chunks else []
                except Exception as exc:  # битый PDF, недоступная Ollama и т.п.
                    log.warning("knowledge: failed to index %s: %s", rel, exc)
                    stats.failed += 1
                    continue
                await conn.execute("DELETE FROM kb_files WHERE path = ?", (rel,))
                await conn.execute(
                    "INSERT INTO kb_files (path, sha256, embed_model) VALUES (?, ?, ?)",
                    (rel, digest, self._embed_model),
                )
                await conn.executemany(
                    "INSERT INTO kb_chunks (path, text, embedding) VALUES (?, ?, ?)",
                    [(rel, c, to_blob(v)) for c, v in zip(chunks, vectors, strict=True)],
                )
                await conn.commit()
                stats.indexed += 1
                stats.chunks += len(chunks)
                log.info("knowledge: indexed %s (%d chunks)", rel, len(chunks))

            for rel in set(known) - seen:
                await conn.execute("DELETE FROM kb_files WHERE path = ?", (rel,))
                stats.removed += 1
            await conn.commit()
            self._cache = None
            return stats

    async def _matrix(self) -> tuple[list[str], list[str], np.ndarray]:
        if self._cache is None:
            async with self._db.conn.execute(
                "SELECT c.path, c.text, c.embedding FROM kb_chunks c "
                "JOIN kb_files f ON f.path = c.path WHERE f.embed_model = ?",
                (self._embed_model,),
            ) as cur:
                rows = await cur.fetchall()
            matrix = (
                np.vstack([from_blob(r[2]) for r in rows])
                if rows else np.empty((0, 0), dtype=np.float32)
            )
            self._cache = ([r[0] for r in rows], [r[1] for r in rows], matrix)
        return self._cache

    async def search(self, query: str, k: int, min_score: float) -> list[Passage]:
        sources, texts, matrix = await self._matrix()
        if not texts:
            return []
        [vector] = await self._llm.embed(self._embed_model, [query])
        return [Passage(sources[i], texts[i], s) for i, s in top_k(vector, matrix, k, min_score)]

    async def stats(self) -> tuple[int, int]:
        async with self._db.conn.execute(
            "SELECT (SELECT COUNT(*) FROM kb_files), (SELECT COUNT(*) FROM kb_chunks)"
        ) as cur:
            files, chunks = await cur.fetchone()
        return files, chunks

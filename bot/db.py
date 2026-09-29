"""Хранилище: пользователи, настройки, история, память и база знаний (SQLite)."""

from dataclasses import dataclass
from pathlib import Path

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    model      TEXT,
    mode       TEXT,
    added_by   INTEGER,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);

CREATE TABLE IF NOT EXISTS memories (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    text       TEXT NOT NULL,
    embedding  BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);

CREATE TABLE IF NOT EXISTS kb_files (
    path        TEXT PRIMARY KEY,
    sha256      TEXT NOT NULL,
    embed_model TEXT NOT NULL,
    indexed_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS kb_chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    path      TEXT NOT NULL REFERENCES kb_files(path) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    embedding BLOB NOT NULL
);
"""

# Колонки, добавленные после первой версии: (таблица, колонка, определение)
MIGRATIONS = [("users", "mode", "TEXT")]


@dataclass(frozen=True)
class User:
    id: int
    name: str
    model: str | None
    mode: str | None


class Database:
    def __init__(self, path: str) -> None:
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self._path != ":memory:":
            Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            async with self._conn.execute(f"PRAGMA table_info({table})") as cur:
                columns = {row[1] for row in await cur.fetchall()}
            if column not in columns:
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    # --- пользователи ---

    async def add_user(self, user_id: int, name: str = "", added_by: int | None = None) -> bool:
        """Добавляет пользователя. Возвращает False, если он уже был."""
        cur = await self.conn.execute(
            "INSERT OR IGNORE INTO users (id, name, added_by) VALUES (?, ?, ?)",
            (user_id, name, added_by),
        )
        await self.conn.commit()
        return cur.rowcount > 0

    async def remove_user(self, user_id: int) -> bool:
        cur = await self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        await self.conn.commit()
        return cur.rowcount > 0

    async def is_user(self, user_id: int) -> bool:
        async with self.conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)) as cur:
            return await cur.fetchone() is not None

    async def list_users(self) -> list[User]:
        async with self.conn.execute(
            "SELECT id, name, model, mode FROM users ORDER BY created_at"
        ) as cur:
            return [User(*row) for row in await cur.fetchall()]

    async def _get_field(self, user_id: int, field: str) -> str | None:
        async with self.conn.execute(f"SELECT {field} FROM users WHERE id = ?", (user_id,)) as cur:
            row = await cur.fetchone()
        return row[0] if row else None

    async def get_model(self, user_id: int) -> str | None:
        return await self._get_field(user_id, "model")

    async def set_model(self, user_id: int, model: str) -> None:
        await self.conn.execute("UPDATE users SET model = ? WHERE id = ?", (model, user_id))
        await self.conn.commit()

    async def get_mode(self, user_id: int) -> str | None:
        return await self._get_field(user_id, "mode")

    async def set_mode(self, user_id: int, mode: str) -> None:
        await self.conn.execute("UPDATE users SET mode = ? WHERE id = ?", (mode, user_id))
        await self.conn.commit()

    # --- история ---

    async def add_message(self, user_id: int, role: str, content: str) -> None:
        await self.conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await self.conn.commit()

    async def get_history(self, user_id: int, limit: int) -> list[dict[str, str]]:
        """Последние `limit` сообщений в хронологическом порядке."""
        async with self.conn.execute(
            "SELECT role, content FROM messages WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cur:
            rows = await cur.fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    async def clear_history(self, user_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return cur.rowcount

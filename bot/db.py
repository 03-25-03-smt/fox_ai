"""Хранилище (SQLite): пользователи и их настройки, диалоги, резюме, память,
база знаний, личные документы, напоминания, лимиты, разрешённые группы."""

import datetime
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id             INTEGER PRIMARY KEY,
    name           TEXT NOT NULL DEFAULT '',
    model          TEXT,
    mode           TEXT,
    persona        TEXT,
    temperature    REAL,
    length         TEXT,
    voice_reply    INTEGER NOT NULL DEFAULT 0,
    tz             TEXT,
    intra_login    TEXT,
    intra_notified TEXT,
    added_by       INTEGER,
    created_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Диалоги. chat_id: для личных чатов = id пользователя, для групп = id группы.
CREATE TABLE IF NOT EXISTS chat_messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    user_id    INTEGER,
    role       TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
    content    TEXT NOT NULL,
    tg_msg_id  INTEGER,
    model      TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_messages ON chat_messages(chat_id, id);

-- Резюме старой части диалога (всё с id <= upto_id уже свёрнуто в text)
CREATE TABLE IF NOT EXISTS summaries (
    chat_id INTEGER PRIMARY KEY,
    text    TEXT NOT NULL,
    upto_id INTEGER NOT NULL
);

-- Последний присланный код и контекст режима защиты
CREATE TABLE IF NOT EXISTS chat_state (
    chat_id      INTEGER PRIMARY KEY,
    code_name    TEXT,
    files        TEXT,
    defense_code TEXT
);

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

CREATE TABLE IF NOT EXISTS docs (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    name       TEXT NOT NULL,
    chunks     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS doc_chunks (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    INTEGER NOT NULL REFERENCES docs(id) ON DELETE CASCADE,
    text      TEXT NOT NULL,
    embedding BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_doc_chunks ON doc_chunks(doc_id);

CREATE TABLE IF NOT EXISTS reminders (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    chat_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    due_at     TEXT NOT NULL,  -- UTC, 'YYYY-MM-DD HH:MM:SS'
    done       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(done, due_at);

CREATE TABLE IF NOT EXISTS usage (
    user_id INTEGER NOT NULL,
    day     TEXT NOT NULL,
    count   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, day)
);

-- Учитель языков: личный словарь с интервальным повторением (Leitner: box 0..5)
CREATE TABLE IF NOT EXISTS vocab (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lang        TEXT NOT NULL,
    word        TEXT NOT NULL,           -- как показывать: «der Hund»
    lemma       TEXT NOT NULL,           -- ключ уникальности: «hund»
    sort_key    TEXT NOT NULL,           -- алфавитный порядок с учётом языка
    translation TEXT NOT NULL,
    pos         TEXT NOT NULL DEFAULT '',
    grammar     TEXT NOT NULL DEFAULT '',
    examples    TEXT NOT NULL DEFAULT '[]',
    tip         TEXT NOT NULL DEFAULT '',
    box         INTEGER NOT NULL DEFAULT 0,
    due         TEXT NOT NULL,           -- локальная дата YYYY-MM-DD
    correct     INTEGER NOT NULL DEFAULT 0,
    wrong       INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    UNIQUE (user_id, lang, lemma)
);
CREATE INDEX IF NOT EXISTS idx_vocab_due ON vocab(user_id, due);

CREATE TABLE IF NOT EXISTS lang_levels (
    user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lang    TEXT NOT NULL,
    level   TEXT NOT NULL,
    PRIMARY KEY (user_id, lang)
);

-- Текущий язык, план ежедневного повторения и серия дней
CREATE TABLE IF NOT EXISTS lang_state (
    user_id     INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
    current     TEXT NOT NULL DEFAULT 'de',
    daily       INTEGER NOT NULL DEFAULT 1,
    plan_day    TEXT,
    plan_at     TEXT,                    -- UTC, когда прислать повторение
    plan_count  INTEGER NOT NULL DEFAULT 0,
    plan_words  TEXT,                    -- JSON: id слов, которые ушли в повторение
    plan_sent   INTEGER NOT NULL DEFAULT 0,
    streak      INTEGER NOT NULL DEFAULT 0,
    last_review TEXT
);

-- Пройденные уроки и тесты: чтобы не повторяться и предлагать следующее
CREATE TABLE IF NOT EXISTS lang_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    lang       TEXT NOT NULL,
    kind       TEXT NOT NULL,
    topic      TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Аквариумы владельца. Время задач — ISO с часовым поясом.
CREATE TABLE IF NOT EXISTS aq_tanks (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    name   TEXT NOT NULL,
    volume REAL NOT NULL
);
-- График ухода: что, в каком аквариуме, во сколько и по каким дням (0 = пн)
CREATE TABLE IF NOT EXISTS aq_plan (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tank_id INTEGER NOT NULL REFERENCES aq_tanks(id) ON DELETE CASCADE,
    title   TEXT NOT NULL,
    kind    TEXT NOT NULL DEFAULT 'other',
    time    TEXT NOT NULL,
    days    TEXT NOT NULL
);
-- Задачи по дням (task_id = «s<id пункта графика>» или тестовая)
CREATE TABLE IF NOT EXISTS aq_tasks (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    date              TEXT NOT NULL,
    task_id           TEXT NOT NULL,
    task_name         TEXT NOT NULL,
    tank_id           INTEGER,
    sent_at           TEXT NOT NULL,
    remind_at         TEXT,
    overdue_at        TEXT,
    reminded          INTEGER NOT NULL DEFAULT 0,
    overdue_notified  INTEGER NOT NULL DEFAULT 0,
    snooze_count      INTEGER NOT NULL DEFAULT 0,
    completed_at      TEXT,
    completed_by      INTEGER,
    completed_by_name TEXT,
    cant_at           TEXT,
    cant_reason       TEXT,
    cant_by_name      TEXT,
    UNIQUE (date, task_id)
);
CREATE TABLE IF NOT EXISTS aq_settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS aq_achievements (streak INTEGER PRIMARY KEY, achieved_at TEXT NOT NULL);

-- Что агент знает об аквариумах (tank_id NULL — общее для всех)
CREATE TABLE IF NOT EXISTS aq_facts (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    tank_id    INTEGER,
    topic      TEXT NOT NULL,
    text       TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS aq_questions (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER NOT NULL,
    tank_id   INTEGER,
    topic     TEXT NOT NULL,
    question  TEXT NOT NULL,
    tg_msg_id INTEGER,
    answered  INTEGER NOT NULL DEFAULT 0,  -- 1 ответил, -1 «не знаю» / пропустил
    asked_at  TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS aq_water (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    tank_id INTEGER,
    at      TEXT NOT NULL DEFAULT (datetime('now')),
    param   TEXT NOT NULL,
    value   REAL NOT NULL
);

-- Кухня: общий для чата список покупок и книга рецептов
CREATE TABLE IF NOT EXISTS shop_items (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    text       TEXT NOT NULL,
    qty        TEXT NOT NULL DEFAULT '',
    done       INTEGER NOT NULL DEFAULT 0,
    added_by   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_shop_items ON shop_items(chat_id);
CREATE TABLE IF NOT EXISTS recipes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id    INTEGER NOT NULL,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,
    added_by   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS allowed_chats (
    chat_id  INTEGER PRIMARY KEY,
    title    TEXT NOT NULL DEFAULT '',
    added_by INTEGER
);
"""

# Колонки, добавленные после первых версий: (таблица, колонка, определение)
MIGRATIONS = [
    ("users", "mode", "TEXT"),
    ("users", "persona", "TEXT"),
    ("users", "temperature", "REAL"),
    ("users", "length", "TEXT"),
    ("users", "voice_reply", "INTEGER NOT NULL DEFAULT 0"),
    ("users", "tz", "TEXT"),
    ("users", "intra_login", "TEXT"),
    ("users", "intra_notified", "TEXT"),
    # аквариумы: первая версия была с одним аквариумом
    ("aq_tasks", "tank_id", "INTEGER"),
    ("aq_facts", "tank_id", "INTEGER"),
    ("aq_questions", "tank_id", "INTEGER"),
    ("aq_questions", "tg_msg_id", "INTEGER"),
    ("aq_water", "tank_id", "INTEGER"),
]

USER_FIELDS = (
    "model", "mode", "persona", "temperature", "length", "voice_reply",
    "tz", "intra_login", "intra_notified",
)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"


def to_db_time(dt: datetime.datetime) -> str:
    """Любое aware-время -> строка UTC для БД."""
    return dt.astimezone(datetime.UTC).strftime(TS_FORMAT)


def from_db_time(value: str) -> datetime.datetime:
    return datetime.datetime.strptime(value, TS_FORMAT).replace(tzinfo=datetime.UTC)


@dataclass(frozen=True)
class User:
    id: int
    name: str = ""
    model: str | None = None
    mode: str | None = None
    persona: str | None = None
    temperature: float | None = None
    length: str | None = None
    voice_reply: bool = False
    tz: str | None = None
    intra_login: str | None = None
    intra_notified: str | None = None


@dataclass(frozen=True)
class StoredMessage:
    id: int
    role: str
    content: str
    tg_msg_id: int | None


@dataclass(frozen=True)
class Reminder:
    id: int
    user_id: int
    chat_id: int
    text: str
    due_at: datetime.datetime


@dataclass(frozen=True)
class ChatState:
    code_name: str | None
    files: dict[str, str]
    defense_code: str | None


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not connected")
        return self._conn

    async def connect(self) -> None:
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        await self._conn.execute("PRAGMA foreign_keys = ON")
        await self._conn.execute("PRAGMA journal_mode = WAL")
        await self._conn.executescript(SCHEMA)
        for table, column, definition in MIGRATIONS:
            if column not in await self._columns(table):
                await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
        await self._migrate_old_messages()
        await self._conn.commit()

    async def _columns(self, table: str) -> set[str]:
        async with self.conn.execute(f"PRAGMA table_info({table})") as cur:
            return {row[1] for row in await cur.fetchall()}

    async def _migrate_old_messages(self) -> None:
        """v1 хранила историю в таблице messages(user_id, ...) — переносим в chat_messages."""
        async with self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'messages'"
        ) as cur:
            if await cur.fetchone() is None:
                return
        await self.conn.execute(
            "INSERT INTO chat_messages (chat_id, user_id, role, content, created_at) "
            "SELECT user_id, CASE role WHEN 'user' THEN user_id END, role, content, created_at "
            "FROM messages ORDER BY id"
        )
        await self.conn.execute("DROP TABLE messages")

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def _fetchone(self, sql: str, params: tuple = ()) -> Any:
        async with self.conn.execute(sql, params) as cur:
            return await cur.fetchone()

    async def _fetchall(self, sql: str, params: tuple = ()) -> list[Any]:
        async with self.conn.execute(sql, params) as cur:
            return list(await cur.fetchall())

    async def _exec(self, sql: str, params: tuple = ()) -> aiosqlite.Cursor:
        cur = await self.conn.execute(sql, params)
        await self.conn.commit()
        return cur

    # ------------------------------------------------------------ пользователи

    async def add_user(self, user_id: int, name: str = "", added_by: int | None = None) -> bool:
        """Добавляет пользователя. Возвращает False, если он уже был."""
        cur = await self._exec(
            "INSERT OR IGNORE INTO users (id, name, added_by) VALUES (?, ?, ?)",
            (user_id, name, added_by),
        )
        return cur.rowcount > 0

    async def remove_user(self, user_id: int) -> bool:
        """Удаляет пользователя вместе с его данными (память, документы, личный чат)."""
        cur = await self.conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        removed = cur.rowcount > 0
        if removed:
            for table in ("chat_messages", "summaries", "chat_state"):
                await self.conn.execute(f"DELETE FROM {table} WHERE chat_id = ?", (user_id,))
            await self.conn.execute("DELETE FROM usage WHERE user_id = ?", (user_id,))
        await self.conn.commit()
        return removed

    async def is_user(self, user_id: int) -> bool:
        return await self._fetchone("SELECT 1 FROM users WHERE id = ?", (user_id,)) is not None

    async def get_user(self, user_id: int) -> User | None:
        cols = ", ".join(("id", "name", *USER_FIELDS))
        row = await self._fetchone(f"SELECT {cols} FROM users WHERE id = ?", (user_id,))
        if row is None:
            return None
        data = dict(zip(("id", "name", *USER_FIELDS), row, strict=True))
        data["voice_reply"] = bool(data["voice_reply"])
        return User(**data)

    async def list_users(self) -> list[User]:
        rows = await self._fetchall("SELECT id FROM users ORDER BY created_at, id")
        return [u for (uid,) in rows if (u := await self.get_user(uid)) is not None]

    async def set_user_field(self, user_id: int, field: str, value: Any) -> None:
        if field not in USER_FIELDS:
            raise ValueError(f"unknown user field {field}")
        await self._exec(f"UPDATE users SET {field} = ? WHERE id = ?", (value, user_id))

    async def get_model(self, user_id: int) -> str | None:
        user = await self.get_user(user_id)
        return user.model if user else None

    async def set_model(self, user_id: int, model: str | None) -> None:
        await self.set_user_field(user_id, "model", model)

    async def get_mode(self, user_id: int) -> str | None:
        user = await self.get_user(user_id)
        return user.mode if user else None

    async def set_mode(self, user_id: int, mode: str) -> None:
        await self.set_user_field(user_id, "mode", mode)

    # ------------------------------------------------------------ диалоги

    async def add_message(
        self,
        chat_id: int,
        role: str,
        content: str,
        *,
        user_id: int | None = None,
        tg_msg_id: int | None = None,
        model: str | None = None,
    ) -> int:
        cur = await self._exec(
            "INSERT INTO chat_messages (chat_id, user_id, role, content, tg_msg_id, model) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, user_id, role, content, tg_msg_id, model),
        )
        return int(cur.lastrowid)

    async def set_tg_msg_id(self, message_id: int, tg_msg_id: int) -> None:
        await self._exec("UPDATE chat_messages SET tg_msg_id = ? WHERE id = ?", (tg_msg_id, message_id))

    async def _summary_upto(self, chat_id: int) -> int:
        row = await self._fetchone("SELECT upto_id FROM summaries WHERE chat_id = ?", (chat_id,))
        return row[0] if row else 0

    async def get_history(self, chat_id: int, limit: int) -> list[dict[str, str]]:
        """Последние `limit` несвёрнутых сообщений в хронологическом порядке."""
        rows = await self._fetchall(
            "SELECT role, content FROM chat_messages WHERE chat_id = ? AND id > ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, await self._summary_upto(chat_id), limit),
        )
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    async def last_messages(self, chat_id: int, limit: int = 2) -> list[StoredMessage]:
        rows = await self._fetchall(
            "SELECT id, role, content, tg_msg_id FROM chat_messages WHERE chat_id = ? "
            "ORDER BY id DESC LIMIT ?",
            (chat_id, limit),
        )
        return [StoredMessage(*r) for r in reversed(rows)]

    async def message_by_tg_id(self, chat_id: int, tg_msg_id: int) -> StoredMessage | None:
        row = await self._fetchone(
            "SELECT id, role, content, tg_msg_id FROM chat_messages "
            "WHERE chat_id = ? AND tg_msg_id = ? ORDER BY id DESC LIMIT 1",
            (chat_id, tg_msg_id),
        )
        return StoredMessage(*row) if row else None

    async def delete_messages(self, ids: list[int]) -> None:
        await self.conn.executemany("DELETE FROM chat_messages WHERE id = ?", [(i,) for i in ids])
        await self.conn.commit()

    async def clear_history(self, chat_id: int) -> int:
        cur = await self.conn.execute("DELETE FROM chat_messages WHERE chat_id = ?", (chat_id,))
        await self.conn.execute("DELETE FROM summaries WHERE chat_id = ?", (chat_id,))
        await self.conn.commit()
        return cur.rowcount

    # ------------------------------------------------------------ резюме

    async def get_summary(self, chat_id: int) -> str | None:
        row = await self._fetchone("SELECT text FROM summaries WHERE chat_id = ?", (chat_id,))
        return row[0] if row else None

    async def messages_to_summarize(self, chat_id: int, keep: int) -> list[StoredMessage]:
        """Несвёрнутые сообщения, кроме последних `keep`."""
        upto = await self._summary_upto(chat_id)
        rows = await self._fetchall(
            "SELECT id, role, content, tg_msg_id FROM chat_messages WHERE chat_id = ? AND id > ? "
            "ORDER BY id",
            (chat_id, upto),
        )
        return [StoredMessage(*r) for r in rows[: max(len(rows) - keep, 0)]]

    async def set_summary(self, chat_id: int, text: str, upto_id: int) -> None:
        await self._exec(
            "INSERT INTO summaries (chat_id, text, upto_id) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET text = excluded.text, upto_id = excluded.upto_id",
            (chat_id, text, upto_id),
        )

    # ------------------------------------------------------------ состояние чата (код, защита)

    async def get_state(self, chat_id: int) -> ChatState:
        row = await self._fetchone(
            "SELECT code_name, files, defense_code FROM chat_state WHERE chat_id = ?", (chat_id,)
        )
        if row is None:
            return ChatState(None, {}, None)
        return ChatState(row[0], json.loads(row[1]) if row[1] else {}, row[2])

    async def set_code(self, chat_id: int, name: str, files: dict[str, str]) -> None:
        await self._exec(
            "INSERT INTO chat_state (chat_id, code_name, files) VALUES (?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET code_name = excluded.code_name, files = excluded.files",
            (chat_id, name, json.dumps(files, ensure_ascii=False)),
        )

    async def set_defense(self, chat_id: int, code: str | None) -> None:
        await self._exec(
            "INSERT INTO chat_state (chat_id, defense_code) VALUES (?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET defense_code = excluded.defense_code",
            (chat_id, code),
        )

    # ------------------------------------------------------------ напоминания

    async def add_reminder(
        self, user_id: int, chat_id: int, text: str, due_at: datetime.datetime
    ) -> int:
        cur = await self._exec(
            "INSERT INTO reminders (user_id, chat_id, text, due_at) VALUES (?, ?, ?, ?)",
            (user_id, chat_id, text, to_db_time(due_at)),
        )
        return int(cur.lastrowid)

    async def due_reminders(self, now: datetime.datetime) -> list[Reminder]:
        rows = await self._fetchall(
            "SELECT id, user_id, chat_id, text, due_at FROM reminders "
            "WHERE done = 0 AND due_at <= ? ORDER BY due_at",
            (to_db_time(now),),
        )
        return [Reminder(r[0], r[1], r[2], r[3], from_db_time(r[4])) for r in rows]

    async def list_reminders(self, user_id: int) -> list[Reminder]:
        rows = await self._fetchall(
            "SELECT id, user_id, chat_id, text, due_at FROM reminders "
            "WHERE done = 0 AND user_id = ? ORDER BY due_at",
            (user_id,),
        )
        return [Reminder(r[0], r[1], r[2], r[3], from_db_time(r[4])) for r in rows]

    async def complete_reminder(self, reminder_id: int) -> None:
        await self._exec("UPDATE reminders SET done = 1 WHERE id = ?", (reminder_id,))

    async def delete_reminder(self, user_id: int, reminder_id: int) -> bool:
        cur = await self._exec(
            "DELETE FROM reminders WHERE id = ? AND user_id = ? AND done = 0", (reminder_id, user_id)
        )
        return cur.rowcount > 0

    # ------------------------------------------------------------ лимиты

    async def increment_usage(self, user_id: int, day: str) -> int:
        await self._exec(
            "INSERT INTO usage (user_id, day, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, day) DO UPDATE SET count = count + 1",
            (user_id, day),
        )
        return await self.get_usage(user_id, day)

    async def get_usage(self, user_id: int, day: str) -> int:
        row = await self._fetchone("SELECT count FROM usage WHERE user_id = ? AND day = ?", (user_id, day))
        return row[0] if row else 0

    async def usage_for_day(self, day: str) -> dict[int, int]:
        rows = await self._fetchall("SELECT user_id, count FROM usage WHERE day = ?", (day,))
        return {uid: count for uid, count in rows}

    # ------------------------------------------------------------ группы

    async def allow_chat(self, chat_id: int, title: str, added_by: int) -> None:
        await self._exec(
            "INSERT OR REPLACE INTO allowed_chats (chat_id, title, added_by) VALUES (?, ?, ?)",
            (chat_id, title, added_by),
        )

    async def disallow_chat(self, chat_id: int) -> bool:
        cur = await self._exec("DELETE FROM allowed_chats WHERE chat_id = ?", (chat_id,))
        return cur.rowcount > 0

    async def is_chat_allowed(self, chat_id: int) -> bool:
        return await self._fetchone("SELECT 1 FROM allowed_chats WHERE chat_id = ?", (chat_id,)) is not None

    async def stats(self) -> dict[str, int]:
        result = {}
        for table in ("users", "chat_messages", "memories", "docs", "kb_chunks", "allowed_chats"):
            (result[table],) = await self._fetchone(f"SELECT COUNT(*) FROM {table}")
        (result["reminders"],) = await self._fetchone("SELECT COUNT(*) FROM reminders WHERE done = 0")
        return result

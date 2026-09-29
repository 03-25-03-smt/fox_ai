import pytest

from bot.db import Database
from bot.knowledge import KnowledgeBase, chunk_text
from bot.memory import MemoryStore, _parse_facts

from .fakes import FakeLLM


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "m.sqlite3"))
    await database.connect()
    await database.add_user(1)
    await database.add_user(2)
    yield database
    await database.close()


async def test_memory_search_relevance(db):
    mem = MemoryStore(db, FakeLLM(), "fake")
    await mem.add(1, "Пользователь любит итальянскую пасту")
    await mem.add(1, "Пользователь делает проект minishell")
    found = await mem.search(1, "как дела с minishell проект", k=1, min_score=0.1)
    assert found == ["Пользователь делает проект minishell"]
    assert await mem.search(2, "minishell", k=5, min_score=0.0) == []


async def test_memory_dedup_and_delete_scoped_to_user(db):
    mem = MemoryStore(db, FakeLLM(), "fake")
    mid = await mem.add(1, "Пользователь живёт в Париже")
    assert await mem.add(1, "Пользователь живёт в Париже") is None
    assert not await mem.delete(2, mid)  # чужой факт удалить нельзя
    assert await mem.delete(1, mid)


def test_parse_facts():
    assert _parse_facts('{"facts": ["a", " ", 3, "b"]}') == ["a", "b"]
    assert _parse_facts('["x"]') == ["x"]
    assert _parse_facts("не json") == []


def test_chunk_text():
    text = "\n\n".join(f"Абзац {i} " + "слово " * 30 for i in range(40))
    chunks = chunk_text(text, size=500, overlap=50)
    assert len(chunks) > 5
    assert all(len(c) <= 500 + 50 + 2 for c in chunks)
    assert "Абзац 0" in chunks[0] and "Абзац 39" in chunks[-1]
    long = chunk_text("x" * 2500, size=1000, overlap=100)
    assert len(long) == 3


async def test_knowledge_incremental_reindex(db, tmp_path):
    root = tmp_path / "kb"
    root.mkdir()
    (root / "norm.md").write_text("Функция не длиннее 25 строк")
    (root / "ignored.bin").write_bytes(b"\x00")
    kb = KnowledgeBase(db, FakeLLM(), "fake", str(root))

    stats = await kb.reindex()
    assert (stats.indexed, stats.skipped) == (1, 0)
    assert (await kb.reindex()).skipped == 1  # без изменений — не переиндексируем

    (root / "norm.md").write_text("Функция не длиннее 25 строк, максимум 4 параметра")
    assert (await kb.reindex()).indexed == 1
    [passage] = await kb.search("максимум 4 параметра", k=3, min_score=0.1)
    assert passage.source == "norm.md" and "4 параметра" in passage.text

    (root / "norm.md").unlink()
    assert (await kb.reindex()).removed == 1
    assert await kb.stats() == (0, 0)

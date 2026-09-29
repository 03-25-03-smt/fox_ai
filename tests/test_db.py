import pytest

from bot.db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.sqlite3"))
    await database.connect()
    yield database
    await database.close()


async def test_users_crud(db):
    assert not await db.is_user(1)
    assert await db.add_user(1, "Alice", added_by=99)
    assert not await db.add_user(1, "Alice again")
    assert await db.is_user(1)
    assert [u.name for u in await db.list_users()] == ["Alice"]
    assert await db.remove_user(1)
    assert not await db.remove_user(1)
    assert not await db.is_user(1)


async def test_model_per_user(db):
    await db.add_user(1)
    await db.add_user(2)
    assert await db.get_model(1) is None
    await db.set_model(1, "qwen2.5-coder:14b")
    assert await db.get_model(1) == "qwen2.5-coder:14b"
    assert await db.get_model(2) is None


async def test_history_limit_order_and_isolation(db):
    await db.add_user(1)
    await db.add_user(2)
    for i in range(5):
        await db.add_message(1, "user", f"q{i}")
        await db.add_message(1, "assistant", f"a{i}")
    await db.add_message(2, "user", "other")

    history = await db.get_history(1, 4)
    assert history == [
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
        {"role": "user", "content": "q4"},
        {"role": "assistant", "content": "a4"},
    ]
    assert await db.clear_history(1) == 10
    assert await db.get_history(1, 10) == []
    assert len(await db.get_history(2, 10)) == 1


async def test_remove_user_deletes_history(db):
    await db.add_user(1)
    await db.add_message(1, "user", "hi")
    await db.remove_user(1)
    await db.add_user(1)
    assert await db.get_history(1, 10) == []

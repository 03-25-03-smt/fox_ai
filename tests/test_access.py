from unittest.mock import AsyncMock

import pytest
from aiogram.types import Message

from bot.access import AccessMiddleware
from bot.db import Database


class FakeUser:
    def __init__(self, uid: int):
        self.id = uid
        self.is_bot = False
        self.full_name = f"user{uid}"


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "t.sqlite3"))
    await database.connect()
    yield database
    await database.close()


def fake_message() -> Message:
    msg = AsyncMock(spec=Message)
    msg.answer = AsyncMock()
    return msg


async def test_admin_passes_and_is_registered(db):
    mw = AccessMiddleware(db, frozenset({10}))
    handler = AsyncMock(return_value="ok")
    data = {"event_from_user": FakeUser(10)}
    assert await mw(handler, fake_message(), data) == "ok"
    assert data["is_admin"] is True
    assert await db.is_user(10)


async def test_added_user_passes(db):
    await db.add_user(20)
    mw = AccessMiddleware(db, frozenset({10}))
    handler = AsyncMock(return_value="ok")
    data = {"event_from_user": FakeUser(20)}
    assert await mw(handler, fake_message(), data) == "ok"
    assert data["is_admin"] is False


async def test_stranger_blocked_and_gets_id(db):
    mw = AccessMiddleware(db, frozenset({10}))
    handler = AsyncMock()
    msg = fake_message()
    assert await mw(handler, msg, {"event_from_user": FakeUser(30)}) is None
    handler.assert_not_called()
    assert "30" in msg.answer.call_args.args[0]
    assert not await db.is_user(30)

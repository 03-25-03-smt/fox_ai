"""Сквозная тестовая среда: апдейты Telegram -> Dispatcher -> хендлеры, без сети и GPU."""

import asyncio
import datetime
import itertools
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    EditMessageText,
    GetFile,
    SendDocument,
    SendMessage,
    SendPhoto,
    SendVoice,
    TelegramMethod,
)
from aiogram.types import (
    CallbackQuery,
    Chat,
    Document,
    File,
    Message,
    PhotoSize,
    Update,
    User,
    Voice,
)

from bot.app import App, GpuQueue
from bot.assistant import Assistant
from bot.config import Settings
from bot.db import Database
from bot.docs import PersonalDocs
from bot.handlers import build_router
from bot.knowledge import KnowledgeBase
from bot.memory import MemoryStore

from .fakes import FakeImages, FakeLLM, FakeSandbox, FakeSpeech, FakeWeb, FakeYouTube

ADMIN, FRIEND, STRANGER = 10, 20, 30
GROUP = -100500
BOT_USERNAME = "fox_ai_bot"

SENDS = (SendMessage, EditMessageText, SendPhoto, SendVoice, SendDocument)


class FakeSession(BaseSession):
    """Записывает запросы к Telegram API вместо отправки."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.files: dict[str, bytes] = {}
        self._ids = itertools.count(1000)

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, SENDS):
            text = getattr(method, "text", None) or getattr(method, "caption", None)
            chat_id = method.chat_id or 0
            return Message(
                message_id=getattr(method, "message_id", None) or next(self._ids),
                date=datetime.datetime.now(),
                chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup"),
                from_user=User(id=1, is_bot=True, first_name="Fox", username=BOT_USERNAME),
                text=text,
            ).as_(bot)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path=method.file_id)
        return True

    async def stream_content(self, url, *args, **kwargs) -> AsyncIterator[bytes]:
        yield self.files[url.rsplit("/", 1)[-1]]

    async def close(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [
            (getattr(r, "text", None) or getattr(r, "caption", None) or "")
            for r in self.requests if isinstance(r, SENDS)
        ]

    def of_type(self, cls) -> list:
        return [r for r in self.requests if isinstance(r, cls)]


class Env:
    def __init__(self, tmp_path) -> None:
        self.tmp_path = tmp_path
        self.counter = itertools.count(1)

    async def setup(self, **overrides) -> "Env":
        self.db = Database(str(self.tmp_path / "e2e.sqlite3"))
        await self.db.connect()
        await self.db.add_user(FRIEND, "Friend")
        self.kb_dir = self.tmp_path / "knowledge"
        self.kb_dir.mkdir(exist_ok=True)
        (self.kb_dir / "norm.md").write_text("Функция не больше 25 строк. Запрещён for и switch.")
        params = dict(
            bot_token="1:x", admin_ids=str(ADMIN), default_model="qwen2.5:7b",
            knowledge_dir=str(self.kb_dir), memory_auto=True, searxng_url="http://searx",
            memory_min_score=0.1, knowledge_min_score=0.1, docs_min_score=0.1,
            backup_dir=str(self.tmp_path / "backups"), timezone="Europe/Paris",
        )
        params.update(overrides)
        self.settings = Settings(**params)
        self.llm = FakeLLM()
        self.web = FakeWeb()
        self.memory = MemoryStore(self.db, self.llm, self.settings.embed_model)
        knowledge = KnowledgeBase(self.db, self.llm, self.settings.embed_model, self.settings.knowledge_dir)
        await knowledge.reindex()
        self.assistant = Assistant(
            self.settings, self.llm, self.db, self.memory, knowledge, self.web,
            PersonalDocs(self.db, self.llm, self.settings.embed_model),
        )
        self.sandbox, self.speech, self.images = FakeSandbox(), FakeSpeech(), FakeImages()
        self.assistant.sandbox = self.sandbox
        self.app = App(
            settings=self.settings, db=self.db, assistant=self.assistant,
            sandbox=self.sandbox, speech=self.speech, imagegen=self.images,
            queue=GpuQueue(self.settings.max_concurrent), bot_username=BOT_USERNAME,
            youtube=FakeYouTube(),
        )
        self.youtube = self.app.youtube
        self.session = FakeSession()
        self.bot = Bot("123:fake", session=self.session)
        self.dp = Dispatcher(app=self.app)
        self.dp.include_router(build_router(self.app))
        return self

    def message(self, uid: int, chat_id: int | None = None, **kwargs) -> Message:
        chat_id = chat_id or uid
        return Message(
            message_id=next(self.counter),
            date=datetime.datetime.now(),
            chat=Chat(id=chat_id, type="private" if chat_id > 0 else "supergroup", title="g"),
            from_user=User(id=uid, is_bot=False, first_name=f"u{uid}"),
            **kwargs,
        )

    async def feed(self, **update) -> None:
        await self.dp.feed_update(self.bot, Update(update_id=next(self.counter), **update))
        await self.drain()

    async def drain(self) -> None:
        while self.app.background:
            await asyncio.gather(*list(self.app.background))

    async def send(self, uid: int, text: str, chat_id: int | None = None, **kwargs) -> None:
        await self.feed(message=self.message(uid, chat_id, text=text, **kwargs))

    async def send_file(self, uid: int, name: str, content: bytes, caption: str | None = None,
                        reply_to: Message | None = None) -> Message:
        self.session.files[name] = content
        doc = Document(file_id=name, file_unique_id=name, file_name=name, file_size=len(content))
        msg = self.message(uid, document=doc, caption=caption, reply_to_message=reply_to)
        await self.feed(message=msg)
        return msg

    async def send_voice(self, uid: int, audio: bytes = b"ogg") -> None:
        self.session.files["voice1"] = audio
        voice = Voice(file_id="voice1", file_unique_id="v", duration=2, file_size=len(audio))
        await self.feed(message=self.message(uid, voice=voice))

    async def send_photo(self, uid: int, caption: str | None = None) -> None:
        self.session.files["photo1"] = b"\xff\xd8jpeg"
        photo = PhotoSize(file_id="photo1", file_unique_id="p", width=10, height=10, file_size=6)
        await self.feed(message=self.message(uid, photo=[photo], caption=caption))

    async def click(self, uid: int, data: str, message_id: int | None = None, chat_id: int | None = None) -> None:
        cb = CallbackQuery(
            id=str(next(self.counter)), from_user=User(id=uid, is_bot=False, first_name="u"),
            chat_instance="x", data=data,
            message=Message(
                message_id=message_id or next(self.counter), date=datetime.datetime.now(),
                chat=Chat(id=chat_id or uid, type="private"), text="menu",
            ),
        )
        await self.feed(callback_query=cb)

    def texts(self) -> list[str]:
        return self.session.texts()

    def last_text(self) -> str:
        return self.texts()[-1]

    def system_prompt(self, call_index: int = -1) -> str:
        return self.llm.calls[call_index]["messages"][0]["content"]

    def last_user_prompt(self) -> str:
        return self.llm.calls[-1]["messages"][-1]["content"]


@pytest.fixture
async def env(tmp_path):
    e = await Env(tmp_path).setup()
    yield e
    await e.db.close()


@pytest.fixture
async def make_env(tmp_path):
    envs = []

    async def factory(**overrides):
        e = await Env(tmp_path).setup(**overrides)
        envs.append(e)
        return e

    yield factory
    for e in envs:
        await e.db.close()

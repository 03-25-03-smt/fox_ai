"""Сквозные тесты: апдейт от Telegram -> Dispatcher -> хендлеры, без сети и GPU."""

import asyncio
import datetime
from collections.abc import AsyncIterator

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.methods import EditMessageText, GetFile, SendMessage, TelegramMethod
from aiogram.types import CallbackQuery, Chat, Document, File, Message, Update, User

from bot import handlers
from bot.assistant import Assistant
from bot.config import Settings
from bot.db import Database
from bot.handlers import build_router
from bot.knowledge import KnowledgeBase
from bot.memory import MemoryStore

from .fakes import FakeLLM, FakeWeb

ADMIN, FRIEND, STRANGER = 10, 20, 30

BAD_C = '#include <unistd.h>\nint main(){ for(int i=0;i<3;i++) write(1,"a",1); return 0; }\n'


class FakeSession(BaseSession):
    """Записывает запросы к Telegram API вместо отправки."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[TelegramMethod] = []
        self.files: dict[str, bytes] = {}

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        if isinstance(method, (SendMessage, EditMessageText)):
            return Message(
                message_id=len(self.requests),
                date=datetime.datetime.now(),
                chat=Chat(id=method.chat_id or 0, type="private"),
                text=method.text,
            ).as_(bot)
        if isinstance(method, GetFile):
            return File(file_id=method.file_id, file_unique_id="u", file_path=method.file_id)
        return True

    async def stream_content(self, url, *args, **kwargs) -> AsyncIterator[bytes]:
        yield self.files[url.rsplit("/", 1)[-1]]

    async def close(self) -> None:
        pass

    def texts(self) -> list[str]:
        return [r.text for r in self.requests if isinstance(r, (SendMessage, EditMessageText))]


@pytest.fixture
async def env(tmp_path):
    db = Database(str(tmp_path / "e2e.sqlite3"))
    await db.connect()
    await db.add_user(FRIEND, "Friend")
    kb_dir = tmp_path / "knowledge"
    kb_dir.mkdir()
    (kb_dir / "norm.md").write_text("Функция не больше 25 строк. Запрещён for и switch.")
    settings = Settings(
        bot_token="1:x", admin_ids=str(ADMIN), default_model="qwen2.5:7b",
        knowledge_dir=str(kb_dir), memory_auto=True, searxng_url="http://searx",
        # у фейковых bag-of-words эмбеддингов близость ниже, чем у настоящих
        memory_min_score=0.1, knowledge_min_score=0.1,
    )
    llm = FakeLLM()
    memory = MemoryStore(db, llm, settings.embed_model)
    knowledge = KnowledgeBase(db, llm, settings.embed_model, settings.knowledge_dir)
    await knowledge.reindex()
    web = FakeWeb()
    assistant = Assistant(settings, llm, db, memory, knowledge, web)
    session = FakeSession()
    bot = Bot("123:fake", session=session)
    dp = Dispatcher(db=db, assistant=assistant, settings=settings)
    dp.include_router(build_router(db, settings.admins))

    counter = iter(range(1, 10_000))

    def base_message(uid: int, **kwargs) -> Message:
        return Message(
            message_id=next(counter),
            date=datetime.datetime.now(),
            chat=Chat(id=uid, type="private"),
            from_user=User(id=uid, is_bot=False, first_name=f"u{uid}"),
            **kwargs,
        )

    async def send(uid: int, text: str) -> None:
        await dp.feed_update(bot, Update(update_id=next(counter), message=base_message(uid, text=text)))
        await _drain_background()

    async def send_file(uid: int, name: str, content: bytes, caption: str | None = None) -> None:
        session.files[name] = content
        doc = Document(file_id=name, file_unique_id=name, file_name=name, file_size=len(content))
        msg = base_message(uid, document=doc, caption=caption)
        await dp.feed_update(bot, Update(update_id=next(counter), message=msg))
        await _drain_background()

    async def click(uid: int, data: str) -> None:
        cb = CallbackQuery(
            id=str(next(counter)), from_user=User(id=uid, is_bot=False, first_name="u"),
            chat_instance="x", data=data, message=base_message(uid, text="menu"),
        )
        await dp.feed_update(bot, Update(update_id=next(counter), callback_query=cb))

    yield {"send": send, "send_file": send_file, "click": click, "session": session,
           "db": db, "llm": llm, "web": web, "memory": memory, "kb_dir": kb_dir}
    await db.close()


async def _drain_background() -> None:
    while handlers._background:
        await asyncio.gather(*list(handlers._background))


def system_prompt(call: dict) -> str:
    return call["messages"][0]["content"]


async def test_stranger_is_blocked(env):
    await env["send"](STRANGER, "привет")
    text = env["session"].texts()[0]
    assert "Доступ закрыт" in text and str(STRANGER) in text
    assert env["llm"].calls == []


async def test_friend_chat_saves_history_and_formats_html(env):
    await env["send"](FRIEND, "напиши main")
    call = env["llm"].calls[0]
    assert call["model"] == "qwen2.5:7b"
    assert call["messages"][-1] == {"role": "user", "content": "напиши main"}

    final = env["session"].requests[-1]
    assert isinstance(final, EditMessageText) and final.parse_mode == "HTML"
    assert '<pre><code class="language-c">' in final.text

    history = await env["db"].get_history(FRIEND, 10)
    assert [m["role"] for m in history] == ["user", "assistant"]

    await env["send"](FRIEND, "а теперь с argc")
    assert len(env["llm"].calls[1]["messages"]) == 1 + 2 + 1


async def test_reset_clears_history(env):
    await env["send"](FRIEND, "привет")
    await env["send"](FRIEND, "/reset")
    assert await env["db"].get_history(FRIEND, 10) == []


async def test_admin_commands_only_for_admin(env):
    db = env["db"]
    await env["send"](FRIEND, "/adduser 555")
    assert not await db.is_user(555)
    assert "Не знаю такую команду" in env["session"].texts()[-1]

    await env["send"](ADMIN, "/adduser 555 Вася")
    assert await db.is_user(555)
    await env["send"](ADMIN, "/deluser 555")
    assert not await db.is_user(555)


async def test_model_keyboard_hides_embedding_model(env):
    await env["send"](FRIEND, "/model")
    markup = env["session"].requests[-1].reply_markup
    labels = [row[0].text for row in markup.inline_keyboard]
    assert labels == ["llama3.1:8b", "✅ qwen2.5:7b"]

    await env["click"](FRIEND, "m:llama3.1:8b")
    assert await env["db"].get_model(FRIEND) == "llama3.1:8b"


async def test_modes_switch_prompt_and_knowledge(env):
    # режим chat: базы знаний в промпте нет
    await env["send"](FRIEND, "сколько строк можно в функции")
    assert "базы знаний" not in system_prompt(env["llm"].calls[-1])

    await env["click"](FRIEND, "mode:code42")
    assert await env["db"].get_mode(FRIEND) == "code42"

    await env["send"](FRIEND, "сколько строк можно в функции")
    prompt = system_prompt(env["llm"].calls[-1])
    assert "наставник школы 42" in prompt
    assert "[norm.md]" in prompt and "25 строк" in prompt


async def test_memory_commands_and_injection(env):
    await env["send"](FRIEND, "/remember Пользователь любит борщ со сметаной")
    assert "Запомнил" in env["session"].texts()[-1]
    await env["send"](FRIEND, "/remember Пользователь любит борщ со сметаной")
    assert "уже знаю" in env["session"].texts()[-1]

    await env["send"](FRIEND, "что приготовить борщ или суп")
    assert "любит борщ" in system_prompt(env["llm"].calls[-1])

    items = await env["memory"].list_facts(FRIEND)
    await env["send"](FRIEND, f"/forget {items[0][0]}")
    assert await env["memory"].list_facts(FRIEND) == []


async def test_memory_is_isolated_between_users(env):
    await env["db"].add_user(ADMIN)
    await env["memory"].add(ADMIN, "Пользователь любит борщ")
    await env["send"](FRIEND, "борщ борщ борщ")
    assert "любит борщ" not in system_prompt(env["llm"].calls[-1])


async def test_auto_memory_extraction(env):
    env["llm"].facts_json = '{"facts": ["Пользователь учится в 42 Paris"]}'
    await env["send"](FRIEND, "Привет! Я учусь в 42 Paris и делаю minishell")
    assert [t for _, t in await env["memory"].list_facts(FRIEND)] == ["Пользователь учится в 42 Paris"]


async def test_norminette_document_flow(env):
    await env["send_file"](FRIEND, "main.c", BAD_C.encode())
    texts = env["session"].texts()
    assert any("ошибок norminette" in t and "FORBIDDEN_CS" in t for t in texts)
    # затем модель объясняет ошибки
    last_user_msg = env["llm"].calls[-1]["messages"][-1]["content"]
    assert "norminette" in last_user_msg and "FORBIDDEN_CS" in last_user_msg


async def test_norm_command_ok_code(env):
    code = (
        "/* ************************************************************************** */\n"
        "/*                                                                            */\n"
        "/*                                                        :::      ::::::::   */\n"
        "/*   ft_one.c                                           :+:      :+:    :+:   */\n"
        "/*                                                    +:+ +:+         +:+     */\n"
        "/*   By: fox <fox@student.42.fr>                    +#+  +:+       +#+        */\n"
        "/*                                                +#+#+#+#+#+   +#+           */\n"
        "/*   Created: 2026/09/29 10:00:00 by fox               #+#    #+#             */\n"
        "/*   Updated: 2026/09/29 10:00:00 by fox              ###   ########.fr       */\n"
        "/*                                                                            */\n"
        "/* ************************************************************************** */\n"
        "\n"
        "int\tft_one(void)\n"
        "{\n"
        "\treturn (1);\n"
        "}\n"
    )
    await env["send"](FRIEND, f"/norm\n```c\n{code}```")
    assert "Norm OK" in env["session"].texts()[-1]
    assert env["llm"].calls == []


async def test_web_tool_loop(env):
    env["llm"].tool_script = [
        [{"function": {"name": "web_search", "arguments": {"query": "ollama latest"}}}],
        [{"function": {"name": "fetch_url", "arguments": {"url": "https://example.com/ollama"}}}],
    ]
    env["llm"].reply = "Последняя версия — 0.34"
    await env["send"](FRIEND, "какая последняя версия ollama?")

    assert env["web"].queries == ["ollama latest"]
    assert env["web"].fetched == ["https://example.com/ollama"]
    texts = env["session"].texts()
    assert any("🔎 Ищу: ollama latest" in t for t in texts)
    assert texts[-1].startswith("Последняя версия")
    tool_msgs = [m for m in env["llm"].calls[-1]["messages"] if m["role"] == "tool"]
    assert "Ollama 0.34 released" in tool_msgs[0]["content"]
    assert "Page body text" in tool_msgs[1]["content"]


async def test_model_without_tools_falls_back(env):
    env["llm"].supports_tools = False
    await env["send"](FRIEND, "привет")
    assert env["llm"].calls[-1]["tools"] is None
    assert "Ответ" in env["session"].texts()[-1]


async def test_search_command(env):
    await env["send"](FRIEND, "/search цена rtx 3070")
    assert env["web"].queries == ["цена rtx 3070"]
    assert "Результаты поиска" in env["llm"].calls[-1]["messages"][-1]["content"]


async def test_kb_upload_admin_only(env):
    await env["send_file"](FRIEND, "subject.md", b"minishell subject", caption="/kb")
    assert "только админ" in env["session"].texts()[-1]

    await env["send_file"](ADMIN, "subject.md", b"# Minishell\nImplement a shell.", caption="/kb")
    assert (env["kb_dir"] / "uploads" / "subject.md").exists()
    assert "База знаний: 2 файлов" in env["session"].texts()[-1]

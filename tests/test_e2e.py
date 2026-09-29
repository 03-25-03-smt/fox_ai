"""Сквозные тесты базовых сценариев (доступ, диалог, режимы, память, norminette, веб)."""

from aiogram.methods import EditMessageText

from .conftest import ADMIN, FRIEND, STRANGER

BAD_C = '#include <unistd.h>\nint main(){ for(int i=0;i<3;i++) write(1,"a",1); return 0; }\n'

OK_C = (
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


async def test_stranger_is_blocked(env):
    await env.send(STRANGER, "привет")
    assert "Доступ закрыт" in env.texts()[0] and str(STRANGER) in env.texts()[0]
    assert env.llm.calls == []


async def test_friend_chat_saves_history_and_formats_html(env):
    await env.send(FRIEND, "расскажи что-нибудь интересное про лис и их повадки")
    call = env.llm.calls[0]
    assert call["model"] == "qwen2.5:7b"  # автовыбор: обычный вопрос -> основная модель

    final = [r for r in env.session.requests if isinstance(r, EditMessageText)][-1]
    assert final.parse_mode == "HTML"
    assert '<pre><code class="language-c">' in final.text
    buttons = [b.text for b in final.reply_markup.inline_keyboard[0]]
    assert buttons == ["🔄", "📖 Подробнее", "🔀 Другая модель", "🔊"]

    history = await env.db.get_history(FRIEND, 10)
    assert [m["role"] for m in history] == ["user", "assistant"]

    await env.send(FRIEND, "а теперь подробнее")
    assert len(env.llm.calls[1]["messages"]) == 1 + 2 + 1


async def test_reset_clears_history(env):
    await env.send(FRIEND, "привет")
    await env.send(FRIEND, "/reset")
    assert await env.db.get_history(FRIEND, 10) == []


async def test_admin_commands_only_for_admin(env):
    await env.send(FRIEND, "/adduser 555")
    assert not await env.db.is_user(555)
    assert "Не знаю такую команду" in env.last_text()

    await env.send(ADMIN, "/adduser 555 Вася")
    assert await env.db.is_user(555)
    await env.send(ADMIN, "/deluser 555")
    assert not await env.db.is_user(555)


async def test_model_keyboard_and_auto(env):
    await env.send(FRIEND, "/model")
    markup = env.session.requests[-1].reply_markup
    labels = [row[0].text for row in markup.inline_keyboard]
    assert labels[0] == "✅ 🤖 Авто (по запросу)"
    assert "bge-m3:latest" not in labels and "llama3.1:8b" in labels

    await env.click(FRIEND, "m:llama3.1:8b")
    assert await env.db.get_model(FRIEND) == "llama3.1:8b"
    await env.send(FRIEND, "привет")
    assert env.llm.calls[-1]["model"] == "llama3.1:8b"

    await env.click(FRIEND, "m:__auto__")
    assert await env.db.get_model(FRIEND) is None


async def test_modes_switch_prompt_and_knowledge(env):
    await env.send(FRIEND, "сколько строк можно в функции")
    assert "базы знаний" not in env.system_prompt()

    await env.click(FRIEND, "mode:code42")
    assert await env.db.get_mode(FRIEND) == "code42"

    await env.send(FRIEND, "сколько строк можно в функции")
    prompt = env.system_prompt()
    assert "наставник школы 42" in prompt
    assert "[norm.md]" in prompt and "25 строк" in prompt
    assert env.llm.calls[-1]["model"] == "qwen2.5-coder:7b"  # режим 42 -> coder-модель


async def test_memory_commands_and_injection(env):
    await env.send(FRIEND, "/remember Пользователь любит борщ со сметаной")
    assert "Запомнил" in env.last_text()
    await env.send(FRIEND, "/remember Пользователь любит борщ со сметаной")
    assert "уже знаю" in env.last_text()

    await env.send(FRIEND, "что приготовить борщ или суп")
    assert "любит борщ" in env.system_prompt()

    items = await env.memory.list_facts(FRIEND)
    await env.send(FRIEND, f"/forget {items[0][0]}")
    assert await env.memory.list_facts(FRIEND) == []


async def test_memory_is_isolated_between_users(env):
    await env.db.add_user(ADMIN)
    await env.memory.add(ADMIN, "Пользователь любит борщ")
    await env.send(FRIEND, "борщ борщ борщ")
    assert "любит борщ" not in env.system_prompt()


async def test_auto_memory_extraction(env):
    env.llm.facts_json = '{"facts": ["Пользователь учится в 42 Paris"]}'
    await env.send(FRIEND, "Привет! Я учусь в 42 Paris и делаю minishell")
    assert [t for _, t in await env.memory.list_facts(FRIEND)] == ["Пользователь учится в 42 Paris"]


async def test_norminette_document_flow(env):
    await env.send_file(FRIEND, "main.c", BAD_C.encode())
    assert any("ошибок norminette" in t and "FORBIDDEN_CS" in t for t in env.texts())
    assert "FORBIDDEN_CS" in env.last_user_prompt()
    state = await env.db.get_state(FRIEND)
    assert state.files == {"main.c": BAD_C}  # код запомнен для /run, /tests, /defense


async def test_norm_command_ok_code(env):
    await env.send(FRIEND, f"/norm\n```c\n{OK_C}```")
    assert "Norm OK" in env.last_text()
    assert env.llm.calls == []


async def test_web_tool_loop(env):
    env.llm.tool_script = [
        [{"function": {"name": "web_search", "arguments": {"query": "ollama latest"}}}],
        [{"function": {"name": "fetch_url", "arguments": {"url": "https://example.com/ollama"}}}],
    ]
    env.llm.reply = "Последняя версия — 0.34"
    await env.send(FRIEND, "какая последняя версия ollama?")

    assert env.web.queries == ["ollama latest"]
    assert env.web.fetched == ["https://example.com/ollama"]
    assert any("🔎 Ищу: ollama latest" in t for t in env.texts())
    assert env.last_text().startswith("Последняя версия")
    tool_msgs = [m for m in env.llm.calls[-1]["messages"] if m["role"] == "tool"]
    assert "Ollama 0.34 released" in tool_msgs[0]["content"]


async def test_model_without_tools_falls_back(env):
    env.llm.supports_tools = False
    await env.send(FRIEND, "привет")
    assert env.llm.calls[-1]["tools"] is None
    assert "Ответ" in env.last_text()


async def test_search_command(env):
    await env.send(FRIEND, "/search цена rtx 3070")
    assert env.web.queries == ["цена rtx 3070"]
    assert "Результаты поиска" in env.last_user_prompt()


async def test_kb_upload_admin_only(env):
    await env.send_file(FRIEND, "subject.md", b"minishell subject", caption="/kb")
    assert "только админ" in env.last_text()

    await env.send_file(ADMIN, "subject.md", b"# Minishell\nImplement a shell.", caption="/kb")
    assert (env.kb_dir / "uploads" / "subject.md").exists()
    assert "База знаний: 2 файлов" in env.last_text()

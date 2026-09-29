"""Сквозные тесты новых функций: песочница, проекты, тесты, защита, голос, фото, картинки,
кнопки, персоны, настройки, документы, напоминания, группы, лимиты, статус, бэкапы, сжатие."""

import datetime
import io
import zipfile

from aiogram.methods import SendDocument, SendMessage, SendPhoto, SendVoice

from bot import tasks
from bot.services import ProcResult, RunResult

from .conftest import ADMIN, FRIEND, GROUP, STRANGER

LEAKY = RunResult(
    compiled=True, compile_command="cc -Wall -Wextra -Werror -g main.c", compile_output="",
    valgrind_log="==1== definitely lost: 10 bytes in 1 blocks\n==1== ERROR SUMMARY: 1 errors",
    run=ProcResult(exit_code=42, signal=None, timed_out=False, stdout="hi\n", stderr="", duration_ms=50),
)


def make_zip(files: dict[str, str], root: str = "libft") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, content in files.items():
            z.writestr(f"{root}/{name}", content)
        z.writestr(f"{root}/image.png", b"\x89PNG")
    return buf.getvalue()


# ---------------------------------------------------------------- песочница


async def test_run_uses_last_code_and_args(env):
    await env.send_file(FRIEND, "main.c", b"int main(void){return 0;}\n")
    env.llm.calls.clear()
    await env.send(FRIEND, '/run hello "big world" <<< input line')
    run = env.sandbox.runs[-1]
    assert run["files"] == {"main.c": "int main(void){return 0;}\n"}
    assert run["args"] == ["hello", "big world"] and run["stdin"] == "input line\n"
    assert run["check"] == "none"
    assert "Завершилась с кодом 0" in env.last_text()
    assert env.llm.calls == []  # всё хорошо — модель не зовём


async def test_valgrind_problems_are_explained(env):
    env.sandbox.result = LEAKY
    await env.send_file(FRIEND, "main.c", b"int main(void){return 0;}\n")
    await env.send(FRIEND, "/valgrind")
    assert env.sandbox.runs[-1]["check"] == "valgrind"
    assert any("definitely lost" in t for t in env.texts())
    assert "definitely lost" in env.last_user_prompt()


async def test_run_from_reply_to_code_message(env):
    code_msg = env.message(FRIEND, text="```c\nint main(void){return 1;}\n```")
    await env.send(FRIEND, "/asan", reply_to_message=code_msg)
    assert env.sandbox.runs[-1]["files"] == {"main.c": "int main(void){return 1;}\n"}
    assert env.sandbox.runs[-1]["check"] == "asan"


async def test_run_without_code(env):
    await env.send(FRIEND, "/run")
    assert "Сначала пришли код" in env.last_text()


async def test_sandbox_disabled(make_env):
    e = await make_env()
    e.app.sandbox = None
    await e.send(FRIEND, "/run")
    assert "Песочница выключена" in e.last_text()


async def test_project_zip(env):
    env.sandbox.project_report["makefile"]["issues"] = ["Relink: повторный make снова что-то компилирует"]
    data = make_zip({"Makefile": "all:\n", "ft_strlen.c": "int x;", "libft.h": "#pragma once"})
    await env.send_file(FRIEND, "libft.zip", data)
    files = env.sandbox.projects[-1]
    assert set(files) == {"Makefile", "ft_strlen.c", "libft.h"}  # корневая папка срезана, png пропущен
    assert any("Проверка проекта" in t and "Relink" in t for t in env.texts())
    assert "Relink" in env.last_user_prompt()  # есть проблемы — модель разбирает
    assert (await env.db.get_state(FRIEND)).files == files


async def test_tests_generation_flow(env):
    env.llm.chat_reply = '```c\n#include <stdio.h>\nint main(void){ puts("OK: a"); return 0; }\n```'
    await env.send_file(FRIEND, "ft_strlen.c", b"int ft_strlen(char *s){int i=0; while(s[i]) i++; return i;}\n")
    await env.send(FRIEND, "/tests")
    run = env.sandbox.runs[-1]
    assert set(run["files"]) == {"ft_strlen.c", "fox_tests.c"}
    assert run["check"] == "asan" and run["werror"] is False
    assert 'puts("OK: a")' in run["files"]["fox_tests.c"]
    docs = env.session.of_type(SendDocument)
    assert docs and docs[-1].document.filename == "fox_tests.c"
    assert "AddressSanitizer" in env.last_user_prompt()


async def test_tests_rejects_code_with_main(env):
    await env.send_file(FRIEND, "main.c", b"int main(void){return 0;}\n")
    await env.send(FRIEND, "/tests")
    assert "уже есть main" in env.last_text()


async def test_defense_mode(env):
    await env.send_file(FRIEND, "ft_atoi.c", b"int ft_atoi(const char *s){return 0;}\n")
    await env.send(FRIEND, "/defense")
    assert await env.db.get_mode(FRIEND) == "defense"
    prompt = env.system_prompt()
    assert "проверяющий" in prompt and "ft_atoi" in prompt
    assert env.llm.calls[-1]["model"] == "qwen2.5-coder:7b"

    await env.send(FRIEND, "потому что пробелы пропускаются через while")
    assert "ft_atoi" in env.system_prompt()

    await env.send(FRIEND, "/defense stop")
    assert env.last_user_prompt() == "итог"
    assert await env.db.get_mode(FRIEND) == "code42"
    assert (await env.db.get_state(FRIEND)).defense_code is None


# ---------------------------------------------------------------- голос, фото, картинки


async def test_voice_message_transcribed_and_answered(env):
    env.speech.text = "расскажи рецепт борща"
    await env.send_voice(FRIEND)
    assert any("🎤" in t and "рецепт борща" in t for t in env.texts())
    assert env.last_user_prompt() == "расскажи рецепт борща"
    assert env.session.of_type(SendVoice) == []  # голосовые ответы выключены по умолчанию

    await env.click(FRIEND, "st:v")
    await env.send_voice(FRIEND)
    assert env.speech.synthesized and env.session.of_type(SendVoice)


async def test_tts_button(env):
    env.llm.reply = "Вот рецепт борща"
    await env.send(FRIEND, "рецепт борща пожалуйста подробный")
    stored = (await env.db.last_messages(FRIEND, 1))[0]
    await env.click(FRIEND, "a:tts", message_id=stored.tg_msg_id)
    assert env.speech.synthesized[-1].startswith("Вот рецепт борща")


async def test_photo_goes_to_vision_model(env):
    await env.send_photo(FRIEND, caption="что приготовить из этого?")
    call = env.llm.calls[-1]
    assert call["model"] == "qwen2.5vl:7b"
    assert call["messages"][-1]["images"] and call["tools"] is None
    history = await env.db.get_history(FRIEND, 5)
    assert history[0]["content"] == "[фото] что приготовить из этого?"


async def test_photo_without_vision_model(env):
    env.llm.models.remove("qwen2.5vl:7b")
    await env.send_photo(FRIEND)
    assert "не скачана" in env.last_text()


async def test_draw(env):
    env.llm.chat_reply = "a red fox coding at night"
    await env.send(FRIEND, "/draw рыжая лиса программирует ночью")
    assert env.images.prompts == ["a red fox coding at night"]
    assert env.llm.unloaded == ["*"] and env.images.unloads == 1  # одна 3070: VRAM освобождается
    photo = env.session.of_type(SendPhoto)[-1]
    assert "рыжая лиса" in photo.caption


# ---------------------------------------------------------------- кнопки под ответом


async def test_regenerate_replaces_last_answer(env):
    env.llm.reply = "первый ответ"
    await env.send(FRIEND, "придумай название для кота")
    last = (await env.db.last_messages(FRIEND, 1))[0]
    env.llm.reply = "второй ответ"
    await env.click(FRIEND, "a:rg", message_id=last.tg_msg_id)
    history = await env.db.get_history(FRIEND, 10)
    assert [m["content"].strip() for m in history] == ["придумай название для кота", "второй ответ"]


async def test_regenerate_only_last_answer(env):
    await env.send(FRIEND, "первый вопрос про котов и собак")
    first = (await env.db.last_messages(FRIEND, 1))[0]
    await env.send(FRIEND, "второй вопрос про котов и собак")
    calls = len(env.llm.calls)
    await env.click(FRIEND, "a:rg", message_id=first.tg_msg_id)
    assert len(env.llm.calls) == calls


async def test_alternative_model(env):
    await env.send(FRIEND, "объясни рекурсию на пальцах пожалуйста")
    last = (await env.db.last_messages(FRIEND, 1))[0]
    await env.click(FRIEND, "a:alt", message_id=last.tg_msg_id)
    labels = [row[0].text for row in env.session.of_type(SendMessage)[-1].reply_markup.inline_keyboard]
    assert "llama3.1:8b" in labels and "bge-m3:latest" not in labels
    await env.click(FRIEND, "am:llama3.1:8b")
    assert env.llm.calls[-1]["model"] == "llama3.1:8b"
    assert len(await env.db.get_history(FRIEND, 10)) == 2  # старая пара заменена


async def test_more_button(env):
    await env.send(FRIEND, "что такое указатель в C")
    await env.click(FRIEND, "a:more")
    assert "подробнее" in env.last_user_prompt()


# ---------------------------------------------------------------- настройки и персоны


async def test_persona_and_settings(env):
    await env.send(FRIEND, "/persona шеф")
    await env.click(FRIEND, "st:t:0.2")
    await env.click(FRIEND, "st:l:short")
    await env.send(FRIEND, "что приготовить на ужин сегодня вечером")
    prompt = env.system_prompt()
    assert "шеф-повар" in prompt and "максимально кратко" in prompt
    assert env.llm.calls[-1]["options"] == {"temperature": 0.2}

    await env.send(FRIEND, "/persona off")
    await env.click(FRIEND, "st:reset")
    await env.send(FRIEND, "что приготовить на ужин сегодня вечером")
    assert "шеф-повар" not in env.system_prompt()
    assert env.llm.calls[-1]["options"] == {}


async def test_timezone(env):
    await env.send(FRIEND, "/tz Asia/Tokyo")
    assert (await env.db.get_user(FRIEND)).tz == "Asia/Tokyo"
    await env.send(FRIEND, "/tz Mars/Olympus")
    assert "Не знаю" in env.last_text()


# ---------------------------------------------------------------- документы


async def test_personal_documents(env):
    await env.send_file(FRIEND, "recipes.md", "Бабушкин пирог: 3 яйца, 200 г муки, 150 г сахара.".encode())
    assert "Добавил «recipes.md»" in env.last_text()
    await env.send(FRIEND, "сколько муки нужно на бабушкин пирог")
    assert "[recipes.md]" in env.system_prompt() and "200 г муки" in env.system_prompt()

    docs = await env.assistant.docs.list_docs(FRIEND)
    await env.db.add_user(ADMIN)
    await env.send(ADMIN, "сколько муки нужно на бабушкин пирог")
    assert "recipes.md" not in env.system_prompt()  # чужие документы не видны

    await env.send(FRIEND, f"/docdel {docs[0].id}")
    assert await env.assistant.docs.list_docs(FRIEND) == []


# ---------------------------------------------------------------- напоминания


async def test_remind_command_and_delivery(env):
    await env.send(FRIEND, "/remind через 20 минут снять пасту")
    assert "Напомню" in env.last_text()
    [reminder] = await env.db.list_reminders(FRIEND)
    assert reminder.text == "снять пасту"
    now = datetime.datetime.now(datetime.UTC)
    assert datetime.timedelta(minutes=19) < reminder.due_at - now <= datetime.timedelta(minutes=20)

    assert await tasks.deliver_reminders(env.bot, env.app, now) == 0
    sent = await tasks.deliver_reminders(env.bot, env.app, now + datetime.timedelta(minutes=21))
    assert sent == 1 and "⏰ Напоминание: снять пасту" in env.last_text()
    assert await env.db.list_reminders(FRIEND) == []


async def test_reminder_via_llm_tool(env):
    env.llm.tool_script = [[{"function": {"name": "set_reminder",
                                          "arguments": {"when": "через 2 часа", "text": "позвонить маме"}}}]]
    env.llm.reply = "Готово, напомню!"
    await env.send(FRIEND, "напомни через 2 часа позвонить маме")
    [reminder] = await env.db.list_reminders(FRIEND)
    assert reminder.text == "позвонить маме"
    assert any("⏰ Ставлю напоминание" in t for t in env.texts())


async def test_unremind(env):
    await env.send(FRIEND, "/remind завтра в 9 защита")
    [r] = await env.db.list_reminders(FRIEND)
    await env.send(FRIEND, f"/unremind {r.id}")
    assert await env.db.list_reminders(FRIEND) == []


# ---------------------------------------------------------------- группы


async def test_group_ignores_unaddressed_and_strangers(env):
    await env.send(FRIEND, "просто болтаем", chat_id=GROUP)
    await env.send(STRANGER, "@fox_ai_bot привет", chat_id=GROUP)
    assert env.llm.calls == []
    assert "Доступ закрыт" in env.last_text()


async def test_group_mention_uses_shared_history(env):
    await env.send(FRIEND, "@fox_ai_bot какой фильм посмотреть вечером", chat_id=GROUP)
    assert env.last_user_prompt() == "[u20]: какой фильм посмотреть вечером"
    assert "групповой чат" in env.system_prompt()
    assert len(await env.db.get_history(GROUP, 10)) == 2
    assert await env.db.get_history(FRIEND, 10) == []  # личный диалог не тронут


async def test_group_allowchat_lets_guests_in(env):
    await env.send(ADMIN, "/allowchat", chat_id=GROUP)
    assert await env.db.is_chat_allowed(GROUP)
    await env.send(STRANGER, "/ask посоветуй пиццу на вечер", chat_id=GROUP)
    assert env.last_user_prompt() == "[u30]: посоветуй пиццу на вечер"
    assert "Что ты знаешь о собеседнике" not in env.system_prompt()
    await env.send(STRANGER, "/remember я гость", chat_id=GROUP)
    assert "только пользователям" in env.last_text()


async def test_group_command_for_other_bot_ignored(env):
    await env.send(FRIEND, "/help@other_bot", chat_id=GROUP)
    assert env.texts() == []


# ---------------------------------------------------------------- лимиты, очередь


async def test_daily_limit(make_env):
    e = await make_env(daily_limit=2)
    await e.send(FRIEND, "первый вопрос о жизни и вселенной")
    await e.send(FRIEND, "второй вопрос о жизни и вселенной")
    await e.send(FRIEND, "третий вопрос о жизни и вселенной")
    assert len(e.llm.calls) == 2 and "лимит" in e.last_text()
    await e.db.add_user(ADMIN)
    for _ in range(3):
        await e.send(ADMIN, "админ без лимитов, правда же")
    assert len(e.llm.calls) == 5


# ---------------------------------------------------------------- админ: статус и бэкапы


async def test_status(env, monkeypatch):
    from bot.gpu import GpuInfo
    from bot.handlers import admin

    async def fake_gpus():
        return [GpuInfo(0, "Tesla P100-PCIE-16GB", 88, 95, 15000, 16384, 240.0, None),
                GpuInfo(1, "NVIDIA GeForce RTX 3070", 60, 10, 2000, 8192, 50.0, 40)]

    monkeypatch.setattr(admin, "query_gpus", fake_gpus)
    await env.send(ADMIN, "/status")
    text = env.last_text()
    assert "🔥 [0] Tesla P100" in text and "88°C" in text
    assert "qwen2.5:7b" in text and "Очередь" in text
    assert "песочница: ✅" in text and "картинки: ❌" in text

    await env.send(FRIEND, "/status")
    assert "Не знаю такую команду" in env.last_text()


async def test_gpu_alert(env, monkeypatch):
    from bot.gpu import GpuInfo

    async def hot():
        return [GpuInfo(0, "Tesla P100", 91, 100, 1, 2, None, None)]

    monkeypatch.setattr(tasks, "query_gpus", hot)
    assert await tasks.check_gpu_temperature(env.bot, env.app)
    assert "перегрев" in env.last_text()
    assert not await tasks.check_gpu_temperature(env.bot, env.app)  # кулдаун


async def test_backup_command(env):
    await env.send(FRIEND, "привет")
    await env.send(ADMIN, "/backup")
    assert "Бэкап готов" in env.last_text()
    [path] = list((env.tmp_path / "backups").glob("*.sqlite3.gz"))
    import gzip
    import sqlite3

    raw = env.tmp_path / "restored.sqlite3"
    raw.write_bytes(gzip.decompress(path.read_bytes()))
    with sqlite3.connect(raw) as conn:
        assert conn.execute("SELECT COUNT(*) FROM chat_messages").fetchone()[0] == 2


# ---------------------------------------------------------------- сжатие диалога


async def test_history_summarization(make_env):
    e = await make_env(history_limit=4, summary_batch=4)
    e.llm.chat_reply = "Обсуждали рецепты пасты"
    for i in range(5):
        await e.send(FRIEND, f"вопрос номер {i} про пасту карбонару")
    assert await e.db.get_summary(FRIEND) == "Обсуждали рецепты пасты"
    assert len(await e.db.get_history(FRIEND, 100)) < 10
    await e.send(FRIEND, "а что с соусом делать дальше")
    assert "Обсуждали рецепты пасты" in e.system_prompt()
    summary_calls = [c for c in e.llm.chat_calls if not c["json"]]
    assert summary_calls and summary_calls[0]["model"] == "qwen2.5:3b"

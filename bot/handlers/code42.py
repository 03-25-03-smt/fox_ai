"""Всё для 42: norminette, запуск кода, тесты, проверка проекта, защита, интра."""

import datetime
import html
import logging
import re
import shlex
from pathlib import PurePosixPath

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ..app import App
from ..assistant import Turn
from ..intra import IntraError, IntraProfile
from ..llm import LLMError
from ..norminette import (
    MAX_SOURCE_BYTES,
    NorminetteError,
    extract_code,
    run_norminette,
    safe_filename,
)
from ..projects import ProjectError, files_from_git, files_from_zip, sources_digest
from ..services import RunResult, ServiceError
from .common import need_registered, respond, send_long, with_user
from .registry import Routes

log = logging.getLogger(__name__)
router = Routes("code42")

MAX_CODE_IN_PROMPT = 12000
MAX_OUT_IN_MSG = 2500
TEST_FILE = "fox_tests.c"
MAIN_RE = re.compile(r"\bint\s+main\s*\(")
GIT_URL_RE = re.compile(r"https://\S+")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "\n…[обрезано]"


# ---------------------------------------------------------------- norminette


async def check_norm(message: Message, app: App, turn: Turn, source: str, filename: str) -> None:
    try:
        result = await run_norminette(source, filename)
    except NorminetteError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    if result.ok:
        await message.answer(f"✅ <code>{result.filename}</code>: Norm OK!", parse_mode=ParseMode.HTML)
        return
    await send_long(
        message,
        f"❌ **{result.filename}**: ошибок norminette — {result.errors}\n```\n{result.output}\n```",
        edit=False,
    )
    code = _clip(source, MAX_CODE_IN_PROMPT)
    prompt = (
        f"Я проверил файл {result.filename} через norminette. Объясни ошибки простыми словами, "
        "сгруппируй однотипные и покажи, как исправить (с примерами исправленных строк).\n\n"
        f"Вывод norminette:\n```\n{result.output[:6000]}\n```\n\nКод:\n```c\n{code}\n```"
    )
    await respond(message, app, turn, prompt, store_text=f"[norminette {result.filename}]\n{prompt}",
                  extract_memory=False, allow_tools=False)


@router.message(Command("norm"))
async def cmd_norm(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    reply = message.reply_to_message
    source = command.args or (reply.text if reply and reply.text else "")
    if not source.strip():
        await message.answer("Пришли файл .c/.h, или /norm с кодом, или ответь /norm на сообщение с кодом.")
        return
    source = extract_code(source)
    name = safe_filename(None, source)
    await app.db.set_code(turn.chat_id, name, {name: source})
    await check_norm(message, app, turn, source, name)


# ---------------------------------------------------------------- получение кода


async def _code_from_reply(message: Message, bot: Bot) -> tuple[str, dict[str, str]] | None:
    reply = message.reply_to_message
    if reply is None:
        return None
    if reply.document and reply.document.file_name:
        name = reply.document.file_name
        if (reply.document.file_size or 0) > 20 * 1024 * 1024:
            return None
        data = (await bot.download(reply.document)).read()
        if name.lower().endswith(".zip"):
            files, _ = files_from_zip(data)
            return name, files
        if name.lower().endswith((".c", ".h")) and len(data) <= MAX_SOURCE_BYTES:
            fname = safe_filename(name, data.decode(errors="replace"))
            return fname, {fname: data.decode("utf-8", errors="replace")}
    if reply.text:
        code = extract_code(reply.text)
        return "main.c", {"main.c": code}
    return None


async def get_code(message: Message, bot: Bot, app: App, turn: Turn) -> tuple[str, dict[str, str]] | None:
    try:
        found = await _code_from_reply(message, bot)
    except ProjectError as exc:
        await message.answer(f"⚠️ {exc}")
        return None
    if found:
        await app.db.set_code(turn.chat_id, *found)
        return found
    state = await app.db.get_state(turn.chat_id)
    if state.files:
        return state.code_name or "код", state.files
    await message.answer(
        "Сначала пришли код: файл .c, zip-архив, /project <git-ссылка> — "
        "или ответь командой на сообщение с кодом."
    )
    return None


# ---------------------------------------------------------------- /run /valgrind /asan


def format_run(result: RunResult, check: str) -> str:
    parts = [f"⚙️ `{result.compile_command}`"]
    if not result.compiled:
        parts.append("❌ **Ошибка компиляции**")
        parts.append(f"```\n{_clip(result.compile_output, MAX_OUT_IN_MSG)}\n```")
        return "\n".join(parts)
    if result.compile_output:
        parts.append(f"Предупреждения:\n```\n{_clip(result.compile_output, 1000)}\n```")
    run = result.run
    if run.timed_out:
        verdict = "⏱ **Таймаут** — программа работала слишком долго (бесконечный цикл?)"
    elif run.signal:
        verdict = f"💥 **Упала с сигналом {run.signal}**"
    elif run.exit_code == 0:
        verdict = "✅ Завершилась с кодом 0"
    else:
        verdict = f"⚠️ Код выхода {run.exit_code}"
    parts.append(f"{verdict} ({run.duration_ms} мс)")
    if run.stdout:
        parts.append(f"stdout:\n```\n{_clip(run.stdout, MAX_OUT_IN_MSG)}\n```")
    if run.stderr:
        parts.append(f"stderr:\n```\n{_clip(run.stderr, MAX_OUT_IN_MSG)}\n```")
    if check == "valgrind" and result.valgrind_log:
        summary = "\n".join(
            line for line in result.valgrind_log.splitlines()
            if any(k in line for k in ("definitely lost", "indirectly lost", "possibly lost",
                                       "still reachable", "ERROR SUMMARY", "Invalid", "uninitialised",
                                       "FILE DESCRIPTORS", "All heap blocks were freed"))
        )
        parts.append(f"valgrind:\n```\n{_clip(summary or result.valgrind_log, MAX_OUT_IN_MSG)}\n```")
    return "\n".join(parts)


def _parse_run_args(raw: str | None) -> tuple[list[str], str]:
    """'/run a b <<< текст' -> (['a', 'b'], 'текст\\n')."""
    raw = raw or ""
    stdin = ""
    if "<<<" in raw:
        raw, stdin = raw.split("<<<", 1)
        stdin = stdin.strip() + "\n"
    try:
        return shlex.split(raw), stdin
    except ValueError:
        return raw.split(), stdin


async def _run_command(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn,
                       check: str) -> None:
    if app.sandbox is None:
        await message.answer("🧪 Песочница выключена (не задан SANDBOX_URL).")
        return
    found = await get_code(message, bot, app, turn)
    if found is None:
        return
    name, files = found
    args, stdin = _parse_run_args(command.args)
    status = await message.answer(f"🧪 Компилирую и запускаю {name}…")
    try:
        result = await app.sandbox.run(files, args=args, stdin=stdin, check=check)
    except ServiceError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    report = format_run(result, check)
    await send_long(status, report)
    if result.has_problems:
        code = _clip(sources_digest({k: v for k, v in files.items() if k.endswith((".c", ".h"))}),
                     MAX_CODE_IN_PROMPT)
        prompt = (
            "Я скомпилировал и запустил код в песочнице. Объясни, что пошло не так "
            "(ошибки компиляции, падение, утечки, ошибки памяти), укажи конкретные строки "
            f"и как исправить.\n\nРезультат:\n{report}\n\nКод:\n```c\n{code}\n```"
        )
        await respond(message, app, turn, prompt, store_text=f"[{check} {name}]\n{report}",
                      extract_memory=False, allow_tools=False)


@router.message(Command("run"))
async def cmd_run(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    await _run_command(message, command, bot, app, turn, "none")


@router.message(Command("valgrind"))
async def cmd_valgrind(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    await _run_command(message, command, bot, app, turn, "valgrind")


@router.message(Command("asan"))
async def cmd_asan(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    await _run_command(message, command, bot, app, turn, "asan")


# ---------------------------------------------------------------- /tests

TESTS_PROMPT = (
    "Напиши файл {test_file} на C с функцией main, который тщательно тестирует функции "
    "из кода ниже. Требования:\n"
    "- подключи нужные заголовки (<stdio.h>, <string.h>, <stdlib.h>, <limits.h>, <ctype.h> и т.д.) "
    "и объяви прототипы тестируемых функций, если нет своего .h;\n"
    "- если у функции есть аналог в libc (strlen, atoi, memcpy, printf…) — сравнивай результаты с ним;\n"
    "- покрой граничные случаи: пустые строки, NULL где допустимо, INT_MIN/INT_MAX, "
    "большие размеры, пересекающиеся области памяти и т.п.;\n"
    "- для каждого теста печатай строку «OK: описание» или «KO: описание (ожидалось X, получено Y)»;\n"
    "- в конце напечатай «TOTAL: пройдено/всего»; освобождай память;\n"
    "- НЕ определяй функции из тестируемого кода заново.\n"
    "Верни ТОЛЬКО код в одном блоке ```c.\n\nКод:\n```c\n{code}\n```"
)


async def _generate_tests(app: App, turn: Turn, code: str, error: str | None = None,
                          previous: str | None = None) -> str:
    model = await app.assistant.resolve_model(turn, "```c test", app.assistant.mode_for(turn))
    if turn.user is None or turn.user.model is None:
        model = app.settings.code_model or model
    messages = [{"role": "user", "content": TESTS_PROMPT.format(test_file=TEST_FILE, code=code)}]
    if error and previous:
        messages += [
            {"role": "assistant", "content": f"```c\n{previous}\n```"},
            {"role": "user", "content": f"Тесты не компилируются:\n```\n{error[:3000]}\n```\n"
                                        "Исправь и верни весь файл целиком в блоке ```c."},
        ]
    async with app.queue.slot():
        raw = await app.llm.chat(model, messages, options={"temperature": 0.2})
    return extract_code(raw).strip() + "\n"


@router.message(Command("tests"))
async def cmd_tests(message: Message, bot: Bot, app: App, turn: Turn) -> None:
    if app.sandbox is None:
        await message.answer("🧪 Песочница выключена (не задан SANDBOX_URL).")
        return
    allowed, _ = await app.check_limit(turn.user_id)
    if not allowed:
        await message.answer("🚫 Дневной лимит исчерпан.")
        return
    found = await get_code(message, bot, app, turn)
    if found is None:
        return
    name, files = found
    sources = {k: v for k, v in files.items() if k.endswith((".c", ".h"))}
    if any(MAIN_RE.search(v) for k, v in sources.items() if k.endswith(".c")):
        await message.answer("В коде уже есть main — пришли файл(ы) только с функциями, тесты я напишу сам.")
        return
    code = _clip(sources_digest(sources), MAX_CODE_IN_PROMPT)
    status = await message.answer("🧪 Пишу тесты…")
    try:
        tests = await _generate_tests(app, turn, code)
        await status.edit_text("🧪 Компилирую и запускаю тесты (ASan)…")
        result = await app.sandbox.run({**sources, TEST_FILE: tests}, check="asan", werror=False, timeout=20)
        if not result.compiled and TEST_FILE in result.compile_output:
            await status.edit_text("🧪 Тесты не собрались, исправляю…")
            tests = await _generate_tests(app, turn, code, result.compile_output, tests)
            result = await app.sandbox.run({**sources, TEST_FILE: tests}, check="asan", werror=False, timeout=20)
    except (LLMError, ServiceError) as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    await app.count_usage(turn.user_id)
    await message.answer_document(BufferedInputFile(tests.encode(), TEST_FILE),
                                  caption="Тесты — можно запускать и у себя")
    report = format_run(result, "asan")
    await send_long(status, report)
    prompt = (
        "Я сгенерировал тесты для кода и прогнал их с AddressSanitizer. Кратко подведи итог: "
        "сколько тестов прошло, какие упали и почему (с указанием строк кода), какие ошибки памяти "
        "нашлись и как исправить. Если тест сам по себе неверен — так и скажи.\n\n"
        f"Результат:\n{report}\n\nКод:\n```c\n{code}\n```\n\nТесты:\n```c\n{_clip(tests, 6000)}\n```"
    )
    await respond(message, app, turn, prompt, store_text=f"[tests {name}]\n{report}",
                  extract_memory=False, allow_tools=False)


# ---------------------------------------------------------------- проект


def format_project(report: dict, skipped: int) -> str:
    norm, mk, build = report["norminette"], report["makefile"], report["build"]
    lines = [f"📦 **Проверка проекта** ({len(report['files'])} файлов)"]
    if skipped:
        lines.append(f"(пропущено из-за лимитов: {skipped})")
    lines.append(
        f"\n**Norminette:** {'✅ OK' if norm['ok'] else '❌ ошибок: ' + str(norm['errors'])} "
        f"(файлов: {norm['files_checked']})"
    )
    if not norm["ok"]:
        lines.append(f"```\n{_clip(norm['output'], 2000)}\n```")
    if mk["exists"]:
        lines.append(f"\n**Makefile** ({mk['path']}): " + ("✅ OK" if not mk["issues"] else ""))
        lines += [f"• {issue}" for issue in mk["issues"]]
    else:
        lines.append("\n**Makefile:** ❌ не найден")
    if build["attempted"]:
        lines.append(f"\n**Сборка:** {'✅ собирается' if build['ok'] else '❌ ошибка'}")
        if not build["ok"]:
            lines.append(f"```\n{_clip(build['output'], 2000)}\n```")
        elif build["relinks"] is not None:
            lines.append("Relink: " + ("❌ есть" if build["relinks"] else "✅ нет"))
    return "\n".join(lines)


async def check_project(message: Message, app: App, turn: Turn, name: str,
                        files: dict[str, str], skipped: int) -> None:
    await app.db.set_code(turn.chat_id, name, files)
    if app.sandbox is None:
        await message.answer(
            f"📦 Сохранил {name} ({len(files)} файлов). Песочница выключена, поэтому проверяю только "
            "норму отдельных файлов — пришли их по одному. /defense работает."
        )
        return
    status = await message.answer(f"📦 Проверяю {name}: norminette, Makefile, сборка, relink…")
    try:
        report = await app.sandbox.check_project(files)
    except ServiceError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    text = format_project(report, skipped)
    await send_long(status, text + "\n\nДальше: /run, /valgrind, /tests, /defense")
    has_issues = (not report["norminette"]["ok"] or report["makefile"]["issues"]
                  or (report["build"]["attempted"] and not report["build"]["ok"]))
    if has_issues:
        prompt = (
            "Я проверил проект 42. Разбери результаты: объясни каждую проблему и как её исправить, "
            "по приоритету (сначала то, из-за чего проект не примут).\n\n"
            f"{text}\n\nИсходники (фрагмент):\n{_clip(sources_digest(files, 8000), 8000)}"
        )
        await respond(message, app, turn, prompt, store_text=f"[project {name}]\n{text}",
                      extract_memory=False, allow_tools=False)


@router.message(Command("project"))
async def cmd_project(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    url_match = GIT_URL_RE.search(command.args or "")
    if not url_match:
        await message.answer(
            "📦 Пришли zip-архив проекта или: /project https://github.com/login/libft\n"
            "Проверю norminette всех файлов, Makefile (правила, wildcard, флаги), сборку и relink."
        )
        return
    url = url_match.group(0)
    status = await message.answer("📥 Клонирую репозиторий…")
    try:
        files, skipped = await files_from_git(url)
    except ProjectError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    await status.delete()
    name = PurePosixPath(url.rstrip("/")).name.removesuffix(".git") or "repo"
    await check_project(message, app, turn, name, files, skipped)


# ---------------------------------------------------------------- защита


@router.message(Command("defense"))
async def cmd_defense(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    arg = (command.args or "").strip().lower()
    if arg in ("stop", "стоп", "итог", "end"):
        state = await app.db.get_state(turn.chat_id)
        if not state.defense_code:
            await message.answer("Защита не идёт. Начать: /defense")
            return
        await respond(message, app, turn, "итог", extract_memory=False, allow_tools=False)
        await app.db.set_defense(turn.chat_id, None)
        await app.db.set_mode(turn.user_id, "code42")
        await message.answer("🎓 Защита завершена, режим: 🧑‍💻 42 / код")
        return

    found = await get_code(message, bot, app, turn)
    if found is None:
        return
    name, files = found
    digest = sources_digest(files, MAX_CODE_IN_PROMPT)
    await app.db.set_defense(turn.chat_id, digest)
    await app.db.set_mode(turn.user_id, "defense")
    turn = with_user(turn, mode="defense")
    await message.answer(
        f"🎓 Начинаем защиту {name}. Отвечай на вопросы как на настоящей защите.\n"
        "Закончить и получить оценку: /defense stop"
    )
    await respond(message, app, turn, "Начни защиту: коротко поприветствуй и задай первый вопрос.",
                  extract_memory=False, allow_tools=False, keyboard=False)


# ---------------------------------------------------------------- интра


def format_profile(p: IntraProfile, now: datetime.datetime | None = None) -> str:
    lines = [f"🎓 <b>{html.escape(p.display_name or p.login)}</b> ({html.escape(p.login)})"]
    if p.campus:
        lines.append(f"Кампус: {html.escape(p.campus)}")
    if p.level is not None:
        lines.append(f"Уровень: {p.level:.2f}" + (f" · {html.escape(p.grade)}" if p.grade else ""))
    days = p.blackhole_days(now)
    if days is not None:
        icon = "🟢" if days > 30 else "🟡" if days > 14 else "🔴"
        lines.append(f"{icon} Blackhole через {days} дн. ({p.blackhole_at:%d.%m.%Y})")
    lines.append(f"Кошелёк: {p.wallet} ₳ · Очки проверок: {p.correction_points}")
    if p.in_progress:
        lines.append("\n<b>В процессе:</b>")
        lines += [f"• {html.escape(pr.name)} ({pr.status.replace('_', ' ')})" for pr in p.in_progress]
    if p.finished:
        lines.append("\n<b>Последние оценки:</b>")
        for pr in p.finished[:6]:
            mark = "✅" if pr.validated else "❌"
            lines.append(f"{mark} {html.escape(pr.name)}: {pr.final_mark if pr.final_mark is not None else '—'}")
    return "\n".join(lines)


@router.message(Command("42"))
async def cmd_intra(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    if app.intra is None:
        await message.answer(
            "🎓 Интра не подключена. Админу: создать приложение на "
            "https://profile.intra.42.fr/oauth/applications и задать INTRA_CLIENT_ID/INTRA_CLIENT_SECRET."
        )
        return
    parts = (command.args or "").split()
    if len(parts) == 2 and parts[0].lower() in ("login", "логин"):
        await app.db.set_user_field(turn.user_id, "intra_login", parts[1].lower())
        await message.answer(f"✅ Логин сохранён: {parts[1].lower()}. Буду напоминать о blackhole.")
        return
    if parts and parts[0].lower() in ("off", "выкл"):
        await app.db.set_user_field(turn.user_id, "intra_login", None)
        await message.answer("Логин удалён, напоминаний о blackhole не будет.")
        return
    login = parts[0] if parts else (turn.user.intra_login or "")
    if not login:
        await message.answer("Сначала укажи логин: /42 login твой_логин\nИли посмотри чужой: /42 логин")
        return
    try:
        profile = await app.intra.get_profile(login)
    except IntraError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer(format_profile(profile), parse_mode=ParseMode.HTML)

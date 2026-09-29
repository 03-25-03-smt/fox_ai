"""Обработчики команд и сообщений Telegram."""

import asyncio
import html
import logging
import re
import time
from collections import defaultdict
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from .access import AccessMiddleware, admin_only
from .assistant import Assistant, format_search_results
from .db import Database
from .formatting import md_to_html, split_markdown, strip_think
from .knowledge import SUPPORTED as KB_SUPPORTED
from .llm import LLMError
from .modes import MODES, get_mode
from .norminette import (
    MAX_SOURCE_BYTES,
    NorminetteError,
    extract_code,
    run_norminette,
    safe_filename,
)
from .web import WebError

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.5  # секунд между обновлениями сообщения во время генерации
PREVIEW_LIMIT = 3800  # Telegram: максимум 4096 символов в сообщении
MAX_CODE_IN_PROMPT = 12000
MAX_KB_UPLOAD = 20 * 1024 * 1024  # лимит Bot API на скачивание
MODEL_CB = "m:"
MODE_CB = "mode:"

BOT_COMMANDS = [
    BotCommand(command="mode", description="Режим: общение / 42 и код"),
    BotCommand(command="model", description="Выбрать модель"),
    BotCommand(command="norm", description="Проверить код norminette"),
    BotCommand(command="search", description="Найти в интернете"),
    BotCommand(command="remember", description="Запомнить факт о себе"),
    BotCommand(command="memories", description="Что бот обо мне помнит"),
    BotCommand(command="forget", description="Забыть факт / всё"),
    BotCommand(command="reset", description="Очистить текущий диалог"),
    BotCommand(command="whoami", description="Мои настройки"),
    BotCommand(command="help", description="Помощь"),
]

HELP_TEXT = (
    "🦊 <b>Fox AI</b> — локальный ассистент.\n\n"
    "Просто пиши — я помню диалог и важные факты о тебе.\n\n"
    "<b>Режимы и модели</b>\n"
    "/mode — 💬 общение или 🧑‍💻 42 / код\n"
    "/model — выбрать модель\n\n"
    "<b>42</b>\n"
    "Пришли файл <code>.c</code> / <code>.h</code> — прогоню norminette и объясню ошибки.\n"
    "/norm &lt;код&gt; или ответом на сообщение с кодом\n\n"
    "<b>Интернет</b>\n"
    "/search &lt;запрос&gt; — поиск (в обычном диалоге я тоже сам ищу, когда нужно)\n\n"
    "<b>Память</b>\n"
    "/remember &lt;факт&gt; · /memories · /forget &lt;номер|all&gt;\n"
    "/reset — забыть текущий диалог (факты остаются)\n"
    "/whoami — мои настройки\n"
)
ADMIN_HELP = (
    "\n<b>Админ</b>\n"
    "/adduser &lt;id&gt; [имя] · /deluser &lt;id&gt; · /users\n"
    "/reindex — переиндексировать базу знаний\n"
    "Документ .pdf/.md/.txt с подписью <code>/kb</code> — добавить в базу знаний\n"
)

# Один активный запрос на пользователя, чтобы не забивать GPU очередью.
_user_locks: defaultdict[int, asyncio.Lock] = defaultdict(asyncio.Lock)
# Ссылки на фоновые задачи, чтобы их не собрал GC.
_background: set[asyncio.Task] = set()


def _spawn(coro) -> None:
    task = asyncio.create_task(coro)
    _background.add(task)
    task.add_done_callback(_background.discard)


# ---------------------------------------------------------------- отправка


async def _safe_edit(message: Message, text: str, parse_mode: str | None = None) -> bool:
    try:
        await message.edit_text(text, parse_mode=parse_mode)
        return True
    except TelegramRetryAfter as exc:
        await asyncio.sleep(exc.retry_after)
        return False
    except TelegramBadRequest as exc:
        # "message is not modified" и подобное во время стрима — не страшно
        log.debug("edit failed: %s", exc)
        return False


def _preview(text: str, status: str = "") -> str:
    if len(text) > PREVIEW_LIMIT:
        text = "…" + text[-PREVIEW_LIMIT:]
    head = f"{status}\n\n" if status else ""
    return head + (text + " ▌" if text else "…")


async def _send_long(target: Message, text: str, edit: bool = True) -> None:
    """Отправляет Markdown-текст, разбивая на части. edit=True — первая часть
    заменяет target (плейсхолдер), иначе всё отправляется ответом на target."""
    for i, chunk in enumerate(split_markdown(text)):
        for body, mode in ((md_to_html(chunk), ParseMode.HTML), (chunk, None)):
            try:
                if i == 0 and edit:
                    await target.edit_text(body, parse_mode=mode)
                else:
                    await target.answer(body, parse_mode=mode)
                break
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    break
                log.warning("send failed (%s), fallback to plain text", exc)


async def _respond(
    message: Message,
    assistant: Assistant,
    user_text: str,
    *,
    extract_memory: bool = True,
) -> None:
    """Главный цикл: контекст -> модель (с инструментами) -> стрим в Telegram -> история."""
    uid = message.from_user.id
    lock = _user_locks[uid]
    if lock.locked():
        await message.answer("⏳ Ещё отвечаю на предыдущее сообщение, подожди.")
        return

    db, s = assistant.db, assistant.settings
    async with lock:
        model = await db.get_model(uid) or s.default_model
        mode = get_mode(await db.get_mode(uid), s.default_mode)
        messages = await assistant.build_messages(uid, mode, user_text)

        placeholder = await message.answer("🦊 думаю…")
        answer, status = "", ""
        last_edit = time.monotonic()
        try:
            async for event in assistant.run(model, messages):
                if event.kind == "status":
                    status = event.value
                    await _safe_edit(placeholder, _preview(answer, status))
                    last_edit = time.monotonic()
                    continue
                answer += event.value
                if time.monotonic() - last_edit >= EDIT_INTERVAL:
                    await _safe_edit(placeholder, _preview(strip_think(answer), status))
                    last_edit = time.monotonic()
        except LLMError as exc:
            log.warning("LLM error for user %s, model %s: %s", uid, model, exc)
            await _safe_edit(placeholder, f"⚠️ Ошибка модели {model}: {exc}")
            return

        answer = strip_think(answer).strip() or "(модель вернула пустой ответ)"
        await db.add_message(uid, "user", user_text)
        await db.add_message(uid, "assistant", answer)
        await _send_long(placeholder, answer)

    if extract_memory and s.memory_auto:
        _spawn(assistant.memory.extract_and_store(uid, user_text, s.memory_model or model))


# ---------------------------------------------------------------- общие команды


async def cmd_start(message: Message, is_admin: bool) -> None:
    await message.answer(HELP_TEXT + (ADMIN_HELP if is_admin else ""), parse_mode=ParseMode.HTML)


async def cmd_whoami(message: Message, assistant: Assistant) -> None:
    uid, s = message.from_user.id, assistant.settings
    model = await assistant.db.get_model(uid) or s.default_model
    mode = get_mode(await assistant.db.get_mode(uid), s.default_mode)
    facts = len(await assistant.memory.list_facts(uid))
    web = "вкл" if assistant.web else "выкл"
    await message.answer(
        f"ID: <code>{uid}</code>\n"
        f"Режим: {mode.title}\n"
        f"Модель: <code>{html.escape(model)}</code>\n"
        f"Фактов в памяти: {facts}\n"
        f"Интернет: {web}",
        parse_mode=ParseMode.HTML,
    )


async def cmd_reset(message: Message, db: Database) -> None:
    count = await db.clear_history(message.from_user.id)
    await message.answer(f"🧹 Диалог очищен ({count} сообщ.). Факты из /memories остались.")


async def cmd_mode(message: Message, assistant: Assistant) -> None:
    s = assistant.settings
    current = get_mode(await assistant.db.get_mode(message.from_user.id), s.default_mode)
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if m.key == current.key else "") + m.title, callback_data=MODE_CB + m.key
        )]
        for m in MODES.values()
    ]
    await message.answer(
        f"Текущий режим: {current.title}\nВыбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def on_mode_chosen(callback: CallbackQuery, db: Database) -> None:
    key = callback.data.removeprefix(MODE_CB)
    if key not in MODES:
        await callback.answer("Неизвестный режим", show_alert=True)
        return
    await db.set_mode(callback.from_user.id, key)
    await callback.answer("Готово")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"✅ Режим: {MODES[key].title}")


async def cmd_model(message: Message, assistant: Assistant) -> None:
    try:
        models = await assistant.llm.list_models()
    except LLMError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    embed = assistant.settings.embed_model
    models = [m for m in models if m.split(":")[0] != embed.split(":")[0]]  # модель эмбеддингов не для чата
    if not models:
        await message.answer("Нет скачанных моделей. На сервере: ollama pull <модель>")
        return

    s = assistant.settings
    current = await assistant.db.get_model(message.from_user.id) or s.default_model
    buttons = [
        [InlineKeyboardButton(text=("✅ " if name == current else "") + name, callback_data=MODEL_CB + name)]
        for name in models
        if len((MODEL_CB + name).encode()) <= 64  # лимит Telegram на callback_data
    ]
    await message.answer(
        f"Текущая модель: <code>{html.escape(current)}</code>\nВыбери:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


async def on_model_chosen(callback: CallbackQuery, assistant: Assistant) -> None:
    name = callback.data.removeprefix(MODEL_CB)
    try:
        models = await assistant.llm.list_models()
    except LLMError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    if name not in models:
        await callback.answer("Эта модель больше недоступна", show_alert=True)
        return
    await assistant.db.set_model(callback.from_user.id, name)
    await callback.answer("Готово")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"✅ Модель: <code>{html.escape(name)}</code>", parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------- память


async def cmd_remember(message: Message, command: CommandObject, assistant: Assistant) -> None:
    if not command.args:
        await message.answer("Использование: /remember я учусь в 42 Paris, мой логин vborodii")
        return
    try:
        saved = await assistant.memory.add(message.from_user.id, command.args)
    except LLMError as exc:
        await message.answer(f"⚠️ Не получилось: {exc}")
        return
    await message.answer("🧠 Запомнил" if saved else "Я это уже знаю 🙂")


async def cmd_memories(message: Message, assistant: Assistant) -> None:
    items = await assistant.memory.list_facts(message.from_user.id)
    if not items:
        await message.answer("Пока ничего о тебе не помню. /remember <факт>")
        return
    lines = [f"<code>{mid}</code>. {html.escape(text)}" for mid, text in items]
    text = "🧠 Что я помню (удалить: /forget номер):\n\n" + "\n".join(lines)
    for chunk in split_markdown(text, 3500):
        await message.answer(chunk, parse_mode=ParseMode.HTML)


async def cmd_forget(message: Message, command: CommandObject, assistant: Assistant) -> None:
    arg = (command.args or "").strip().lower()
    uid = message.from_user.id
    if arg in ("all", "всё", "все"):
        count = await assistant.memory.clear(uid)
        await message.answer(f"🧽 Забыл всё ({count} фактов)")
    elif arg.isdigit():
        ok = await assistant.memory.delete(uid, int(arg))
        await message.answer("🧽 Забыл" if ok else "Нет такого номера, см. /memories")
    else:
        await message.answer("Использование: /forget <номер> или /forget all")


# ---------------------------------------------------------------- norminette


async def _check_norm(message: Message, assistant: Assistant, source: str, filename: str) -> None:
    try:
        result = await run_norminette(source, filename)
    except NorminetteError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    if result.ok:
        await message.answer(f"✅ <code>{result.filename}</code>: Norm OK!", parse_mode=ParseMode.HTML)
        return

    await _send_long(
        message,
        f"❌ **{result.filename}**: ошибок norminette — {result.errors}\n```\n{result.output}\n```",
        edit=False,
    )
    code = source if len(source) <= MAX_CODE_IN_PROMPT else source[:MAX_CODE_IN_PROMPT] + "\n/* ...обрезано... */"
    prompt = (
        f"Я проверил файл {result.filename} через norminette. Объясни ошибки простыми словами, "
        "сгруппируй однотипные и покажи, как исправить (с примерами исправленных строк).\n\n"
        f"Вывод norminette:\n```\n{result.output[:6000]}\n```\n\nКод:\n```c\n{code}\n```"
    )
    await _respond(message, assistant, prompt, extract_memory=False)


async def cmd_norm(message: Message, command: CommandObject, assistant: Assistant) -> None:
    reply = message.reply_to_message
    source = command.args or (reply.text if reply and reply.text else "")
    if not source.strip():
        await message.answer(
            "Пришли файл .c/.h, или /norm с кодом, или ответь /norm на сообщение с кодом."
        )
        return
    source = extract_code(source)
    await _check_norm(message, assistant, source, safe_filename(None, source))


async def on_document(message: Message, bot: Bot, assistant: Assistant, is_admin: bool) -> None:
    doc = message.document
    name = doc.file_name or ""
    suffix = Path(name).suffix.lower()

    if suffix in (".c", ".h"):
        if (doc.file_size or 0) > MAX_SOURCE_BYTES:
            await message.answer("Файл слишком большой (максимум 256 КБ)")
            return
        data = await bot.download(doc)
        source = data.read().decode("utf-8", errors="replace")
        await _check_norm(message, assistant, source, safe_filename(name, source))
        return

    caption = (message.caption or "").strip()
    if caption.startswith("/kb") and suffix in KB_SUPPORTED:
        if not is_admin:
            await message.answer("Добавлять в базу знаний может только админ.")
            return
        if (doc.file_size or 0) > MAX_KB_UPLOAD:
            await message.answer("Файл больше 20 МБ — Telegram не даст его скачать боту.")
            return
        target_dir = Path(assistant.settings.knowledge_dir) / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", Path(name).name) or f"upload{suffix}"
        await bot.download(doc, destination=target_dir / safe)
        status = await message.answer(f"📚 Сохранил {safe}, индексирую…")
        await _reindex(status, assistant)
        return

    await message.answer(
        "Из файлов понимаю .c/.h (проверка norminette). "
        "Админ может добавить .pdf/.md/.txt в базу знаний с подписью /kb."
    )


# ---------------------------------------------------------------- интернет


async def cmd_search(message: Message, command: CommandObject, assistant: Assistant) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /search что ищем")
        return
    if assistant.web is None:
        await message.answer("🌐 Интернет выключен (не задан SEARXNG_URL).")
        return
    try:
        results = await assistant.web.search(query)
    except WebError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    prompt = (
        f"Вопрос: {query}\n\nРезультаты поиска в интернете:\n\n{format_search_results(results)}\n\n"
        "Ответь на вопрос по этим результатам (при необходимости открой страницы через fetch_url). "
        "В конце перечисли использованные ссылки."
    )
    await _respond(message, assistant, prompt, extract_memory=False)


# ---------------------------------------------------------------- админ


def _parse_user_id(command: CommandObject) -> tuple[int, str] | None:
    if not command.args:
        return None
    parts = command.args.split(maxsplit=1)
    try:
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except ValueError:
        return None


async def cmd_adduser(message: Message, command: CommandObject, db: Database) -> None:
    parsed = _parse_user_id(command)
    if parsed is None:
        await message.answer("Использование: /adduser <id> [имя]")
        return
    uid, name = parsed
    added = await db.add_user(uid, name, added_by=message.from_user.id)
    await message.answer("✅ Добавлен" if added else "Уже есть доступ")


async def cmd_deluser(message: Message, command: CommandObject, assistant: Assistant) -> None:
    parsed = _parse_user_id(command)
    if parsed is None:
        await message.answer("Использование: /deluser <id>")
        return
    uid = parsed[0]
    if uid in assistant.settings.admins:
        await message.answer("Админа удалить нельзя — убери его из ADMIN_IDS в .env")
        return
    removed = await assistant.db.remove_user(uid)
    await message.answer("🗑 Удалён вместе с историей и памятью" if removed else "Такого пользователя нет")


async def cmd_users(message: Message, assistant: Assistant) -> None:
    s = assistant.settings
    lines = [
        f"{'👑' if u.id in s.admins else '👤'} <code>{u.id}</code> {html.escape(u.name)} — "
        f"{get_mode(u.mode, s.default_mode).title}, {html.escape(u.model or s.default_model)}"
        for u in await assistant.db.list_users()
    ]
    await message.answer("\n".join(lines) or "Пусто", parse_mode=ParseMode.HTML)


async def _reindex(status: Message, assistant: Assistant) -> None:
    stats = await assistant.knowledge.reindex()
    files, chunks = await assistant.knowledge.stats()
    await _safe_edit(
        status,
        f"📚 База знаний: {files} файлов, {chunks} фрагментов.\n"
        f"Проиндексировано: {stats.indexed}, без изменений: {stats.skipped}, "
        f"удалено: {stats.removed}, ошибок: {stats.failed}",
    )


async def cmd_reindex(message: Message, assistant: Assistant) -> None:
    status = await message.answer("📚 Индексирую базу знаний…")
    await _reindex(status, assistant)


# ---------------------------------------------------------------- прочее


async def on_text(message: Message, assistant: Assistant) -> None:
    await _respond(message, assistant, message.text)


async def on_unknown_command(message: Message) -> None:
    await message.answer("Не знаю такую команду. /help")


async def on_unsupported(message: Message) -> None:
    await message.answer("Пока понимаю текст и файлы .c/.h 🙂")


def build_router(db: Database, admins: frozenset[int]) -> Router:
    router = Router(name="fox_ai")
    access = AccessMiddleware(db, admins)
    router.message.outer_middleware(access)
    router.callback_query.outer_middleware(access)

    router.message.register(cmd_start, CommandStart())
    router.message.register(cmd_start, Command("help"))
    router.message.register(cmd_whoami, Command("whoami"))
    router.message.register(cmd_reset, Command("reset"))
    router.message.register(cmd_mode, Command("mode"))
    router.message.register(cmd_model, Command("model"))
    router.message.register(cmd_remember, Command("remember"))
    router.message.register(cmd_memories, Command("memories"))
    router.message.register(cmd_forget, Command("forget"))
    router.message.register(cmd_norm, Command("norm"))
    router.message.register(cmd_search, Command("search"))
    router.callback_query.register(on_model_chosen, F.data.startswith(MODEL_CB))
    router.callback_query.register(on_mode_chosen, F.data.startswith(MODE_CB))

    router.message.register(cmd_adduser, Command("adduser"), admin_only)
    router.message.register(cmd_deluser, Command("deluser"), admin_only)
    router.message.register(cmd_users, Command("users"), admin_only)
    router.message.register(cmd_reindex, Command("reindex"), admin_only)

    router.message.register(on_document, F.document)
    router.message.register(on_text, F.text & ~F.text.startswith("/"))
    router.message.register(on_unknown_command, F.text.startswith("/"))
    router.message.register(on_unsupported)
    return router

"""Память, личные документы, напоминания, интернет-поиск, картинки."""

import datetime
import html

from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ..app import App
from ..assistant import Turn, format_search_results
from ..formatting import split_markdown
from ..llm import LLMError
from ..services import ServiceError
from ..timeparse import parse_reminder
from ..web import WebError
from .common import need_registered, respond
from .registry import Routes

router = Routes("tools")


# ---------------------------------------------------------------- память


@router.message(Command("remember"))
async def cmd_remember(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    if not command.args:
        await message.answer("Использование: /remember я учусь в 42 Paris, мой логин vborodii")
        return
    try:
        saved = await app.assistant.memory.add(turn.user_id, command.args)
    except LLMError as exc:
        await message.answer(f"⚠️ Не получилось: {exc}")
        return
    await message.answer("🧠 Запомнил" if saved else "Я это уже знаю 🙂")


@router.message(Command("memories"))
async def cmd_memories(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    items = await app.assistant.memory.list_facts(turn.user_id)
    if not items:
        await message.answer("Пока ничего о тебе не помню. /remember <факт>")
        return
    lines = [f"<code>{mid}</code>. {html.escape(text)}" for mid, text in items]
    text = "🧠 Что я помню (удалить: /forget номер):\n\n" + "\n".join(lines)
    for chunk in split_markdown(text, 3500):
        await message.answer(chunk, parse_mode=ParseMode.HTML)


@router.message(Command("forget"))
async def cmd_forget(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    arg = (command.args or "").strip().lower()
    memory = app.assistant.memory
    if arg in ("all", "всё", "все"):
        count = await memory.clear(turn.user_id)
        await message.answer(f"🧽 Забыл всё ({count} фактов)")
    elif arg.isdigit():
        ok = await memory.delete(turn.user_id, int(arg))
        await message.answer("🧽 Забыл" if ok else "Нет такого номера, см. /memories")
    else:
        await message.answer("Использование: /forget <номер> или /forget all")


# ---------------------------------------------------------------- документы


@router.message(Command("docs"))
async def cmd_docs(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    docs = await app.assistant.docs.list_docs(turn.user_id)
    if not docs:
        await message.answer(
            "📄 Личных документов нет. Пришли PDF/TXT/MD — я буду отвечать с опорой на них."
        )
        return
    lines = [f"<code>{d.id}</code>. {html.escape(d.name)} ({d.chunks} фрагм.)" for d in docs]
    await message.answer(
        "📄 Твои документы (удалить: /docdel номер):\n\n" + "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


@router.message(Command("docdel"))
async def cmd_docdel(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /docdel <номер из /docs>")
        return
    ok = await app.assistant.docs.delete(turn.user_id, int(arg))
    await message.answer("🗑 Удалил" if ok else "Нет такого документа")


# ---------------------------------------------------------------- напоминания


def _fmt_due(app: App, turn: Turn, due: datetime.datetime) -> str:
    local = due.astimezone(app.assistant.tz(turn.user))
    return local.strftime("%d.%m.%Y %H:%M")


@router.message(Command("remind"))
async def cmd_remind(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    if not command.args:
        await message.answer(
            "⏰ Примеры:\n/remind через 20 минут снять пасту\n/remind завтра в 9 защита\n"
            "/remind в пятницу в 18:30 пицца\n/remind 31.12 23:59 загадать желание\n\n"
            "Можно и словами в обычном чате: «напомни через час позвонить маме»."
        )
        return
    now = datetime.datetime.now(app.assistant.tz(turn.user))
    parsed = parse_reminder(command.args, now)
    if parsed is None:
        await message.answer("Не понял, когда напомнить 🤔 Пример: /remind через 2 часа выключить духовку")
        return
    rid = await app.db.add_reminder(turn.user_id, turn.chat_id, parsed.text, parsed.due)
    await message.answer(
        f"⏰ Напомню {_fmt_due(app, turn, parsed.due)}: {parsed.text}\n(отменить: /unremind {rid})"
    )


@router.message(Command("reminders"))
async def cmd_reminders(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    items = await app.db.list_reminders(turn.user_id)
    if not items:
        await message.answer("⏰ Активных напоминаний нет.")
        return
    lines = [f"<code>{r.id}</code>. {_fmt_due(app, turn, r.due_at)} — {html.escape(r.text)}" for r in items]
    await message.answer("⏰ Напоминания (отменить: /unremind номер):\n\n" + "\n".join(lines),
                         parse_mode=ParseMode.HTML)


@router.message(Command("unremind"))
async def cmd_unremind(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /unremind <номер из /reminders>")
        return
    ok = await app.db.delete_reminder(turn.user_id, int(arg))
    await message.answer("🗑 Отменил" if ok else "Нет такого напоминания")


# ---------------------------------------------------------------- интернет


@router.message(Command("search"))
async def cmd_search(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    query = (command.args or "").strip()
    if not query:
        await message.answer("Использование: /search что ищем")
        return
    web = app.assistant.web
    if web is None:
        await message.answer("🌐 Интернет выключен (не задан SEARXNG_URL).")
        return
    try:
        results = await web.search(query)
    except WebError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    prompt = (
        f"Вопрос: {query}\n\nРезультаты поиска в интернете:\n\n{format_search_results(results)}\n\n"
        "Ответь на вопрос по этим результатам (при необходимости открой страницы через fetch_url). "
        "В конце перечисли использованные ссылки."
    )
    await respond(message, app, turn, prompt, store_text=f"/search {query}", extract_memory=False)


# ---------------------------------------------------------------- картинки


@router.message(Command("draw"))
async def cmd_draw(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    prompt = (command.args or "").strip()
    if not prompt:
        await message.answer("🎨 Использование: /draw рыжая лиса программирует ночью, неон, киберпанк")
        return
    if app.imagegen is None:
        await message.answer("🎨 Генерация картинок выключена (не задан IMAGEGEN_URL).")
        return
    allowed, _ = await app.check_limit(turn.user_id)
    if not allowed:
        await message.answer("🚫 Дневной лимит исчерпан. Приходи завтра!")
        return
    if app.image_lock.locked():
        await message.answer("🎨 Уже рисую другую картинку, подожди немного.")
        return
    async with app.image_lock:
        status = await message.answer("🎨 Рисую… (первый запуск может занять пару минут — грузится модель)")
        english = await app.assistant.translate_to_english(prompt)
        try:
            png = await app.imagegen.generate(english)
        except ServiceError as exc:
            await status.edit_text(f"⚠️ {exc}")
            return
        await app.count_usage(turn.user_id)
        await status.delete()
        caption = prompt if english == prompt else f"{html.escape(prompt)}\n\n<i>{html.escape(english)}</i>"
        await message.answer_photo(
            BufferedInputFile(png, "fox_ai.png"),
            caption=caption[:1000],
            parse_mode=ParseMode.HTML if english != prompt else None,
        )

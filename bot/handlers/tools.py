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
from ..summarize import (
    MAX_CHUNKS,
    SUMMARY_SYSTEM,
    LinkError,
    chunk_text,
    fetch_transcript,
    final_prompt,
    find_urls,
    fmt_duration,
    part_prompt,
)
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


# ---------------------------------------------------------------- пересказ ссылок


async def summarize_link(message: Message, app: App, turn: Turn, url: str, question: str = "") -> None:
    status = await message.answer("📖 Читаю…" if "youtu" not in url else "🎬 Достаю субтитры…")
    try:
        t = await fetch_transcript(url, app.assistant.web, app.youtube)
    except (LinkError, WebError, ServiceError) as exc:
        await status.edit_text(f"⚠️ Не получилось: {exc}")
        return
    chunks = chunk_text(t.text)[:MAX_CHUNKS]
    head = f"📖 <b>{html.escape(t.title)}</b>\n<i>{html.escape(t.source)}"
    if t.duration:
        head += f", {fmt_duration(t.duration)}"
    head += f", {len(t.text) // 1000 or 1} тыс. знаков</i>"
    if len(chunks) == 1:
        await status.edit_text(head, parse_mode=ParseMode.HTML)
        body = chunks[0]
    else:
        notes = []
        for i, part in enumerate(chunks, 1):
            await status.edit_text(f"{head}\n\n⏳ Конспектирую часть {i}/{len(chunks)}…", parse_mode=ParseMode.HTML)
            try:
                async with app.queue.slot():
                    notes.append(await app.llm.chat(app.settings.default_model, [
                        {"role": "system", "content": SUMMARY_SYSTEM},
                        {"role": "user", "content": part_prompt(t, part, i, len(chunks))},
                    ], options={"temperature": 0.2}))
            except LLMError as exc:
                await status.edit_text(f"⚠️ Ошибка модели: {exc}")
                return
        await status.edit_text(head, parse_mode=ParseMode.HTML)
        body = "\n\n".join(f"Часть {i}:\n{n.strip()}" for i, n in enumerate(notes, 1))
    prompt = final_prompt(t, body, from_parts=len(chunks) > 1, question=question)
    await respond(message, app, turn, prompt, store_text=f"Перескажи {url} {question}".strip(),
                  allow_tools=False, extract_memory=False)


@router.message(Command("sum", "tldr"))
async def cmd_sum(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    args = command.args or ""
    if message.reply_to_message:
        args = f"{args} {message.reply_to_message.text or message.reply_to_message.caption or ''}"
    urls = find_urls(args)
    if not urls:
        await message.answer("📖 Использование: /sum <ссылка> [вопрос] — пересказ статьи или YouTube.\n"
                             "Можно просто прислать ссылку отдельным сообщением.")
        return
    question = args.replace(urls[0], "").strip() if command.args else ""
    await summarize_link(message, app, turn, urls[0], question)


# ---------------------------------------------------------------- Python


def _looks_like_code(text: str) -> bool:
    return "\n" in text or text.startswith(("import ", "from ", "print(")) or "=" in text.split("\n")[0]


def strip_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else ""
        text = text.rsplit("```", 1)[0]
    return text.strip()


@router.message(Command("py"))
async def cmd_py(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    """/py <код> — выполнить как есть; /py <задача словами> — модель напишет и выполнит код."""
    raw = strip_fence(command.args or "")
    if not raw:
        await message.answer(
            "🐍 Примеры:\n/py print(2**100)\n"
            "/py сколько будет 1850 € в кронах по курсу 24.3 и сколько это в месяц на 12 мес\n"
            "/py построй график sin(x) и cos(x) от 0 до 2π\n\n"
            "Есть numpy, pandas, matplotlib, sympy, scipy. Можно и без команды — "
            "я сам считаю на Python, когда нужна точность."
        )
        return
    if app.sandbox is None:
        await message.answer("🐍 Песочница выключена (не задан SANDBOX_URL).")
        return
    if not _looks_like_code(raw):
        prompt = (f"Задача: {raw}\n\nРеши её, обязательно вызвав run_python (напечатай итог print-ом; "
                  "если просят график — построй matplotlib). Затем коротко объясни результат.")
        await respond(message, app, turn, prompt, store_text=f"/py {raw}")
        return
    status = await message.answer("🐍 Выполняю…")
    try:
        result = await app.sandbox.python(raw)
    except ServiceError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    text = result.as_text(3500)
    await status.edit_text(f"<pre>{html.escape(text)}</pre>", parse_mode=ParseMode.HTML)
    for png in result.images:
        await message.answer_photo(BufferedInputFile(png, "plot.png"))


# ---------------------------------------------------------------- картинки


async def _generate_image(app: App, prompt: str) -> bytes:
    if not app.settings.imagegen_exclusive:
        return await app.imagegen.generate(prompt)
    # Одна видеокарта: LLM и SDXL вместе в 8 ГБ не помещаются. Занимаем очередь GPU
    # (чаты подождут), выгружаем модели Ollama, рисуем и сразу освобождаем VRAM.
    async with app.queue.slot():
        await app.llm.unload_all()
        try:
            return await app.imagegen.generate(prompt)
        finally:
            try:
                await app.imagegen.unload()
            except ServiceError:
                pass


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
            png = await _generate_image(app, english)
        except (ServiceError, LLMError) as exc:
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

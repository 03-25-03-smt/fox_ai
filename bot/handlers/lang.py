"""Учитель языков: /w /dict /wexport /wimport /quiz /lesson /test /talk /speak /dictation /lang.

Доступно пользователям из LANG_USER_IDS (по умолчанию — админам): словарь личный.
"""

import datetime
import html
import logging

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    BufferedInputFile,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..app import App
from ..assistant import Turn
from ..lang import (
    LANGS,
    LEVELS,
    MAX_IMPORT,
    MAX_IMPORT_LOOKUP,
    TOPICS,
    DictationCheck,
    LangStore,
    Pending,
    QuizSession,
    Word,
    check_dictation,
    compare_speech,
    dictation_messages,
    export_csv,
    first_letter,
    lookup_messages,
    parse_import,
    parse_lang,
    parse_lookup,
    parse_phrase,
    parse_reading,
    parse_topic,
    phrase_messages,
    reading_messages,
    split_lang_arg,
)
from ..llm import LLMError
from ..services import ServiceError
from .common import respond, with_user
from .registry import Routes

log = logging.getLogger(__name__)
router = Routes("lang")

CB_WORD = "lw:"  # lw:<действие>:<id слова>
CB_QUIZ = "lq:"  # lq:a:<вариант> · lq:stop · lq:daily
CB_SPEAK = "ls:"  # ls:next:<язык> · ls:stop
CB_READ = "lr:"  # lr:ru · lr:add · lr:tts · lr:more
CB_DICT = "ld:"  # ld:again · ld:next:<язык> · ld:show · ld:stop
QUIZ_DEFAULT = 10
QUIZ_MAX = 30
DICT_CHUNK = 3500
MAX_VOICE_BYTES = 20 * 1024 * 1024
MAX_IMPORT_BYTES = 1024 * 1024


# ---------------------------------------------------------------- общее


def lang_allowed(app: App, turn: Turn) -> bool:
    return turn.registered and turn.user_id in app.settings.lang_users


async def need_lang(message: Message, app: App, turn: Turn) -> bool:
    if lang_allowed(app, turn):
        return True
    await message.answer("🔒 Учитель языков доступен только владельцу бота.")
    return False


def _store(app: App) -> LangStore:
    return app.assistant.lang


def _today(app: App, turn: Turn) -> str:
    return datetime.datetime.now(app.assistant.tz(turn.user)).date().isoformat()


def _e(text: str) -> str:
    return html.escape(text)


def word_card(word: Word, *, note: str = "") -> str:
    lang = LANGS[word.lang]
    lines = [f"{lang.flag} <b>{_e(word.word)}</b> — {_e(word.translation)}"]
    meta = " · ".join(x for x in (word.pos, word.grammar, word.topic_label) if x)
    if meta:
        lines.append(f"<i>{_e(meta)}</i>")
    if word.examples:
        lines.append("")
        for i, ex in enumerate(word.examples, 1):
            ru = f" — <i>{_e(ex['ru'])}</i>" if ex.get("ru") else ""
            lines.append(f"{i}. {_e(ex['text'])}{ru}")
    if word.tip:
        lines += ["", f"💡 {_e(word.tip)}"]
    if note:
        lines += ["", note]
    return "\n".join(lines)


def word_keyboard(app: App, word: Word) -> InlineKeyboardMarkup:
    row = []
    if app.speech is not None:
        row.append(InlineKeyboardButton(text="🔊", callback_data=f"{CB_WORD}tts:{word.id}"))
    row += [
        InlineKeyboardButton(text="✍️ Своё предложение", callback_data=f"{CB_WORD}sent:{word.id}"),
        InlineKeyboardButton(text="🗑", callback_data=f"{CB_WORD}del:{word.id}"),
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


def speech_text(word: Word) -> str:
    """Что прочитать вслух: слово и примеры."""
    return ". ".join([word.word, *(ex["text"] for ex in word.examples)])


async def send_tts(app: App, target: Message, text: str, lang: str) -> None:
    if app.speech is None:
        return
    try:
        audio = await app.speech.synthesize(text, lang=lang)
    except ServiceError as exc:
        log.warning("tts %s failed: %s", lang, exc)
        return
    await target.answer_voice(BufferedInputFile(audio, f"{lang}.ogg"))


async def _ask_model(app: App, messages: list[dict[str, str]]) -> str:
    async with app.queue.slot():
        return await app.llm.chat(app.settings.tutor_model, messages, json_mode=True,
                                  options={"temperature": 0.3})


# ---------------------------------------------------------------- словарь


@router.message(Command("w", "word"))
async def cmd_word(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    forced, query = split_lang_arg(command.args)
    if not query:
        await message.answer(
            "📖 Использование:\n/w Hund — перевод и в словарь\n/w cs pes — явно чешский\n"
            "/w de собака — с русского на немецкий\n\nСловарь: /dict · повторение: /quiz"
        )
        return
    if len(query) > 80:
        await message.answer("Это слишком длинно для словаря — пришли слово или короткое выражение.")
        return
    store = _store(app)
    state = await store.get_state(turn.user_id)
    hint = forced or state.current
    today = _today(app, turn)

    existing = await store.find_word(turn.user_id, hint, query)
    if existing:
        await message.answer(word_card(existing, note="📚 Уже есть в словаре."), parse_mode=ParseMode.HTML,
                             reply_markup=word_keyboard(app, existing))
        await send_tts(app, message, speech_text(existing), existing.lang)
        return

    status = await message.answer("🔎 Ищу…")
    level = await store.get_level(turn.user_id, hint)
    try:
        raw = await _ask_model(app, lookup_messages(query, hint, forced is not None, level))
        entry = parse_lookup(raw, hint, forced is not None)
    except LLMError as exc:
        await status.edit_text(f"⚠️ Не получилось перевести: {exc}")
        return
    word, created = await store.add_word(turn.user_id, entry, today)
    await app.count_usage(turn.user_id)
    total = (await store.counts(turn.user_id, today)).get(word.lang, (0, 0))[0]
    note = (f"📚 Сохранил в словарь ({LANGS[word.lang].name}: {total} сл.). Повторим завтра."
            if created else "📚 Уже есть в словаре.")
    await status.edit_text(word_card(word, note=note), parse_mode=ParseMode.HTML,
                           reply_markup=word_keyboard(app, word))
    await send_tts(app, message, speech_text(word), word.lang)


@router.message(Command("dict"))
async def cmd_dict(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    store = _store(app)
    forced, rest = split_lang_arg(command.args)
    lang = forced or (await store.get_state(turn.user_id)).current
    topic = parse_topic(rest) if len(rest.strip()) > 2 else None
    letter = "" if topic else rest.strip().upper()
    words = await store.list_words(turn.user_id, lang)
    if not words:
        await message.answer(f"{LANGS[lang].flag} Словарь пуст. Добавь слово: /w Hund")
        return
    if topic:
        words = [w for w in words if (w.topic if w.topic in TOPICS else "другое") == topic]
        if not words:
            await message.answer(f"В теме «{topic}» слов нет. Темы: /topics")
            return
    if letter:
        words = [w for w in words if first_letter(lang, w.word) == letter]
        if not words:
            await message.answer(f"На букву {_e(letter)} слов нет.")
            return

    head = f" · {TOPICS[topic]} {topic}" if topic else ""
    lines = [f"{LANGS[lang].flag} <b>Словарь: {LANGS[lang].name}</b>{head} ({len(words)} сл.)"]
    current = None
    for w in words:
        head = first_letter(lang, w.word)
        if head != current:
            current = head
            lines.append(f"\n<b>{_e(head)}</b>")
        mark = " ⚠️" if w.wrong > w.correct else ""
        lines.append(f"<code>{w.id}</code> {_e(w.word)} — {_e(w.translation)}{mark}")
    lines.append("\nКарточка: /w слово · удалить: /wdel номер · буква: /dict de H · тема: /dict de еда · /topics")

    chunk = ""
    for line in lines:
        if len(chunk) + len(line) > DICT_CHUNK:
            await message.answer(chunk, parse_mode=ParseMode.HTML)
            chunk = ""
        chunk += line + "\n"
    await message.answer(chunk, parse_mode=ParseMode.HTML)


@router.message(Command("wdel"))
async def cmd_wdel(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    arg = (command.args or "").strip()
    if not arg.isdigit():
        await message.answer("Использование: /wdel <номер из /dict>")
        return
    ok = await _store(app).delete_word(turn.user_id, int(arg))
    await message.answer("🗑 Удалил из словаря" if ok else "Нет такого слова, см. /dict")


# ---------------------------------------------------------------- экспорт и импорт словаря


@router.message(Command("wexport"))
async def cmd_wexport(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    arg = (command.args or "").strip()
    lang = parse_lang(arg)
    if arg and lang is None:
        await message.answer("Использование: /wexport — весь словарь · /wexport de · /wexport cs")
        return
    words = []
    for code in [lang] if lang else list(LANGS):
        words += await _store(app).list_words(turn.user_id, code)
    if not words:
        await message.answer("📚 Словарь пуст — экспортировать нечего. Добавь слово: /w Hund")
        return
    name = f"slovar-{lang or 'all'}-{_today(app, turn)}.csv"
    await message.answer_document(
        BufferedInputFile(export_csv(words), name),
        caption=f"📤 Словарь: {len(words)} сл. Открывается в Excel / LibreOffice / Google Таблицах. "
                "Вернуть обратно или на другой аккаунт: /wimport с этим файлом.",
    )


def _import_args(args: str | None) -> tuple[str | None, str]:
    """«de\nHund — собака» → ("de", "Hund — собака"); язык — только первым словом."""
    parts = (args or "").strip().split(None, 1)
    if parts and (lang := parse_lang(parts[0])):
        return lang, parts[1] if len(parts) > 1 else ""
    return None, (args or "").strip()


IMPORT_HELP = (
    "📥 <b>Импорт слов в словарь</b>\n"
    "Пришли следующим сообщением список — по слову на строку, перевод через «—», «;», «=» или Tab "
    "(без перевода — переведу сам и добавлю примеры, до {lookup} слов за раз):\n"
    "<code>der Hund — собака\nKatze\nlaufen; бегать</code>\n"
    "или файл <b>.csv / .txt</b> (например, из /wexport). Язык: {flag} {name} "
    "(другой — /wimport cs). Отменить: /cancel"
)


@router.message(Command("wimport"))
async def cmd_wimport(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    """Импорт только этой командой: список в том же сообщении, файл с подписью /wimport или следующим сообщением."""
    if not await need_lang(message, app, turn):
        return
    store = _store(app)
    forced, body = _import_args(command.args)
    lang = forced or (await store.get_state(turn.user_id)).current
    if message.document:
        if (text := await _read_import_file(message, bot)) is not None:
            await run_import(message, app, turn, text, lang)
        return
    if body:
        await run_import(message, app, turn, body, lang)
        return
    store.pending[turn.user_id] = Pending("import", lang, "")
    await message.answer(IMPORT_HELP.format(lookup=MAX_IMPORT_LOOKUP, flag=LANGS[lang].flag, name=LANGS[lang].name),
                         parse_mode=ParseMode.HTML)


async def _read_import_file(message: Message, bot: Bot) -> str | None:
    doc = message.document
    if not (doc.file_name or "").lower().endswith((".csv", ".txt", ".tsv")):
        await message.answer("Импортирую только .csv, .tsv и .txt")
        return None
    if (doc.file_size or 0) > MAX_IMPORT_BYTES:
        await message.answer("Файл больше 1 МБ — раздели его на части.")
        return None
    raw = (await bot.download(doc)).read()
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1251", errors="replace")  # CSV из русского Excel


async def pending_import(message: Message, app: App) -> bool:
    return await _has_pending(message, app, "import")


@router.message(Command("cancel"), pending_import)
async def cancel_import(message: Message, app: App, turn: Turn) -> None:
    _store(app).pending.pop(turn.user_id, None)
    await message.answer("👌 Импорт отменён.")


@router.message(F.document, pending_import)
async def on_import_file(message: Message, bot: Bot, app: App, turn: Turn) -> None:
    pending = _store(app).pending.pop(turn.user_id)
    if (text := await _read_import_file(message, bot)) is not None:
        await run_import(message, app, turn, text, pending.lang)


@router.message(F.text & ~F.text.startswith("/"), pending_import)
async def on_import_text(message: Message, app: App, turn: Turn) -> None:
    pending = _store(app).pending.pop(turn.user_id)
    await run_import(message, app, turn, message.text, pending.lang)


async def run_import(message: Message, app: App, turn: Turn, text: str, lang: str) -> None:
    store = _store(app)
    rows, skipped = parse_import(text, lang)
    if not rows:
        await message.answer("Не нашёл слов. Формат: по слову на строку, перевод через «—» (или файл из /wexport).")
        return
    today = _today(app, turn)
    status = await message.answer(f"📥 Импортирую {len(rows)} сл.…")
    added = existing = failed = lookups = 0
    no_translation = [i for i, r in enumerate(rows) if not r.translation]
    too_many = set(no_translation[MAX_IMPORT_LOOKUP:])  # индексы: одинаковые строки — тоже разные слова
    for i, row in enumerate(rows):
        if i in too_many:
            continue
        if await store.find_word(turn.user_id, row.lang, row.word):
            existing += 1
            continue
        if row.translation:
            entry = row.entry()
        else:
            lookups += 1
            try:
                level = await store.get_level(turn.user_id, row.lang)
                entry = parse_lookup(await _ask_model(app, lookup_messages(row.word, row.lang, True, level)),
                                     row.lang, True)
            except LLMError as exc:
                log.info("import lookup %r failed: %s", row.word, exc)
                failed += 1
                continue
            if lookups % 5 == 0:
                await status.edit_text(f"📥 Импортирую… {i + 1}/{len(rows)} (перевожу слова без перевода)")
        _, created = await store.add_word(turn.user_id, entry, today)
        added += created
        existing += not created
    if lookups:
        await app.count_usage(turn.user_id)
    lines = [f"📥 Импорт: добавлено <b>{added}</b> сл."]
    if existing:
        lines.append(f"Уже были в словаре: {existing}")
    if failed:
        lines.append(f"Не получилось перевести: {failed}")
    if too_many:
        lines.append(f"Без перевода и сверх лимита {MAX_IMPORT_LOOKUP}: {len(too_many)} — "
                     "добавь им перевод или импортируй следующей порцией")
    if skipped:
        lines.append(f"Пропущено строк: {skipped} (пустые, длиннее 100 символов или сверх {MAX_IMPORT})")
    lines.append("Новые слова — в повторение с завтрашнего дня. Словарь: /dict")
    await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML)


@router.callback_query(F.data.startswith(CB_WORD))
async def on_word_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    if not lang_allowed(app, turn) or not isinstance(callback.message, Message):
        await callback.answer("Недоступно", show_alert=True)
        return
    _, action, raw_id = callback.data.split(":", 2)
    store = _store(app)
    word = await store.get_word(turn.user_id, int(raw_id)) if raw_id.isdigit() else None
    if word is None:
        await callback.answer("Этого слова уже нет в словаре", show_alert=True)
        return
    if action == "tts":
        await callback.answer("Озвучиваю…")
        await send_tts(app, callback.message, speech_text(word), word.lang)
    elif action == "sent":
        store.pending[turn.user_id] = Pending("sentence", word.lang, word.word, word.id)
        await callback.answer()
        await callback.message.answer(
            f"✍️ Напиши своё предложение со словом <b>{_e(word.word)}</b> — я проверю и поправлю.",
            parse_mode=ParseMode.HTML,
        )
    elif action == "del":
        await store.delete_word(turn.user_id, word.id)
        await callback.answer("Удалено")
        await callback.message.edit_reply_markup(reply_markup=None)
        await callback.message.answer(f"🗑 {_e(word.word)} удалено из словаря.")
    else:
        await callback.answer()


async def _has_pending(message: Message, app: App, kind: str) -> bool:
    user = message.from_user
    pending = app.assistant.lang.pending.get(user.id) if user else None
    return pending is not None and pending.kind == kind


async def pending_sentence(message: Message, app: App) -> bool:
    return await _has_pending(message, app, "sentence")


async def pending_speak(message: Message, app: App) -> bool:
    return await _has_pending(message, app, "speak")


@router.message(F.text & ~F.text.startswith("/"), pending_sentence)
async def on_sentence(message: Message, app: App, turn: Turn) -> None:
    pending = _store(app).pending.pop(turn.user_id)
    lang = LANGS[pending.lang]
    await _store(app).set_current(turn.user_id, pending.lang)
    prompt = (
        f"Я тренирую слово «{pending.text}» ({lang.name}). Моё предложение: {message.text}\n"
        "Проверь его: исправленный вариант, коротко по-русски объясни каждую ошибку, оцени, "
        "звучит ли естественно, и предложи 1–2 более удачных варианта с этим словом."
    )
    await respond(message, app, with_user(turn, mode="lang"), prompt, extract_memory=False,
                  allow_tools=False)


# ---------------------------------------------------------------- тест по словарю


def _quiz_keyboard(session: QuizSession) -> InlineKeyboardMarkup:
    item = session.current
    rows = [[InlineKeyboardButton(text=opt[:60], callback_data=f"{CB_QUIZ}a:{i}")]
            for i, opt in enumerate(item.options)]
    if item.kind == "self":
        rows = [[InlineKeyboardButton(text=opt, callback_data=f"{CB_QUIZ}a:{i}")
                 for i, opt in enumerate(item.options)]]
    rows.append([InlineKeyboardButton(text="⏹ Закончить", callback_data=f"{CB_QUIZ}stop")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_question(target: Message, app: App, user_id: int) -> None:
    store = _store(app)
    item = await store.next_question(user_id)
    session = store.quizzes[user_id]
    if item is None:
        return
    head = f"❓ {session.index + 1}/{len(session.words)}\n\n"
    await target.answer(head + item.question, parse_mode=ParseMode.HTML,
                        reply_markup=_quiz_keyboard(session))


async def start_quiz(target: Message, app: App, turn: Turn, words: list[Word], *, daily: bool) -> None:
    if not words:
        await target.answer("📚 В словаре пока нет слов. Добавь: /w Hund")
        return
    await _store(app).start_quiz(turn.user_id, words, daily=daily)
    await target.answer(f"📝 Повторяем {len(words)} сл. Поехали!")
    await send_question(target, app, turn.user_id)


async def finish_quiz(target: Message, app: App, turn: Turn) -> None:
    store = _store(app)
    session = store.quizzes.pop(turn.user_id, None)
    if session is None:
        return
    answered = session.correct + len(session.wrong)
    if not answered:
        await target.answer("⏹ Тест остановлен.")
        return
    streak = await store.finish_review(turn.user_id, _today(app, turn))
    lines = [f"🏁 Итог: {session.correct}/{answered} верно", f"🔥 Серия: {streak} дн. подряд"]
    if session.wrong:
        lines += ["", "Повторим завтра:"]
        lines += [f"• {_e(w.word)} — {_e(w.translation)}" for w in session.wrong]
    lines += ["", "Ещё: /quiz · фраза вслух: /speak · новая тема: /lesson"]
    await target.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("quiz"))
async def cmd_quiz(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    lang, rest = split_lang_arg(command.args)
    parts = rest.split()
    count = next((int(p) for p in parts if p.isdigit()), QUIZ_DEFAULT)
    topic = next((t for p in parts if not p.isdigit() and (t := parse_topic(p))), None)
    count = max(1, min(count, QUIZ_MAX))
    words = await _store(app).review_words(turn.user_id, _today(app, turn), count, lang, topic)
    if topic and not words:
        await message.answer(f"В теме «{topic}» пока нет слов. Темы: /topics")
        return
    await start_quiz(message, app, turn, words, daily=False)


@router.callback_query(F.data.startswith(CB_QUIZ))
async def on_quiz_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    msg = callback.message
    if not lang_allowed(app, turn) or not isinstance(msg, Message):
        await callback.answer("Недоступно", show_alert=True)
        return
    store = _store(app)
    action = callback.data.removeprefix(CB_QUIZ)

    if action == "daily":
        await callback.answer()
        await msg.edit_reply_markup(reply_markup=None)
        state = await store.get_state(turn.user_id)
        words = await store.words_by_ids(turn.user_id, state.plan_words)
        await start_quiz(msg, app, turn, words, daily=True)
        return

    if action == "stop":
        await callback.answer()
        await msg.edit_reply_markup(reply_markup=None)
        await finish_quiz(msg, app, turn)
        return

    session = store.quizzes.get(turn.user_id)
    choice = action.removeprefix("a:")
    if session is None or session.current is None or not choice.isdigit():
        await callback.answer("Этот тест уже закончился. /quiz — новый", show_alert=True)
        return
    item = session.current
    session.current = None  # повторное нажатие той же кнопки не засчитываем
    correct = int(choice) == item.answer
    if item.kind == "self":
        correct = int(choice) == 0
    days = await store.record_answer(turn.user_id, item.word, correct, _today(app, turn))
    session.index += 1
    if correct:
        session.correct += 1
        verdict = "✅ Верно!"
    else:
        session.wrong.append(item.word)
        verdict = f"❌ Правильно: <b>{_e(item.options[item.answer])}</b>" if item.kind != "self" else "❌ Повторим"
    await callback.answer("✅" if correct else "❌")

    w = item.word
    example = f"\n{_e(w.examples[0]['text'])} — <i>{_e(w.examples[0].get('ru', ''))}</i>" if w.examples else ""
    reveal = (f"{item.question}\n\n{verdict}\n{LANGS[w.lang].flag} {_e(w.word)} — {_e(w.translation)}"
              f"{example}\n<i>Следующий повтор через {days} дн.</i>")
    tts = (InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text="🔊", callback_data=f"{CB_WORD}tts:{w.id}")]]) if app.speech else None)
    await msg.edit_text(reveal, parse_mode=ParseMode.HTML, reply_markup=tts)

    if session.finished:
        await finish_quiz(msg, app, turn)
    else:
        await send_question(msg, app, turn.user_id)


# ---------------------------------------------------------------- уроки, тесты, разговор


LESSON_PROMPTS = {
    "lesson": (
        "Проведи мне урок ({lang}, мой уровень {level}){topic}. Объясни на русском понятно и с "
        "примерами {lang_in} с переводом, таблица — если уместно. В конце дай 3 коротких упражнения "
        "и жди моих ответов."
    ),
    "test": (
        "Дай мне тест ({lang}, уровень {level}){topic}: 6 пронумерованных заданий разных типов "
        "(перевод в обе стороны, вставь пропущенное слово, выбери правильную форму, "
        "{grammar_hint}). Используй слова из моего словаря. Ответы не показывай — я пришлю свои, "
        "потом проверь каждый и поставь оценку."
    ),
    "talk": (
        "Давай поговорим {lang_in}{topic}. Пиши короткие реплики уровня {level} и в конце каждой — "
        "вопрос мне. После каждой моей реплики сначала коротко по-русски исправь мои ошибки, "
        "потом продолжай разговор. Начни первым."
    ),
}
DEFAULT_TOPICS = {
    "lesson": ": выбери сам следующую полезную тему для моего уровня, учитывая пройденные уроки",
    "test": " по словам и грамматике, которые я недавно учил",
    "talk": " на бытовую тему на твой выбор",
}
GRAMMAR_HINTS = {"de": "артикль и падеж", "cs": "падеж и вид глагола"}


async def _start_lesson(message: Message, command: CommandObject, app: App, turn: Turn, kind: str) -> None:
    if not await need_lang(message, app, turn):
        return
    store = _store(app)
    forced, topic = split_lang_arg(command.args)
    lang_code = forced or (await store.get_state(turn.user_id)).current
    lang = LANGS[lang_code]
    level = await store.get_level(turn.user_id, lang_code)
    await store.set_current(turn.user_id, lang_code)
    await app.db.set_mode(turn.user_id, "lang")
    await store.log_lesson(turn.user_id, lang_code, kind, topic or "на выбор")
    prompt = LESSON_PROMPTS[kind].format(
        lang=lang.name, lang_in=lang.name_in, level=level,
        topic=f" на тему «{topic}»" if topic else DEFAULT_TOPICS[kind],
        grammar_hint=GRAMMAR_HINTS[lang_code],
    )
    await message.answer(f"{lang.flag} Режим «Учитель языков» включён. Вернуться к обычному чату: /mode")
    await respond(message, app, with_user(turn, mode="lang"), prompt,
                  store_text=f"/{kind} {lang_code} {topic}".strip(), extract_memory=False)


@router.message(Command("lesson"))
async def cmd_lesson(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    await _start_lesson(message, command, app, turn, "lesson")


@router.message(Command("test"))
async def cmd_test(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    await _start_lesson(message, command, app, turn, "test")


@router.message(Command("talk"))
async def cmd_talk(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    await _start_lesson(message, command, app, turn, "talk")


# ---------------------------------------------------------------- произношение


def _speak_keyboard(lang: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="➡️ Другая фраза", callback_data=f"{CB_SPEAK}next:{lang}"),
        InlineKeyboardButton(text="✖️ Хватит", callback_data=f"{CB_SPEAK}stop"),
    ]])


async def offer_phrase(target: Message, app: App, turn: Turn, lang: str) -> None:
    store = _store(app)
    words = await store.review_words(turn.user_id, _today(app, turn), 5, lang)
    examples = [(ex["text"], ex.get("ru", ""), w.id) for w in words for ex in w.examples]
    if examples:
        text, ru, word_id = store.rng.choice(examples)
    else:
        level = await store.get_level(turn.user_id, lang)
        try:
            text, ru = parse_phrase(await _ask_model(app, phrase_messages(lang, level, [w.word for w in words])))
        except LLMError as exc:
            await target.answer(f"⚠️ Не получилось придумать фразу: {exc}")
            return
        word_id = None
    store.pending[turn.user_id] = Pending("speak", lang, text, word_id)
    translation = f"\n<i>{_e(ru)}</i>" if ru else ""
    await target.answer(
        f"🗣 {LANGS[lang].flag} Послушай и прочитай вслух — пришли голосовое:\n\n<b>{_e(text)}</b>{translation}",
        parse_mode=ParseMode.HTML, reply_markup=_speak_keyboard(lang),
    )
    await send_tts(app, target, text, lang)


@router.message(Command("speak"))
async def cmd_speak(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    if app.speech is None:
        await message.answer("🎤 Сервис речи выключен (не задан SPEECH_URL).")
        return
    lang = parse_lang(command.args) or (await _store(app).get_state(turn.user_id)).current
    await offer_phrase(message, app, turn, lang)


@router.message(F.voice | F.audio, pending_speak)
async def on_speak_voice(message: Message, bot: Bot, app: App, turn: Turn) -> None:
    pending = _store(app).pending[turn.user_id]
    media = message.voice or message.audio
    if (media.file_size or 0) > MAX_VOICE_BYTES:
        await message.answer("Голосовое слишком большое.")
        return
    status = await message.answer("🎧 Слушаю…")
    data = (await bot.download(media)).read()
    try:
        heard = await app.speech.transcribe(data, "voice.ogg", language=pending.lang)
    except ServiceError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    check = compare_speech(pending.text, heard)
    if check.score >= 90:
        verdict = "🎉 Отлично, всё понятно!"
    elif check.score >= 60:
        verdict = "👍 Хорошо, но есть что подтянуть."
    else:
        verdict = "🔁 Пока непохоже — послушай пример ещё раз и попробуй медленнее."
    lines = [f"🎧 Я услышал: <i>{_e(heard) or '—'}</i>", f"Совпадение: <b>{check.score}%</b> — {verdict}"]
    if check.missed:
        lines.append("Не расслышал: " + ", ".join(f"<b>{_e(w)}</b>" for w in check.missed))
    lines.append("\nМожно прислать ещё раз или взять другую фразу.")
    await status.edit_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=_speak_keyboard(pending.lang))


@router.callback_query(F.data.startswith(CB_SPEAK))
async def on_speak_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    msg = callback.message
    if not lang_allowed(app, turn) or not isinstance(msg, Message):
        await callback.answer("Недоступно", show_alert=True)
        return
    action = callback.data.removeprefix(CB_SPEAK)
    await callback.answer()
    await msg.edit_reply_markup(reply_markup=None)
    if action == "stop":
        _store(app).pending.pop(turn.user_id, None)
        await msg.answer("👌 Закончили с произношением.")
    elif action.startswith("next:") and (lang := action.removeprefix("next:")) in LANGS:
        await offer_phrase(msg, app, turn, lang)


# ---------------------------------------------------------------- диктант


def _dictation_keyboard(lang: str, answered: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="🔊 Ещё раз", callback_data=f"{CB_DICT}again")] if not answered else []
    if not answered:
        row.append(InlineKeyboardButton(text="👀 Показать", callback_data=f"{CB_DICT}show"))
    row += [InlineKeyboardButton(text="➡️ Дальше", callback_data=f"{CB_DICT}next:{lang}"),
            InlineKeyboardButton(text="✖️ Хватит", callback_data=f"{CB_DICT}stop")]
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def offer_dictation(target: Message, app: App, turn: Turn, lang: str) -> None:
    """Фраза только голосом — текст ученик не видит, пока не напишет сам."""
    store = _store(app)
    words = await store.review_words(turn.user_id, _today(app, turn), 5, lang)
    examples = [(ex["text"], ex.get("ru", "")) for w in words for ex in w.examples]
    text = ""
    if not examples or store.rng.random() < 0.5:
        level = await store.get_level(turn.user_id, lang)
        try:
            text, ru = parse_phrase(await _ask_model(app, dictation_messages(lang, level, [w.word for w in words])))
        except LLMError as exc:
            if not examples:
                await target.answer(f"⚠️ Не получилось придумать фразу: {exc}")
                return
    if not text:
        text, ru = store.rng.choice(examples)
    store.pending[turn.user_id] = Pending("dictation", lang, text, None, ru)
    await target.answer(
        f"✍️ {LANGS[lang].flag} <b>Диктант.</b> Послушай и напиши фразу {LANGS[lang].name_in} "
        "одним сообщением — проверю орфографию (регистр и диакритику тоже, знаки препинания — нет).",
        parse_mode=ParseMode.HTML, reply_markup=_dictation_keyboard(lang, answered=False),
    )
    await send_tts(app, target, text, lang)


@router.message(Command("dictation", "diktat"))
async def cmd_dictation(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    if app.speech is None:
        await message.answer("🎤 Сервис речи выключен (не задан SPEECH_URL) — диктант без озвучки не получится.")
        return
    arg = (command.args or "").strip()
    lang = parse_lang(arg) or (await _store(app).get_state(turn.user_id)).current
    if arg and parse_lang(arg) is None:
        await message.answer("Использование: /dictation [de|cs]")
        return
    await offer_dictation(message, app, turn, lang)


async def pending_dictation(message: Message, app: App) -> bool:
    return await _has_pending(message, app, "dictation")


def dictation_report(expected: str, ru: str, check: DictationCheck) -> str:
    if check.score == 100:
        verdict = "🎉 Без ошибок!"
    elif check.score >= 80:
        verdict = "👍 Почти идеально."
    elif check.score >= 50:
        verdict = "📝 Неплохо, но есть над чем поработать."
    else:
        verdict = "🔁 Много ошибок — послушай ещё раз и попробуй следующую фразу."
    lines = [f"Правильно: <b>{_e(expected)}</b>"]
    if ru:
        lines.append(f"<i>{_e(ru)}</i>")
    lines.append(f"\nВерно написано: <b>{check.score}%</b> слов — {verdict}")
    for m in check.mistakes[:15]:
        if m.kind == "пропущено":
            lines.append(f"➖ пропущено: <b>{_e(m.expected)}</b>")
        elif m.kind == "лишнее":
            lines.append(f"➕ лишнее: <s>{_e(m.typed)}</s>")
        else:
            lines.append(f"❌ <s>{_e(m.typed)}</s> → <b>{_e(m.expected)}</b> ({m.kind})")
    return "\n".join(lines)


@router.message(F.text & ~F.text.startswith("/"), pending_dictation)
async def on_dictation(message: Message, app: App, turn: Turn) -> None:
    pending = _store(app).pending.pop(turn.user_id)
    await _store(app).set_current(turn.user_id, pending.lang)
    check = check_dictation(pending.text, message.text)
    await message.answer(dictation_report(pending.text, pending.note, check), parse_mode=ParseMode.HTML,
                         reply_markup=_dictation_keyboard(pending.lang, answered=True))


@router.callback_query(F.data.startswith(CB_DICT))
async def on_dictation_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    msg = callback.message
    if not lang_allowed(app, turn) or not isinstance(msg, Message):
        await callback.answer("Недоступно", show_alert=True)
        return
    store = _store(app)
    pending = store.pending.get(turn.user_id)
    pending = pending if pending and pending.kind == "dictation" else None
    action = callback.data.removeprefix(CB_DICT)
    if action == "again":
        if pending is None:
            await callback.answer("Эта фраза уже проверена", show_alert=True)
            return
        await callback.answer()
        await send_tts(app, msg, pending.text, pending.lang)
        return
    await callback.answer()
    await msg.edit_reply_markup(reply_markup=None)
    if action == "show":
        if pending is not None:
            store.pending.pop(turn.user_id, None)
            note = f"\n<i>{_e(pending.note)}</i>" if pending.note else ""
            await msg.answer(f"👀 Фраза: <b>{_e(pending.text)}</b>{note}", parse_mode=ParseMode.HTML,
                             reply_markup=_dictation_keyboard(pending.lang, answered=True))
    elif action == "stop":
        if pending is not None:
            store.pending.pop(turn.user_id, None)
        await msg.answer("👌 Закончили с диктантом.")
    elif action.startswith("next:") and (lang := action.removeprefix("next:")) in LANGS:
        await offer_dictation(msg, app, turn, lang)


# ---------------------------------------------------------------- темы словаря


@router.message(Command("topics"))
async def cmd_topics(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    store = _store(app)
    lang = parse_lang(command.args) or (await store.get_state(turn.user_id)).current
    counts = await store.topic_counts(turn.user_id, lang)
    lines = [f"{LANGS[lang].flag} <b>Темы словаря</b>", ""]
    lines += [f"{icon} {topic}: {counts.get(topic, 0)}" for topic, icon in TOPICS.items() if counts.get(topic)]
    if not counts:
        lines.append("Словарь пуст. Добавь слово: /w Hund")
    lines += ["", "Слова темы: /dict de еда · тест: /quiz de еда · текст: /read de еда",
              "Поменять тему слова: /wtopic номер тема", "Все темы: " + ", ".join(TOPICS)]
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("wtopic"))
async def cmd_wtopic(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    num, _, raw = (command.args or "").strip().partition(" ")
    topic = parse_topic(raw)
    if not num.isdigit() or topic is None:
        await message.answer("Использование: /wtopic <номер из /dict> <тема>\nТемы: " + ", ".join(TOPICS))
        return
    ok = await _store(app).set_topic(turn.user_id, int(num), topic)
    await message.answer(f"{TOPICS[topic]} Тема: {topic}" if ok else "Нет такого слова, см. /dict")


# ---------------------------------------------------------------- тексты для чтения


def _read_keyboard(app: App, has_words: bool) -> InlineKeyboardMarkup:
    row = [InlineKeyboardButton(text="🇷🇺 Перевод", callback_data=f"{CB_READ}ru")]
    if app.speech is not None:
        row.append(InlineKeyboardButton(text="🔊", callback_data=f"{CB_READ}tts"))
    rows = [row]
    if has_words:
        rows.append([InlineKeyboardButton(text="➕ Новые слова в словарь", callback_data=f"{CB_READ}add")])
    rows.append([InlineKeyboardButton(text="➡️ Ещё текст", callback_data=f"{CB_READ}more")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def send_reading(target: Message, app: App, turn: Turn, lang: str, topic: str | None) -> None:
    store = _store(app)
    level = await store.get_level(turn.user_id, lang)
    topic = topic or store.rng.choice([t for t in TOPICS if t != "другое"])
    known = [w.word for w in await store.review_words(turn.user_id, _today(app, turn), 8, lang,
                                                      topic if topic != "другое" else None)]
    status = await target.answer(f"📖 Пишу текст ({LANGS[lang].name}, {level}, тема «{topic}»)…")
    try:
        async with app.queue.slot():
            raw = await app.llm.chat(app.settings.tutor_model, reading_messages(lang, level, topic, known),
                                     json_mode=True, options={"temperature": 0.7})
        reading = parse_reading(raw, lang, topic)
    except LLMError as exc:
        await status.edit_text(f"⚠️ Не получилось: {exc}")
        return
    store.readings[turn.user_id] = reading
    await store.log_lesson(turn.user_id, lang, "read", topic)
    await store.set_current(turn.user_id, lang)
    lines = [f"{LANGS[lang].flag} <b>{_e(reading.title)}</b> · {TOPICS.get(topic, '📦')} {topic}, {level}", "",
             _e(reading.text)]
    if reading.new_words:
        lines += ["", "<b>Новые слова</b>"]
        lines += [f"• {_e(w['word'])} — {_e(w['translation'])}" for w in reading.new_words]
    if reading.questions:
        lines += ["", "<b>Вопросы</b> — ответь мне на языке, я проверю:"]
        lines += [f"{i}. {_e(q)}" for i, q in enumerate(reading.questions, 1)]
    await status.edit_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML,
                           reply_markup=_read_keyboard(app, bool(reading.new_words)))
    # Текст — в историю диалога и режим учителя: ответы на вопросы бот проверит с контекстом
    await app.db.add_message(turn.chat_id, "assistant",
                             f"Текст для чтения «{reading.title}»:\n{reading.text}\n\nВопросы:\n"
                             + "\n".join(reading.questions))
    await app.db.set_mode(turn.user_id, "lang")
    await app.count_usage(turn.user_id)


@router.message(Command("read"))
async def cmd_read(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    forced, rest = split_lang_arg(command.args)
    lang = forced or (await _store(app).get_state(turn.user_id)).current
    await send_reading(message, app, turn, lang, parse_topic(rest) if rest else None)


@router.callback_query(F.data.startswith(CB_READ))
async def on_read_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    msg = callback.message
    store = _store(app)
    reading = store.readings.get(turn.user_id)
    if not lang_allowed(app, turn) or not isinstance(msg, Message) or reading is None:
        await callback.answer("Этот текст уже устарел — /read", show_alert=True)
        return
    action = callback.data.removeprefix(CB_READ)
    if action == "ru":
        await callback.answer()
        await msg.answer(f"🇷🇺 <i>{_e(reading.ru or 'перевода нет')}</i>", parse_mode=ParseMode.HTML)
    elif action == "tts":
        await callback.answer("Озвучиваю…")
        await send_tts(app, msg, reading.text, reading.lang)
    elif action == "add":
        today, added = _today(app, turn), []
        for w in reading.new_words:
            entry = {"lang": reading.lang, "word": w["word"], "translation": w["translation"], "pos": w["pos"],
                     "topic": reading.topic, "examples": [
                         {"text": s.strip(), "ru": ""} for s in reading.text.replace("!", ".").split(".")
                         if w["word"].split()[-1].casefold()[:5] in s.casefold()][:1]}
            _, created = await store.add_word(turn.user_id, entry, today)
            if created:
                added.append(w["word"])
        await callback.answer(f"Добавлено: {len(added)}")
        await msg.answer(f"📚 В словарь ({reading.topic}): {', '.join(added)}" if added else "Эти слова уже в словаре 🙂")
    elif action == "more":
        await callback.answer()
        await send_reading(msg, app, turn, reading.lang, reading.topic)
    else:
        await callback.answer()


# ---------------------------------------------------------------- настройки


@router.message(Command("lang"))
async def cmd_lang(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_lang(message, app, turn):
        return
    store = _store(app)
    args = (command.args or "").split()
    if args and args[0].lower() == "daily" and len(args) == 2 and args[1].lower() in ("on", "off"):
        await store.set_daily(turn.user_id, args[1].lower() == "on")
        await message.answer("🔔 Ежедневное повторение включено" if args[1].lower() == "on"
                             else "🔕 Ежедневное повторение выключено")
        return
    if args:
        lang = parse_lang(args[0])
        level = args[1].upper() if len(args) > 1 else None
        if lang is None or (level and level not in LEVELS):
            await message.answer("Использование: /lang de · /lang cs A1 · /lang daily on|off\n"
                                 f"Уровни: {', '.join(LEVELS)}")
            return
        await store.set_current(turn.user_id, lang)
        if level:
            await store.set_level(turn.user_id, lang, level)
        level = level or await store.get_level(turn.user_id, lang)
        await message.answer(f"{LANGS[lang].flag} Сейчас учим: {LANGS[lang].name}, уровень {level}")
        return

    state = await store.get_state(turn.user_id)
    today = _today(app, turn)
    counts = await store.counts(turn.user_id, today)
    lines = ["🎓 <b>Учитель языков</b>", ""]
    for code, lang in LANGS.items():
        total, due = counts.get(code, (0, 0))
        mark = " ← сейчас" if code == state.current else ""
        level = await store.get_level(turn.user_id, code)
        lines.append(f"{lang.flag} {lang.name}: уровень {level}, слов {total}, к повторению {due}{mark}")
    lines.append(f"\n🔥 Серия: {state.streak} дн.")
    if state.daily:
        s = app.settings
        when = "уже было" if state.plan_sent and state.plan_day == today else (
            state.plan_at.astimezone(app.assistant.tz(turn.user)).strftime("%H:%M")
            if state.plan_at and state.plan_day == today else "будет назначено")
        lines.append(f"🔔 Повторение каждый день в случайное время {s.lang_daily_from}:00–"
                     f"{s.lang_daily_to}:00, {s.lang_daily_min}–{s.lang_daily_max} слов "
                     f"(сегодня: {when})")
    else:
        lines.append("🔕 Ежедневное повторение выключено (/lang daily on)")
    lines += [
        "", "<b>Команды</b>",
        "/w слово — перевод, примеры, озвучка → в словарь",
        "/dict [de|cs] [буква|тема] — словарь по алфавиту · /wdel номер",
        "/wexport [de|cs] — словарь в CSV · /wimport [de|cs] — импорт списка слов",
        "/topics — темы словаря · /wtopic номер тема",
        "/read [de|cs] [тема] — текст своего уровня с новыми словами и вопросами",
        "/quiz [de|cs] [тема] [N] — тест по своим словам (или по теме)",
        "/lesson [de|cs] [тема] — урок · /test — тест по грамматике",
        "/talk [de|cs] [тема] — разговорная практика",
        "/speak [de|cs] — произношение (голосовым)",
        "/dictation [de|cs] — диктант: слушаешь фразу и пишешь, я проверяю орфографию",
        "/lang de B1 — язык и уровень · /lang daily on|off",
    ]
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)

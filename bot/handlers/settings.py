"""Команды настроек: /start /help /whoami /reset /mode /model /settings /persona /tz."""

import html
import zoneinfo

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..app import App
from ..assistant import Turn
from ..llm import LLMError
from ..modes import LENGTHS, MODES, PERSONA_PRESETS, TEMPERATURES, get_mode
from .common import need_registered
from .registry import Routes

router = Routes("settings")

MODEL_CB = "m:"
AUTO_MODEL = "__auto__"
MODE_CB = "mode:"
SETTINGS_CB = "st:"
PERSONA_CB = "ps:"
MAX_PERSONA = 600

HELP_TEXT = (
    "🐰 <b>Fox AI</b> — кролик Фоксик, твой локальный ассистент.\n\n"
    "Пиши текстом, голосом 🎤 или присылай фото 🖼 — я помню диалог и важное о тебе.\n\n"
    "<b>Настройки</b>\n"
    "/mode — 💬 общение · 🧑‍💻 42 / код · 🎓 защита\n"
    "/model — модель (или 🤖 авто)\n"
    "/settings — температура, длина ответов, голосовые ответы\n"
    "/persona — роль бота (шеф, ментор, друг…)\n"
    "/tz — часовой пояс\n\n"
    "<b>42</b>\n"
    "Файл <code>.c</code>/<code>.h</code> — norminette и объяснение ошибок\n"
    "Файл <code>.zip</code> или /project &lt;git-ссылка&gt; — проверка проекта целиком\n"
    "/run [аргументы] · /valgrind · /asan — запустить последний код\n"
    "/tests — сгенерировать и прогнать тесты\n"
    "/defense — тренировка защиты · /42 — профиль в интре\n\n"
    "<b>Инструменты</b>\n"
    "/search — поиск в интернете · /draw — нарисовать картинку\n"
    "/py — вычисления и графики на Python\n"
    "Ссылка или /sum &lt;ссылка&gt; — пересказ статьи или YouTube\n"
    "/remind — напоминание · /reminders — список\n"
    "/morning — утренняя сводка: погода, дела, blackhole, новости\n\n"
    "<b>Кухня</b> (в группе — общая)\n"
    "/list молоко, хлеб — список покупок · /menu [дней] [пожелания] — меню и покупки\n"
    "/save — сохранить рецепт (ответом или последний ответ) · /recipes · /recipe N\n\n"
    "<b>Память и документы</b>\n"
    "/remember · /memories · /forget\n"
    "PDF/TXT/MD — в личные документы, /docs — список\n"
    "/export [pdf] — диалог в файл · /ics — напоминания в календарь\n"
    "/reset — забыть текущий диалог\n\n"
    "В группах: упомяни @бота, ответь на его сообщение или /ask."
)
LANG_HELP = (
    "\n\n<b>Учитель языков 🇩🇪🇨🇿</b>\n"
    "/w слово — перевод, примеры и озвучка, слово уходит в словарь\n"
    "/dict — словарь по алфавиту · /topics — темы · /quiz [тема] — повторение\n"
    "/wexport — словарь в CSV · /wimport — импорт списка слов\n"
    "/read [тема] — текст для чтения своего уровня\n"
    "/lesson · /test · /talk — урок, тест, разговор · /speak — произношение · /dictation — диктант\n"
    "/lang — уровень, статистика, ежедневное повторение"
)
AQUARIUM_HELP = (
    "\n\n<b>Аквариум 🐠</b>\n"
    "/aq — меню: задачи, график ухода, статистика · /tank — что я знаю · /water — тесты воды\n"
    "/aqmembers — кто ухаживает · /aqinvite — позвать помощника"
)
AQUARIUM_START_HELP = (
    "\n\n<b>Аквариум 🐠</b>\n"
    "/aqstart 30 — завести свои аквариумы: график ухода, напоминания, тесты воды, советы\n"
    "/aqjoin КОД — присоединиться к чужим аквариумам (код — у хозяина, /aqinvite)"
)
ADMIN_HELP = (
    "\n\n<b>Админ</b>\n"
    "/adduser &lt;id&gt; [имя] · /deluser &lt;id&gt; · /users\n"
    "/status — GPU, очередь, модели · /backup — бэкап БД\n"
    "/pull &lt;модель&gt; · /rm &lt;модель&gt; · /bench — скачать, удалить, сравнить скорость\n"
    "/logs &lt;сервис&gt; · /restart &lt;сервис&gt; · /ps · /power [Вт] — через агент хоста\n"
    "/reindex — переиндексировать базу знаний\n"
    "Документ с подписью <code>/kb</code> — в общую базу знаний\n"
    "/allowchat · /denychat — пустить/убрать всех участников группы"
)


@router.message(CommandStart())
@router.message(Command("help"))
async def cmd_start(message: Message, app: App, turn: Turn, is_admin: bool) -> None:
    s = app.settings
    lang = LANG_HELP if turn.registered and turn.user_id in s.lang_users else ""
    aquarium = ""
    if turn.registered and app.assistant.aquariums is not None:
        has_home = await app.assistant.aquariums.for_user(turn.user_id) is not None
        aquarium = AQUARIUM_HELP if has_home else AQUARIUM_START_HELP
    await message.answer(HELP_TEXT + lang + aquarium + (ADMIN_HELP if is_admin else ""),
                         parse_mode=ParseMode.HTML)


@router.message(Command("whoami"))
async def cmd_whoami(message: Message, app: App, turn: Turn) -> None:
    s, user = app.settings, turn.user
    mode = app.assistant.mode_for(turn)
    model = (user.model if user else None) or ("🤖 авто" if s.auto_model else s.default_model)
    lines = [f"ID: <code>{turn.user_id}</code>", f"Режим: {mode.title}",
             f"Модель: <code>{html.escape(model)}</code>"]
    if user:
        facts = len(await app.assistant.memory.list_facts(turn.user_id))
        docs = len(await app.assistant.docs.list_docs(turn.user_id))
        temp = user.temperature if user.temperature is not None else "по умолчанию"
        lines += [
            f"Персона: {html.escape(user.persona[:80]) if user.persona else '—'}",
            f"Температура: {temp} · Длина: {LENGTHS.get(user.length or 'normal', ('?',))[0]}",
            f"Голосовые ответы: {'вкл' if user.voice_reply else 'выкл'}",
            f"Часовой пояс: {app.assistant.tz(user)}",
            f"Фактов в памяти: {facts} · Документов: {docs}",
            f"Интра: {html.escape(user.intra_login) if user.intra_login else '—'}",
        ]
        allowed, left = await app.check_limit(turn.user_id)
        if left >= 0:
            lines.append(f"Осталось запросов сегодня: {left}")
    else:
        lines.append("Ты гость этой группы — личные настройки и память недоступны.")
    lines.append(f"Интернет: {'вкл' if app.assistant.web else 'выкл'}")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("reset"))
async def cmd_reset(message: Message, app: App, turn: Turn) -> None:
    count = await app.db.clear_history(turn.chat_id)
    await message.answer(f"🧹 Диалог очищен ({count} сообщ.). Факты из /memories остались.")


# ---------------------------------------------------------------- режим


def _mode_allowed(app: App, turn: Turn, key: str) -> bool:
    """Закрытые режимы (учитель языков, аквариум) — только тем, кому они разрешены."""
    audience = MODES[key].audience
    if audience == "lang":
        return turn.user_id in app.settings.lang_users
    if audience == "aquarium":
        return turn.registered and app.assistant.aquariums is not None
    return True


@router.message(Command("mode"))
async def cmd_mode(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    current = app.assistant.mode_for(turn)
    buttons = [
        [InlineKeyboardButton(
            text=("✅ " if m.key == current.key else "") + m.title, callback_data=MODE_CB + m.key
        )]
        for m in MODES.values()
        if _mode_allowed(app, turn, m.key)
    ]
    await message.answer(
        f"Текущий режим: {current.title}\nВыбери:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith(MODE_CB))
async def on_mode_chosen(callback: CallbackQuery, app: App, turn: Turn) -> None:
    key = callback.data.removeprefix(MODE_CB)
    if key not in MODES or not turn.registered or not _mode_allowed(app, turn, key):
        await callback.answer("Недоступно", show_alert=True)
        return
    await app.db.set_mode(turn.user_id, key)
    await callback.answer("Готово")
    hint = {
        "defense": "\nПришли код или проект и нажми /defense, чтобы начать.",
        "lang": "\nПиши на немецком или чешском — поправлю ошибки. Команды: /lang",
        "aquarium": "\nСпрашивай про аквариум или пришли фото. Меню: /aq",
    }.get(key, "")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"✅ Режим: {MODES[key].title}{hint}")


# ---------------------------------------------------------------- модель


@router.message(Command("model"))
async def cmd_model(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    try:
        models = await app.llm.list_models()
    except LLMError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    embed = app.settings.embed_model.split(":")[0]
    models = [m for m in models if m.split(":")[0] != embed]  # модель эмбеддингов не для чата
    if not models:
        await message.answer("Нет скачанных моделей. На сервере: ollama pull <модель>")
        return

    s = app.settings
    chosen = turn.user.model if turn.user else None
    current = chosen or ("🤖 авто" if s.auto_model else s.default_model)
    buttons = []
    if s.auto_model:
        buttons.append([InlineKeyboardButton(
            text=("✅ " if chosen is None else "") + "🤖 Авто (по запросу)",
            callback_data=MODEL_CB + AUTO_MODEL,
        )])
    buttons += [
        [InlineKeyboardButton(text=("✅ " if name == chosen else "") + name, callback_data=MODEL_CB + name)]
        for name in models
        if len((MODEL_CB + name).encode()) <= 64  # лимит Telegram на callback_data
    ]
    await message.answer(
        f"Текущая модель: <code>{html.escape(current)}</code>\nВыбери:",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith(MODEL_CB))
async def on_model_chosen(callback: CallbackQuery, app: App, turn: Turn) -> None:
    if not turn.registered:
        await callback.answer("Недоступно", show_alert=True)
        return
    name = callback.data.removeprefix(MODEL_CB)
    if name == AUTO_MODEL:
        await app.db.set_model(turn.user_id, None)
        label = "🤖 авто"
    else:
        try:
            models = await app.llm.list_models()
        except LLMError as exc:
            await callback.answer(str(exc), show_alert=True)
            return
        if name not in models:
            await callback.answer("Эта модель больше недоступна", show_alert=True)
            return
        await app.db.set_model(turn.user_id, name)
        label = name
    await callback.answer("Готово")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(
            f"✅ Модель: <code>{html.escape(label)}</code>", parse_mode=ParseMode.HTML
        )


# ---------------------------------------------------------------- /settings


def _settings_view(app: App, user) -> tuple[str, InlineKeyboardMarkup]:
    temp = user.temperature
    length = user.length or "normal"
    text = (
        "⚙️ <b>Настройки ответов</b>\n"
        f"Температура: {temp if temp is not None else 'по умолчанию модели'}\n"
        f"Длина: {LENGTHS[length][0] if length in LENGTHS else length}\n"
        f"Голосовые ответы на голосовые: {'вкл' if user.voice_reply else 'выкл'}"
    )
    mark = "✅ "
    rows = [
        [InlineKeyboardButton(text=(mark if temp == v else "") + f"{label} ({v})",
                              callback_data=f"{SETTINGS_CB}t:{v}")
         for label, v in TEMPERATURES.items()],
        [InlineKeyboardButton(text=(mark if length == key else "") + label,
                              callback_data=f"{SETTINGS_CB}l:{key}")
         for key, (label, _) in LENGTHS.items()],
        [InlineKeyboardButton(
            text=f"🔊 Голосовые ответы: {'вкл' if user.voice_reply else 'выкл'}",
            callback_data=f"{SETTINGS_CB}v",
        )],
        [InlineKeyboardButton(text="↩️ Сбросить", callback_data=f"{SETTINGS_CB}reset")],
    ]
    if app.speech is None:
        rows.pop(2)
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


@router.message(Command("settings"))
async def cmd_settings(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    text, markup = _settings_view(app, turn.user)
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@router.callback_query(F.data.startswith(SETTINGS_CB))
async def on_settings(callback: CallbackQuery, app: App, turn: Turn) -> None:
    if not turn.registered:
        await callback.answer("Недоступно", show_alert=True)
        return
    action = callback.data.removeprefix(SETTINGS_CB)
    db, uid = app.db, turn.user_id
    if action.startswith("t:"):
        value = float(action[2:])
        if value not in TEMPERATURES.values():
            await callback.answer("?", show_alert=True)
            return
        await db.set_user_field(uid, "temperature", value)
    elif action.startswith("l:") and action[2:] in LENGTHS:
        await db.set_user_field(uid, "length", action[2:])
    elif action == "v":
        await db.set_user_field(uid, "voice_reply", 0 if turn.user.voice_reply else 1)
    elif action == "reset":
        for field in ("temperature", "length"):
            await db.set_user_field(uid, field, None)
        await db.set_user_field(uid, "voice_reply", 0)
    else:
        await callback.answer("?", show_alert=True)
        return
    await callback.answer("Сохранил")
    text, markup = _settings_view(app, await db.get_user(uid))
    if isinstance(callback.message, Message):
        await callback.message.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


# ---------------------------------------------------------------- /persona


@router.message(Command("persona"))
async def cmd_persona(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    arg = (command.args or "").strip()
    if arg.lower() in ("off", "выкл", "нет", "сброс"):
        await app.db.set_user_field(turn.user_id, "persona", None)
        await message.answer("🎭 Персона сброшена.")
        return
    if arg:
        persona = PERSONA_PRESETS.get(arg.lower(), arg)[:MAX_PERSONA]
        await app.db.set_user_field(turn.user_id, "persona", persona)
        await message.answer(f"🎭 Готово! Теперь я: {persona}")
        return
    current = turn.user.persona
    buttons = [
        [InlineKeyboardButton(text=name.capitalize(), callback_data=PERSONA_CB + name)]
        for name in PERSONA_PRESETS
    ] + [[InlineKeyboardButton(text="Без персоны", callback_data=PERSONA_CB + "off")]]
    await message.answer(
        f"🎭 Текущая персона: {current or '—'}\n\n"
        "Выбери готовую или задай свою: <code>/persona ты пират, говоришь «арр»</code>",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith(PERSONA_CB))
async def on_persona(callback: CallbackQuery, app: App, turn: Turn) -> None:
    if not turn.registered:
        await callback.answer("Недоступно", show_alert=True)
        return
    key = callback.data.removeprefix(PERSONA_CB)
    persona = None if key == "off" else PERSONA_PRESETS.get(key)
    if key != "off" and persona is None:
        await callback.answer("?", show_alert=True)
        return
    await app.db.set_user_field(turn.user_id, "persona", persona)
    await callback.answer("Готово")
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"🎭 Персона: {persona or '—'}")


# ---------------------------------------------------------------- /tz


@router.message(Command("tz"))
async def cmd_tz(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    arg = (command.args or "").strip()
    if not arg:
        await message.answer(
            f"🕐 Сейчас: {app.assistant.tz(turn.user)}\n"
            "Сменить: /tz Europe/Paris (или Europe/Moscow, Asia/Almaty…)"
        )
        return
    try:
        zoneinfo.ZoneInfo(arg)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        await message.answer("Не знаю такой пояс. Пример: Europe/Paris")
        return
    await app.db.set_user_field(turn.user_id, "tz", arg)
    await message.answer(f"🕐 Часовой пояс: {arg}")


def mode_title(app: App, key: str | None) -> str:
    return get_mode(key, app.settings.default_mode).title

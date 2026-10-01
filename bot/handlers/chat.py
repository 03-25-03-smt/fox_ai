"""Диалог: текст, голосовые, фото, кнопки под ответом."""

import base64
import html
import logging

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..access import strip_mention
from ..app import App
from ..assistant import Turn
from ..llm import LLMError
from ..services import ServiceError
from ..summarize import only_link
from .common import (
    CB_ALT,
    CB_ALT_MODEL,
    CB_MORE,
    CB_REGEN,
    CB_TTS,
    MORE_PROMPT,
    respond,
    send_voice,
)
from .registry import Routes
from .tools import summarize_link

log = logging.getLogger(__name__)
router = Routes("chat")

MAX_VOICE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_BYTES = 10 * 1024 * 1024
IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
VISION_DEFAULT_PROMPT = "Опиши, что на изображении, и ответь на возможный вопрос по нему."


async def on_text(message: Message, app: App, turn: Turn) -> None:
    text = strip_mention(message.text, app.bot_username) if turn.is_group else message.text
    if not text:
        await message.answer("Слушаю 🦊")
        return
    if app.settings.auto_summary and (url := only_link(text)):
        # Прислали просто ссылку — пересказываем; короткая приписка становится вопросом
        await summarize_link(message, app, turn, url, text.replace(url, "").strip())
        return
    await respond(message, app, turn, text)


@router.message(Command("ask"))
async def cmd_ask(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not command.args:
        await message.answer("Использование: /ask вопрос")
        return
    await respond(message, app, turn, command.args)


# ---------------------------------------------------------------- голос


@router.message(F.voice | F.audio | F.video_note)
async def on_voice(message: Message, bot: Bot, app: App, turn: Turn) -> None:
    if app.speech is None:
        await message.answer("🎤 Распознавание речи выключено (не задан SPEECH_URL).")
        return
    media = message.voice or message.audio or message.video_note
    if (media.file_size or 0) > MAX_VOICE_BYTES:
        await message.answer("Аудио больше 20 МБ — Telegram не даст его скачать боту.")
        return
    status = await message.answer("🎤 Слушаю…")
    data = (await bot.download(media)).read()
    try:
        text = await app.speech.transcribe(data, getattr(media, "file_name", None) or "voice.ogg")
    except ServiceError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    if not text:
        await status.edit_text("🎤 Не расслышал, попробуй ещё раз.")
        return
    await status.edit_text(f"🎤 <i>{html.escape(text)}</i>", parse_mode=ParseMode.HTML)
    voice_reply = bool(turn.user and turn.user.voice_reply)
    await respond(message, app, turn, text, voice=voice_reply)


# ---------------------------------------------------------------- фото


async def answer_image(message: Message, app: App, turn: Turn, data: bytes, caption: str | None) -> None:
    question = (strip_mention(caption, app.bot_username) if caption else "") or VISION_DEFAULT_PROMPT
    model = app.settings.vision_model
    try:
        available = await app.llm.list_models(cached=True)
    except LLMError:
        available = None
    if available is not None and model not in available and f"{model}:latest" not in available:
        await message.answer(
            f"🖼 Vision-модель {model} не скачана. На сервере: ollama pull {model}"
        )
        return
    await respond(
        message, app, turn, question,
        model=model,
        images=[base64.b64encode(data).decode()],
        store_text=f"[фото] {question}",
        allow_tools=False,  # vision-модели обычно не умеют инструменты
        extract_memory=bool(caption),
    )


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot, app: App, turn: Turn) -> None:
    photo = message.photo[-1]  # самое большое разрешение
    if (photo.file_size or 0) > MAX_IMAGE_BYTES:
        await message.answer("Фото слишком большое.")
        return
    data = (await bot.download(photo)).read()
    await answer_image(message, app, turn, data, message.caption)


# ---------------------------------------------------------------- кнопки под ответом


async def _last_exchange(app: App, turn: Turn, callback: CallbackQuery):
    """Последняя пара вопрос-ответ, если кнопка нажата под последним ответом."""
    last = await app.db.last_messages(turn.chat_id, 2)
    msg = callback.message
    if (
        len(last) != 2 or last[0].role != "user" or last[1].role != "assistant"
        or not isinstance(msg, Message) or last[1].tg_msg_id != msg.message_id
    ):
        await callback.answer("Это можно сделать только с последним ответом", show_alert=True)
        return None
    return last


def _question_text(stored: str, turn: Turn) -> str:
    prefix = f"[{turn.author}]: "
    return stored[len(prefix):] if turn.is_group and stored.startswith(prefix) else stored


@router.callback_query(F.data == CB_REGEN)
async def on_regenerate(callback: CallbackQuery, app: App, turn: Turn) -> None:
    last = await _last_exchange(app, turn, callback)
    if last is None:
        return
    await callback.answer("Перегенерирую")
    await callback.message.edit_reply_markup(reply_markup=None)
    await app.db.delete_messages([m.id for m in last])
    await respond(callback.message, app, turn, _question_text(last[0].content, turn), extract_memory=False)


@router.callback_query(F.data == CB_MORE)
async def on_more(callback: CallbackQuery, app: App, turn: Turn) -> None:
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
        await respond(callback.message, app, turn, MORE_PROMPT, extract_memory=False)


@router.callback_query(F.data == CB_ALT)
async def on_alt(callback: CallbackQuery, app: App, turn: Turn) -> None:
    if await _last_exchange(app, turn, callback) is None:
        return
    try:
        models = await app.llm.list_models(cached=True)
    except LLMError as exc:
        await callback.answer(str(exc), show_alert=True)
        return
    embed = app.settings.embed_model.split(":")[0]
    buttons = [
        [InlineKeyboardButton(text=name, callback_data=CB_ALT_MODEL + name)]
        for name in models
        if name.split(":")[0] != embed and len((CB_ALT_MODEL + name).encode()) <= 64
    ]
    await callback.answer()
    await callback.message.answer(
        "Какой моделью ответить на тот же вопрос?",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons),
    )


@router.callback_query(F.data.startswith(CB_ALT_MODEL))
async def on_alt_model(callback: CallbackQuery, app: App, turn: Turn) -> None:
    model = callback.data.removeprefix(CB_ALT_MODEL)
    last = await app.db.last_messages(turn.chat_id, 2)
    if len(last) != 2 or last[0].role != "user":
        await callback.answer("Нечего переспрашивать", show_alert=True)
        return
    await callback.answer(model)
    if isinstance(callback.message, Message):
        await callback.message.edit_text(f"🔀 Отвечает {model}")
    await app.db.delete_messages([m.id for m in last])
    await respond(callback.message, app, turn, _question_text(last[0].content, turn),
                  model=model, extract_memory=False)


@router.callback_query(F.data == CB_TTS)
async def on_tts(callback: CallbackQuery, app: App, turn: Turn) -> None:
    msg = callback.message
    if not isinstance(msg, Message):
        await callback.answer()
        return
    stored = await app.db.message_by_tg_id(turn.chat_id, msg.message_id)
    text = stored.content if stored else (msg.text or "")
    if not text:
        await callback.answer("Нечего озвучивать", show_alert=True)
        return
    await callback.answer("Озвучиваю…")
    await send_voice(app, msg, text)

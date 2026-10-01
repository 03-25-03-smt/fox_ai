"""Общее для хендлеров: отправка длинных ответов, стрим генерации, кнопки под ответом."""

import asyncio
import dataclasses
import logging
import time
from typing import Any

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)

from ..app import App
from ..assistant import Turn
from ..formatting import md_to_html, split_markdown, strip_think
from ..llm import LLMError
from ..services import ServiceError

log = logging.getLogger(__name__)

EDIT_INTERVAL = 1.5  # секунд между обновлениями сообщения во время генерации
PREVIEW_LIMIT = 3800  # Telegram: максимум 4096 символов в сообщении

# callback_data кнопок под ответом
CB_REGEN = "a:rg"
CB_MORE = "a:more"
CB_ALT = "a:alt"
CB_TTS = "a:tts"
CB_ALT_MODEL = "am:"

MORE_PROMPT = "Расскажи подробнее о своём последнем ответе: больше деталей и примеров."


def answer_keyboard(app: App, *, regenerate: bool = True) -> InlineKeyboardMarkup:
    row = []
    if regenerate:
        row += [
            InlineKeyboardButton(text="🔄", callback_data=CB_REGEN),
            InlineKeyboardButton(text="📖 Подробнее", callback_data=CB_MORE),
            InlineKeyboardButton(text="🔀 Другая модель", callback_data=CB_ALT),
        ]
    if app.speech is not None:
        row.append(InlineKeyboardButton(text="🔊", callback_data=CB_TTS))
    return InlineKeyboardMarkup(inline_keyboard=[row]) if row else None


async def need_registered(message: Message, turn: Turn) -> bool:
    if turn.registered:
        return True
    await message.answer("Эта команда доступна только пользователям с личным доступом к боту.")
    return False


async def safe_edit(message: Message, text: str, parse_mode: str | None = None) -> bool:
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


def preview(text: str, status: str = "") -> str:
    if len(text) > PREVIEW_LIMIT:
        text = "…" + text[-PREVIEW_LIMIT:]
    head = f"{status}\n\n" if status else ""
    return head + (text + " ▌" if text else "…")


async def send_long(
    target: Message,
    text: str,
    *,
    edit: bool = True,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> Message:
    """Отправляет Markdown-текст частями. edit=True — первая часть заменяет target
    (плейсхолдер). Клавиатура вешается на последнюю часть. Возвращает последнее сообщение."""
    chunks = split_markdown(text)
    last = target
    for i, chunk in enumerate(chunks):
        markup = reply_markup if i == len(chunks) - 1 else None
        for body, mode in ((md_to_html(chunk), ParseMode.HTML), (chunk, None)):
            try:
                if i == 0 and edit:
                    result = await target.edit_text(body, parse_mode=mode, reply_markup=markup)
                    last = result if isinstance(result, Message) else target
                else:
                    last = await target.answer(body, parse_mode=mode, reply_markup=markup)
                break
            except TelegramBadRequest as exc:
                if "not modified" in str(exc):
                    break
                log.warning("send failed (%s), fallback to plain text", exc)
    return last


async def send_voice(app: App, target: Message, text: str) -> None:
    if app.speech is None:
        return
    try:
        audio = await app.speech.synthesize(text)
    except ServiceError as exc:
        await target.answer(f"⚠️ {exc}")
        return
    await target.answer_voice(BufferedInputFile(audio, "answer.ogg"))


def _stored_user_text(turn: Turn, text: str) -> str:
    return f"[{turn.author}]: {text}" if turn.is_group else text


async def respond(
    target: Message,
    app: App,
    turn: Turn,
    user_text: str,
    *,
    model: str | None = None,
    images: list[str] | None = None,
    store_text: str | None = None,
    extract_memory: bool = True,
    allow_tools: bool = True,
    voice: bool = False,
    keyboard: bool = True,
) -> str | None:
    """Главный цикл: контекст -> очередь GPU -> модель (с инструментами) -> стрим в Telegram
    -> история. Возвращает текст ответа или None, если ответа не было."""
    lock = app.chat_locks[turn.chat_id]
    if lock.locked():
        await target.answer("⏳ Ещё отвечаю на предыдущее сообщение, подожди.")
        return None
    allowed, _ = await app.check_limit(turn.user_id)
    if not allowed:
        await target.answer(
            f"🚫 Дневной лимит ({app.settings.daily_limit} запросов) исчерпан. Приходи завтра!"
        )
        return None

    assistant = app.assistant
    async with lock:
        mode = assistant.mode_for(turn)
        model = model or await assistant.resolve_model(turn, user_text, mode)
        prompt_text = _stored_user_text(turn, user_text)
        messages = await assistant.build_messages(
            turn, mode, prompt_text, images=images, with_tools_hint=allow_tools
        )

        waiting = app.queue.waiting + (1 if app.queue.busy else 0)
        placeholder = await target.answer(
            f"⏳ В очереди, передо мной {waiting}…" if app.queue.busy else "🦊 думаю…"
        )
        answer, status = "", ""
        try:
            async with app.queue.slot():
                if waiting:
                    await safe_edit(placeholder, "🦊 думаю…")
                last_edit = time.monotonic()
                async for event in assistant.run(
                    model, messages, turn=turn, allow_tools=allow_tools,
                    options=assistant.options_for(turn),
                ):
                    if event.kind == "status":
                        status = event.value
                        await safe_edit(placeholder, preview(strip_think(answer), status))
                        last_edit = time.monotonic()
                        continue
                    answer += event.value
                    if time.monotonic() - last_edit >= EDIT_INTERVAL:
                        await safe_edit(placeholder, preview(strip_think(answer), status))
                        last_edit = time.monotonic()
        except LLMError as exc:
            log.warning("LLM error for user %s, model %s: %s", turn.user_id, model, exc)
            await safe_edit(placeholder, f"⚠️ Ошибка модели {model}: {exc}")
            return None

        answer = strip_think(answer).strip() or "(модель вернула пустой ответ)"
        await app.db.add_message(
            turn.chat_id, "user", store_text or prompt_text, user_id=turn.user_id
        )
        answer_id = await app.db.add_message(turn.chat_id, "assistant", answer, model=model)
        markup = answer_keyboard(app, regenerate=not images) if keyboard else None
        last = await send_long(placeholder, answer, reply_markup=markup)
        await app.db.set_tg_msg_id(answer_id, last.message_id)
        await app.count_usage(turn.user_id)

    for png in assistant.images.pop(turn.chat_id, []):
        await target.answer_photo(BufferedInputFile(png, "plot.png"))
    if voice:
        await send_voice(app, target, answer)
    app.spawn(assistant.after_reply(turn, user_text, extract_memory=extract_memory))
    return answer


def with_user(turn: Turn, **changes: Any) -> Turn:
    """Копия Turn с изменёнными настройками пользователя (после изменения в БД)."""
    if turn.user is None:
        return turn
    return dataclasses.replace(turn, user=dataclasses.replace(turn.user, **changes))

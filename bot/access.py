"""Доступ к боту.

Личные чаты: админы из .env и пользователи, добавленные через /adduser.
Группы: бот реагирует только на обращения к нему (команда, @упоминание, ответ на его
сообщение). Отвечает зарегистрированным пользователям, а в группах, разрешённых через
/allowchat, — всем участникам (как гостям: без личной памяти и настроек).
"""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ChatType, ParseMode
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.types import User as TgUser

from .app import App
from .assistant import Turn


def is_group(message: Message) -> bool:
    return message.chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)


def addressed_to_bot(message: Message, username: str) -> bool:
    """Обращено ли сообщение в группе к боту."""
    text = message.text or message.caption or ""
    uname = username.lower()
    if text.startswith("/"):
        command = text.split(maxsplit=1)[0]
        return "@" not in command or command.lower().endswith("@" + uname)
    if uname and f"@{uname}" in text.lower():
        return True
    reply = message.reply_to_message
    return bool(reply and reply.from_user and reply.from_user.is_bot
                and (reply.from_user.username or "").lower() == uname)


def strip_mention(text: str, username: str) -> str:
    if not username:
        return text
    import re

    return re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE).strip()


class AccessMiddleware(BaseMiddleware):
    def __init__(self, app: App) -> None:
        self._app = app

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        tg_user: TgUser | None = data.get("event_from_user")
        if tg_user is None or tg_user.is_bot:
            return None
        app, db = self._app, self._app.db

        message = event if isinstance(event, Message) else None
        if isinstance(event, CallbackQuery) and isinstance(event.message, Message):
            chat = event.message.chat
        else:
            chat = message.chat if message else None
        group = chat is not None and chat.type in (ChatType.GROUP, ChatType.SUPERGROUP)

        if message is not None and group and not addressed_to_bot(message, app.bot_username):
            return None  # в группах молчим, пока к боту не обратились

        is_admin = tg_user.id in app.settings.admins
        if is_admin:
            # Админ должен быть в таблице users, чтобы хранить его настройки и память.
            await db.add_user(tg_user.id, tg_user.full_name)
        user = await db.get_user(tg_user.id)

        if user is None and not (group and await db.is_chat_allowed(chat.id)):
            if message is not None:
                await message.answer(
                    "🔒 Доступ закрыт.\n"
                    f"Твой ID: <code>{tg_user.id}</code> — передай его админу бота.",
                    parse_mode=ParseMode.HTML,
                )
            elif isinstance(event, CallbackQuery):
                await event.answer("Нет доступа", show_alert=True)
            return None

        data["is_admin"] = is_admin
        data["turn"] = Turn(
            user_id=tg_user.id,
            chat_id=chat.id if chat else tg_user.id,
            user=user,
            is_group=group,
            author=tg_user.first_name or tg_user.full_name,
        )
        return await handler(event, data)


async def admin_only(_: TelegramObject, is_admin: bool = False) -> bool:
    """Фильтр для админских команд (is_admin кладёт AccessMiddleware)."""
    return is_admin

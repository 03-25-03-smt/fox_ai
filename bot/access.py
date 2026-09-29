"""Доступ к боту: только админы из .env и пользователи, добавленные через /adduser."""

from collections.abc import Awaitable, Callable
from typing import Any

from aiogram import BaseMiddleware
from aiogram.enums import ParseMode
from aiogram.types import CallbackQuery, Message, TelegramObject, User

from .db import Database


class AccessMiddleware(BaseMiddleware):
    def __init__(self, db: Database, admins: frozenset[int]) -> None:
        self._db = db
        self._admins = admins

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is None or user.is_bot:
            return None

        if user.id in self._admins:
            # Админ должен быть в таблице users, чтобы хранить его модель и историю.
            await self._db.add_user(user.id, user.full_name)
            data["is_admin"] = True
            return await handler(event, data)

        if await self._db.is_user(user.id):
            data["is_admin"] = False
            return await handler(event, data)

        if isinstance(event, Message):
            await event.answer(
                "🔒 Доступ закрыт.\n"
                f"Твой ID: <code>{user.id}</code> — передай его админу бота.",
                parse_mode=ParseMode.HTML,
            )
        elif isinstance(event, CallbackQuery):
            await event.answer("Нет доступа", show_alert=True)
        return None


async def admin_only(_: TelegramObject, is_admin: bool = False) -> bool:
    """Фильтр для админских команд (is_admin кладёт AccessMiddleware)."""
    return is_admin

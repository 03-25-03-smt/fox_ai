"""Сборка роутеров Telegram."""

from aiogram import F, Router
from aiogram.types import BotCommand, Message

from ..access import AccessMiddleware
from ..app import App
from . import admin, aquarium, chat, code42, files, lang, settings, tools

BOT_COMMANDS = [
    BotCommand(command="mode", description="Режим: общение / 42 / защита"),
    BotCommand(command="model", description="Выбрать модель"),
    BotCommand(command="settings", description="Температура, длина, голос"),
    BotCommand(command="persona", description="Роль бота"),
    BotCommand(command="run", description="Запустить последний код"),
    BotCommand(command="valgrind", description="Запустить под valgrind"),
    BotCommand(command="tests", description="Сгенерировать и прогнать тесты"),
    BotCommand(command="project", description="Проверить проект (git-ссылка)"),
    BotCommand(command="defense", description="Тренировка защиты"),
    BotCommand(command="norm", description="Проверить код norminette"),
    BotCommand(command="42", description="Мой профиль в интре"),
    BotCommand(command="w", description="Слово: перевод и в словарь (de/cs)"),
    BotCommand(command="quiz", description="Повторить слова из словаря"),
    BotCommand(command="lang", description="Учитель языков: уроки, тесты, словарь"),
    BotCommand(command="aq", description="Аквариум: задачи, статистика, советы"),
    BotCommand(command="search", description="Найти в интернете"),
    BotCommand(command="draw", description="Нарисовать картинку"),
    BotCommand(command="py", description="Посчитать / построить график на Python"),
    BotCommand(command="remind", description="Напоминание"),
    BotCommand(command="reminders", description="Мои напоминания"),
    BotCommand(command="memories", description="Что бот обо мне помнит"),
    BotCommand(command="docs", description="Мои документы"),
    BotCommand(command="reset", description="Очистить текущий диалог"),
    BotCommand(command="whoami", description="Мои настройки"),
    BotCommand(command="help", description="Помощь"),
]


async def on_unknown_command(message: Message) -> None:
    await message.answer("Не знаю такую команду. /help")


async def on_unsupported(message: Message) -> None:
    await message.answer("Понимаю текст, голосовые, фото и файлы (.c/.h/.zip/.pdf/.txt/.md) 🙂")


def build_router(app: App) -> Router:
    root = Router(name="fox_ai")
    access = AccessMiddleware(app)
    root.message.outer_middleware(access)
    root.callback_query.outer_middleware(access)

    # aquarium и lang — раньше files и chat: они перехватывают текст, голосовые и файлы,
    # когда ждут ответа (причина «не могу», ответ на вопрос, предложение со словом)
    for module in (settings, admin, tools, code42, aquarium, lang, files, chat):
        root.include_router(module.router.build())
    fallback = Router(name="fallback")
    fallback.message.register(chat.on_text, F.text & ~F.text.startswith("/"))
    fallback.message.register(on_unknown_command, F.text.startswith("/"))
    fallback.message.register(on_unsupported, ~F.new_chat_members & ~F.left_chat_member)
    root.include_router(fallback)
    return root

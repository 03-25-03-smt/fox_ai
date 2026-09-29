"""Точка входа: python -m bot"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .assistant import Assistant
from .config import Settings
from .db import Database
from .handlers import BOT_COMMANDS, build_router
from .knowledge import KnowledgeBase
from .llm import OllamaClient
from .memory import MemoryStore
from .web import WebTools

log = logging.getLogger("fox_ai")


async def _initial_index(knowledge: KnowledgeBase) -> None:
    try:
        stats = await knowledge.reindex()
        log.info("knowledge: %s", stats)
    except Exception:
        log.exception("knowledge: initial indexing failed")


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    settings = Settings()
    if not settings.admins:
        log.warning("ADMIN_IDS пуст — ботом никто не сможет пользоваться")

    db = Database(settings.db_path)
    await db.connect()
    llm = OllamaClient(settings.ollama_url, settings.request_timeout, settings.num_ctx)
    web = WebTools(settings.searxng_url) if settings.web_enabled else None
    memory = MemoryStore(db, llm, settings.embed_model)
    knowledge = KnowledgeBase(db, llm, settings.embed_model, settings.knowledge_dir)
    assistant = Assistant(settings, llm, db, memory, knowledge, web)

    bot = Bot(settings.bot_token.get_secret_value())
    # Эти объекты автоматически передаются в хендлеры по имени аргумента
    dp = Dispatcher(db=db, assistant=assistant, settings=settings)
    dp.include_router(build_router(db, settings.admins))

    index_task = asyncio.create_task(_initial_index(knowledge))
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        log.info(
            "Fox AI запущен. Ollama: %s, модель: %s, эмбеддинги: %s, интернет: %s",
            settings.ollama_url, settings.default_model, settings.embed_model,
            "вкл" if web else "выкл",
        )
        await dp.start_polling(bot)
    finally:
        index_task.cancel()
        if web:
            await web.close()
        await llm.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

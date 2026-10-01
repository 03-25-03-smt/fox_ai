"""Точка входа: python -m bot"""

import asyncio
import logging

from aiogram import Bot, Dispatcher

from .app import App, GpuQueue
from .assistant import Assistant
from .config import Settings
from .db import Database
from .docs import PersonalDocs
from .handlers import BOT_COMMANDS, build_router
from .host import HostAgent
from .intra import IntraClient
from .knowledge import KnowledgeBase
from .llm import OllamaClient
from .memory import MemoryStore
from .metrics import start as start_metrics
from .services import ImageClient, SandboxClient, SpeechClient
from .summarize import YouTube
from .tasks import start_background
from .web import WebTools

log = logging.getLogger("fox_ai")


def build_app(settings: Settings, db: Database) -> App:
    llm = OllamaClient(settings.ollama_url, settings.request_timeout, settings.num_ctx)
    web = WebTools(settings.searxng_url) if settings.web_enabled else None
    assistant = Assistant(
        settings, llm, db,
        MemoryStore(db, llm, settings.embed_model),
        KnowledgeBase(db, llm, settings.embed_model, settings.knowledge_dir),
        web,
        PersonalDocs(db, llm, settings.embed_model),
    )
    intra = None
    if settings.intra_enabled:
        intra = IntraClient(settings.intra_client_id, settings.intra_client_secret.get_secret_value())
    sandbox = SandboxClient(settings.sandbox_url) if settings.sandbox_url else None
    assistant.sandbox = sandbox
    speech = SpeechClient(settings.speech_url) if settings.speech_url else None
    return App(
        settings=settings,
        db=db,
        assistant=assistant,
        sandbox=sandbox,
        speech=speech,
        youtube=YouTube(speech, settings.youtube_cookies) if settings.youtube_enabled else None,
        imagegen=ImageClient(settings.imagegen_url) if settings.imagegen_url else None,
        intra=intra,
        queue=GpuQueue(settings.max_concurrent),
        host=HostAgent(settings.host_dir) if settings.host_dir else None,
    )


async def _initial_index(app: App) -> None:
    try:
        stats = await app.assistant.knowledge.reindex()
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
    app = build_app(settings, db)

    bot = Bot(settings.bot_token.get_secret_value())
    me = await bot.me()
    app.bot_username = me.username or ""
    # app автоматически передаётся в хендлеры по имени аргумента
    dp = Dispatcher(app=app)
    dp.include_router(build_router(app))

    if settings.metrics_port:
        start_metrics(app, settings.metrics_port)
    app.spawn(_initial_index(app))
    start_background(bot, app)
    try:
        await bot.set_my_commands(BOT_COMMANDS)
        enabled = [name for name, on in (
            ("интернет", app.assistant.web), ("песочница", app.sandbox), ("речь", app.speech),
            ("картинки", app.imagegen), ("интра", app.intra), ("бэкапы", settings.backup_dir),
        ) if on]
        log.info("Fox AI (@%s) запущен. Ollama: %s, модель: %s. Включено: %s",
                 app.bot_username, settings.ollama_url, settings.default_model, ", ".join(enabled) or "—")
        await dp.start_polling(bot)
    finally:
        await app.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())

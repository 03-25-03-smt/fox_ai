"""Присланные файлы: код -> norminette, zip -> проверка проекта, документы -> личная
база (или общая с подписью /kb для админа), картинки -> vision."""

import re
from pathlib import Path

from aiogram import Bot, F
from aiogram.types import Message

from ..app import App
from ..assistant import Turn
from ..docs import SUPPORTED as DOC_SUFFIXES
from ..docs import DocError
from ..knowledge import SUPPORTED as KB_SUPPORTED
from ..llm import LLMError
from ..norminette import MAX_SOURCE_BYTES, safe_filename
from ..projects import MAX_ARCHIVE_BYTES, ProjectError, files_from_zip
from .admin import reindex
from .chat import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, answer_image
from .code42 import check_norm, check_project
from .registry import Routes

router = Routes("files")

MAX_DOWNLOAD = 20 * 1024 * 1024  # лимит Bot API на скачивание


@router.message(F.document)
async def on_document(message: Message, bot: Bot, app: App, turn: Turn, is_admin: bool) -> None:
    doc = message.document
    name = doc.file_name or ""
    suffix = Path(name).suffix.lower()
    size = doc.file_size or 0
    if size > MAX_DOWNLOAD:
        await message.answer("Файл больше 20 МБ — Telegram не даст его скачать боту.")
        return
    caption = (message.caption or "").strip()

    if suffix in (".c", ".h"):
        if size > MAX_SOURCE_BYTES:
            await message.answer("Файл слишком большой (максимум 256 КБ)")
            return
        source = (await bot.download(doc)).read().decode("utf-8", errors="replace")
        fname = safe_filename(name, source)
        await app.db.set_code(turn.chat_id, fname, {fname: source})
        await check_norm(message, app, turn, source, fname)
        return

    if suffix == ".zip":
        if size > MAX_ARCHIVE_BYTES:
            await message.answer("Архив больше 20 МБ")
            return
        try:
            files, skipped = files_from_zip((await bot.download(doc)).read())
        except ProjectError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        await check_project(message, app, turn, Path(name).stem or "project", files, skipped)
        return

    if suffix in IMAGE_SUFFIXES:
        if size > MAX_IMAGE_BYTES:
            await message.answer("Картинка слишком большая.")
            return
        await answer_image(message, app, turn, (await bot.download(doc)).read(), message.caption)
        return

    if caption.startswith("/kb") and suffix in KB_SUPPORTED:
        if not is_admin:
            await message.answer("Добавлять в общую базу знаний может только админ.")
            return
        target_dir = Path(app.settings.knowledge_dir) / "uploads"
        target_dir.mkdir(parents=True, exist_ok=True)
        safe = re.sub(r"[^\w.-]", "_", Path(name).name) or f"upload{suffix}"
        await bot.download(doc, destination=target_dir / safe)
        status = await message.answer(f"📚 Сохранил {safe}, индексирую…")
        await reindex(status, app)
        return

    if suffix in DOC_SUFFIXES:
        if not turn.registered:
            await message.answer("Личные документы доступны только пользователям с доступом к боту.")
            return
        status = await message.answer(f"📄 Читаю {name}…")
        try:
            info = await app.assistant.docs.add(turn.user_id, name, (await bot.download(doc)).read())
        except (DocError, LLMError) as exc:
            await status.edit_text(f"⚠️ {exc}")
            return
        await status.edit_text(
            f"📄 Добавил «{info.name}» в твои документы ({info.chunks} фрагм.). "
            "Теперь можешь задавать вопросы по нему. Список: /docs"
        )
        return

    await message.answer(
        "Понимаю файлы: .c/.h (norminette), .zip (проверка проекта), .pdf/.md/.txt "
        "(личные документы), картинки."
    )

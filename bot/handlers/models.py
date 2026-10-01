"""Управление моделями Ollama из Telegram (только админ): /pull, /rm, /bench."""

import re
import time

from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..access import admin_only
from ..app import App
from ..llm import LLMError
from .common import safe_edit
from .registry import Routes

router = Routes("models", message_filters=(admin_only,))

MODEL_RE = re.compile(r"^[a-zA-Z0-9][\w.\-/]*(:[\w.\-]+)?$")
PROGRESS_EVERY = 3.0  # секунд между обновлениями сообщения
BENCH_PROMPT = ("Объясни простыми словами, как работает хэш-таблица, и приведи короткий пример на C. "
                "Ответ — примерно 150 слов.")


def _gb(n: int) -> str:
    return f"{n / 1024**3:.1f} ГБ"


def progress_bar(done: int, total: int, width: int = 16) -> str:
    part = done / total if total else 0
    filled = round(part * width)
    return f"[{'█' * filled}{'░' * (width - filled)}] {part * 100:.0f}%"


@router.message(Command("pull"))
async def cmd_pull(message: Message, command: CommandObject, app: App) -> None:
    model = (command.args or "").strip()
    if not MODEL_RE.match(model):
        await message.answer("Использование: /pull qwen2.5:14b\nКаталог: https://ollama.com/library")
        return
    status = await message.answer(f"⬇️ {model}: начинаю…")
    last, started, line = 0.0, time.monotonic(), ""
    try:
        async for ev in app.llm.pull(model):
            total, done = ev.get("total") or 0, ev.get("completed") or 0
            line = ev.get("status", "")
            if total:
                speed = done / max(time.monotonic() - started, 1) / 1024**2
                line = f"{progress_bar(done, total)}\n{_gb(done)} из {_gb(total)}, {speed:.0f} МБ/с"
            if time.monotonic() - last >= PROGRESS_EVERY:
                await safe_edit(status, f"⬇️ <b>{model}</b>\n{line}", ParseMode.HTML)
                last = time.monotonic()
    except LLMError as exc:
        await safe_edit(status, f"⚠️ {model}: {exc}")
        return
    minutes = (time.monotonic() - started) / 60
    await safe_edit(status, f"✅ <b>{model}</b> скачана за {minutes:.1f} мин. Выбрать: /model · сравнить: /bench",
                    ParseMode.HTML)


@router.message(Command("rm"))
async def cmd_rm(message: Message, command: CommandObject, app: App) -> None:
    model = (command.args or "").strip()
    if not model:
        await message.answer("Использование: /rm <модель> (список — /model)")
        return
    s = app.settings
    protected = {s.default_model, s.code_model, s.fast_model, s.vision_model, s.embed_model, s.tutor_model}
    if model in protected or f"{model}:latest" in protected:
        await message.answer("⛔ Эта модель указана в .env (DEFAULT/CODE/FAST/VISION/EMBED/LANG_MODEL) — "
                             "сначала поменяй настройку.")
        return
    try:
        await app.llm.delete(model)
    except LLMError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    await message.answer(f"🗑 {model} удалена с диска.")


@router.message(Command("bench"))
async def cmd_bench(message: Message, command: CommandObject, app: App) -> None:
    """/bench [модели через пробел] — по умолчанию все скачанные чат-модели."""
    try:
        available = await app.llm.list_models()
    except LLMError as exc:
        await message.answer(f"⚠️ {exc}")
        return
    embed = app.settings.embed_model.split(":")[0]
    models = (command.args or "").split() or [m for m in available if m.split(":")[0] != embed]
    missing = [m for m in models if m not in available and f"{m}:latest" not in available]
    if missing:
        await message.answer(f"Нет таких моделей: {', '.join(missing)}. Скачать: /pull")
        return
    status = await message.answer(f"🏁 Замер {len(models)} моделей, это займёт пару минут…")
    rows = []
    for i, model in enumerate(models, 1):
        await safe_edit(status, f"🏁 {i}/{len(models)}: {model}…")
        try:
            async with app.queue.slot():
                r = await app.llm.bench(model, BENCH_PROMPT)
        except LLMError as exc:
            rows.append((model, None, str(exc)[:80]))
            continue
        rows.append((model, r, ""))
    rows.sort(key=lambda row: -(row[1]["gen_tps"] if row[1] else -1))
    lines = ["🏁 <b>Скорость моделей</b> (генерация · чтение промпта · загрузка)", ""]
    for model, r, err in rows:
        if r is None:
            lines.append(f"❌ <code>{model}</code>: {err}")
        else:
            lines.append(f"<code>{model}</code>: <b>{r['gen_tps']:.0f}</b> ток/с · {r['prompt_tps']:.0f} ток/с · "
                         f"{r['load_s']:.1f} с")
    lines.append("\nЗагрузка ≈ 0 с — модель уже была в видеопамяти.")
    await safe_edit(status, "\n".join(lines), ParseMode.HTML)

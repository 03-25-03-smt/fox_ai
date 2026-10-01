"""Админские команды: пользователи, группы, база знаний, /status, бэкапы."""

import html
import shutil
import time
from pathlib import Path

from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..access import admin_only, is_group
from ..app import App
from ..backup import backup_database, list_backups
from ..gpu import query_gpus
from ..llm import LLMError
from ..modes import get_mode
from .common import safe_edit
from .registry import Routes

router = Routes("admin", message_filters=(admin_only,))


def _parse_user_id(command: CommandObject) -> tuple[int, str] | None:
    if not command.args:
        return None
    parts = command.args.split(maxsplit=1)
    try:
        return int(parts[0]), (parts[1] if len(parts) > 1 else "")
    except ValueError:
        return None


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject, app: App) -> None:
    parsed = _parse_user_id(command)
    if parsed is None:
        await message.answer("Использование: /adduser <id> [имя]")
        return
    uid, name = parsed
    added = await app.db.add_user(uid, name, added_by=message.from_user.id)
    await message.answer("✅ Добавлен" if added else "Уже есть доступ")


@router.message(Command("deluser"))
async def cmd_deluser(message: Message, command: CommandObject, app: App) -> None:
    parsed = _parse_user_id(command)
    if parsed is None:
        await message.answer("Использование: /deluser <id>")
        return
    uid = parsed[0]
    if uid in app.settings.admins:
        await message.answer("Админа удалить нельзя — убери его из ADMIN_IDS в .env")
        return
    removed = await app.db.remove_user(uid)
    await message.answer("🗑 Удалён вместе с историей и памятью" if removed else "Такого пользователя нет")


@router.message(Command("users"))
async def cmd_users(message: Message, app: App) -> None:
    s = app.settings
    usage = await app.db.usage_for_day(app.today())
    lines = [
        f"{'👑' if u.id in s.admins else '👤'} <code>{u.id}</code> {html.escape(u.name)} — "
        f"{get_mode(u.mode, s.default_mode).title}, {html.escape(u.model or 'авто')}, "
        f"сегодня: {usage.get(u.id, 0)}"
        for u in await app.db.list_users()
    ]
    await message.answer("\n".join(lines) or "Пусто", parse_mode=ParseMode.HTML)


@router.message(Command("allowchat"))
async def cmd_allowchat(message: Message, app: App) -> None:
    if not is_group(message):
        await message.answer("Эту команду нужно отправить в группе.")
        return
    await app.db.allow_chat(message.chat.id, message.chat.title or "", message.from_user.id)
    await message.answer(
        "✅ Теперь все участники группы могут обращаться ко мне (@упоминание, ответ, /ask).\n"
        "Совет: в @BotFather → /setprivacy → Disable, чтобы я видел @упоминания."
    )


@router.message(Command("denychat"))
async def cmd_denychat(message: Message, app: App) -> None:
    removed = await app.db.disallow_chat(message.chat.id)
    await message.answer("🚫 Группа больше не разрешена" if removed else "Группа и так не была разрешена")


async def reindex(status: Message, app: App) -> None:
    kb = app.assistant.knowledge
    stats = await kb.reindex()
    files, chunks = await kb.stats()
    await safe_edit(
        status,
        f"📚 База знаний: {files} файлов, {chunks} фрагментов.\n"
        f"Проиндексировано: {stats.indexed}, без изменений: {stats.skipped}, "
        f"удалено: {stats.removed}, ошибок: {stats.failed}",
    )


@router.message(Command("reindex"))
async def cmd_reindex(message: Message, app: App) -> None:
    status = await message.answer("📚 Индексирую базу знаний…")
    await reindex(status, app)


def _uptime(seconds: float) -> str:
    days, rest = divmod(int(seconds), 86400)
    hours, rest = divmod(rest, 3600)
    return f"{days}д {hours}ч {rest // 60}м"


def _gb(num: int) -> str:
    return f"{num / 1024**3:.1f} ГБ"


async def build_status(app: App) -> str:
    s = app.settings
    lines = ["📊 <b>Fox AI — статус</b>", f"Аптайм: {_uptime(time.time() - app.started_at)}"]

    gpus = await query_gpus(s.gpu_stats_file)
    if gpus is None:
        lines.append("\n<b>GPU:</b> нет данных (nvidia-smi недоступен или не запущен windows/gpu-stats.ps1)")
    else:
        lines.append("\n<b>GPU:</b>")
        for g in gpus:
            hot = g.temperature is not None and g.temperature >= s.gpu_temp_alert
            temp = f"{g.temperature}°C" if g.temperature is not None else "?"
            mem = f"{g.memory_used}/{g.memory_total} МиБ" if g.memory_total else "?"
            power = f", {g.power:.0f} Вт" if g.power is not None else ""
            fan = f", вент. {g.fan}%" if g.fan is not None else ""
            lines.append(f"{'🔥' if hot else '•'} [{g.index}] {html.escape(g.name)}: {temp}, "
                         f"загрузка {g.utilization}%, {mem}{power}{fan}")

    try:
        loaded = await app.llm.loaded_models()
        lines.append("\n<b>Загруженные модели:</b>")
        lines += [f"• {html.escape(m.name)} — VRAM {_gb(m.size_vram)} из {_gb(m.size)}" for m in loaded] or ["—"]
    except LLMError as exc:
        lines.append(f"\n⚠️ Ollama: {html.escape(str(exc))}")

    speeds: dict[str, list[float]] = {}
    for sample in list(app.llm.speeds)[-30:]:
        speeds.setdefault(sample.model, []).append(sample.tokens_per_sec)
    if speeds:
        lines.append("\n<b>Скорость (последние ответы):</b>")
        lines += [f"• {html.escape(m)}: {sum(v) / len(v):.1f} ток/с ({len(v)})" for m, v in speeds.items()]

    q = app.queue
    lines.append(f"\n<b>Очередь:</b> генерируется {q.active}/{q.slots}, ждут {q.waiting}")

    services = [("песочница", app.sandbox), ("речь", app.speech), ("картинки", app.imagegen)]
    web = app.assistant.web
    health = []
    for name, client in services:
        if client is None:
            health.append(f"{name}: выкл")
        else:
            health.append(f"{name}: {'✅' if await client.health() else '❌'}")
    health.append(f"интернет: {'вкл' if web else 'выкл'}")
    health.append(f"интра: {'вкл' if app.intra else 'выкл'}")
    lines.append("<b>Сервисы:</b> " + " · ".join(health))

    stats = await app.db.stats()
    usage = await app.db.usage_for_day(app.today())
    lines.append(
        f"\n<b>Данные:</b> пользователей {stats['users']}, сообщений {stats['chat_messages']}, "
        f"фактов {stats['memories']}, документов {stats['docs']}, "
        f"напоминаний {stats['reminders']}, групп {stats['allowed_chats']}"
    )
    lines.append(f"Запросов сегодня: {sum(usage.values())}"
                 + (f" (лимит {s.daily_limit}/чел.)" if s.daily_limit else ""))

    data_dir = Path(app.db.path).parent
    if data_dir.exists():
        disk = shutil.disk_usage(data_dir)
        lines.append(f"Диск (данные): свободно {_gb(disk.free)} из {_gb(disk.total)}")
    if s.backup_dir:
        backups = list_backups(s.backup_dir)
        lines.append(f"Бэкапы: {len(backups)}, последний: {backups[-1].name if backups else '—'}")
    return "\n".join(lines)


@router.message(Command("status"))
async def cmd_status(message: Message, app: App) -> None:
    await message.answer(await build_status(app), parse_mode=ParseMode.HTML)


@router.message(Command("backup"))
async def cmd_backup(message: Message, app: App) -> None:
    s = app.settings
    if not s.backup_dir:
        await message.answer("💾 Бэкапы выключены (не задан BACKUP_DIR).")
        return
    path = await backup_database(app.db.path, s.backup_dir, s.backup_keep)
    await message.answer(f"💾 Бэкап готов: {path.name} ({path.stat().st_size // 1024} КБ)")

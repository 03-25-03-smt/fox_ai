"""Команды для хоста Windows через агента: /logs, /restart, /ps, /power (только админ)."""

import html

from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ..access import admin_only
from ..app import App
from ..host import SERVICE_RE, HostError
from .registry import Routes

router = Routes("host", message_filters=(admin_only,))

MAX_INLINE = 3500


async def _run(message: Message, app: App, cmd: str, *args: str, timeout: float = 60.0):
    if app.host is None:
        await message.answer("🖥 Агент Windows не настроен (HOST_DIR, windows\\fox-agent.ps1 — см. README).")
        return None
    try:
        result = await app.host.run(cmd, *args, timeout=timeout)
    except HostError as exc:
        await message.answer(f"⚠️ {exc}")
        return None
    return result


async def _send_output(message: Message, title: str, result, name: str) -> None:
    out = result.output.strip() or "(пусто)"
    icon = "✅" if result.ok else "⚠️"
    if len(out) <= MAX_INLINE:
        await message.answer(f"{icon} {html.escape(title)}\n<pre>{html.escape(out)}</pre>", parse_mode=ParseMode.HTML)
    else:
        await message.answer_document(BufferedInputFile(out.encode(), name), caption=f"{icon} {title}")


@router.message(Command("logs"))
async def cmd_logs(message: Message, command: CommandObject, app: App) -> None:
    args = (command.args or "").split()
    service = args[0] if args else "bot"
    lines = args[1] if len(args) > 1 and args[1].isdigit() else "80"
    if not SERVICE_RE.match(service):
        await message.answer("Использование: /logs <сервис> [строк], например /logs speech 200")
        return
    if (result := await _run(message, app, "logs", service, lines)) is not None:
        await _send_output(message, f"Логи {service} (последние {lines})", result, f"{service}.log")


@router.message(Command("restart"))
async def cmd_restart(message: Message, command: CommandObject, app: App) -> None:
    service = (command.args or "").strip()
    if not SERVICE_RE.match(service):
        await message.answer("Использование: /restart <сервис> (bot, speech, imagegen, sandbox, searxng, ollama…)")
        return
    if service == "bot":
        await message.answer("🔄 Перезапускаю себя — вернусь через ~20 секунд.")
    status = await message.answer(f"🔄 Перезапускаю {service}…")
    cmd, args = ("ollama-restart", ()) if service == "ollama" else ("restart", (service,))
    result = await _run(message, app, cmd, *args, timeout=180)
    if result is not None:
        await status.edit_text(f"{'✅' if result.ok else '⚠️'} {service}: {result.output.strip()[:500] or 'готово'}")


@router.message(Command("ps"))
async def cmd_ps(message: Message, app: App) -> None:
    if (result := await _run(message, app, "ps")) is not None:
        await _send_output(message, "Контейнеры", result, "ps.txt")


@router.message(Command("power"))
async def cmd_power(message: Message, command: CommandObject, app: App) -> None:
    arg = (command.args or "").strip().lower()
    if arg and not (arg == "default" or (arg.isdigit() and 100 <= int(arg) <= 300)):
        await message.answer("Использование: /power — текущий лимит · /power 180 — лимит P100 в ваттах (100–300) "
                             "· /power default")
        return
    if not arg:
        status = app.host.status() if app.host else None
        if status is None:
            await message.answer("🖥 Агент Windows не запущен.")
            return
        mode = "🌡 бережный режим (перегрев)" if app.throttled else "обычный режим"
        await message.answer(f"⚡ {status.get('power', '?')} · {mode}. Слотов генерации: {app.queue.limit}")
        return
    if (result := await _run(message, app, "power", arg)) is not None:
        await message.answer(("✅ " if result.ok else "⚠️ ") + result.output.strip())

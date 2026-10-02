"""/export — диалог в Markdown/PDF, /ics — напоминания и график аквариумов в календарь."""

import asyncio
import datetime

from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, Message

from ..app import App
from ..assistant import Turn
from ..export import CalendarEvent, ExportError, ExportMessage, to_ics, to_markdown, to_pdf, weekly_rule
from .common import need_registered
from .registry import Routes

router = Routes("export")


@router.message(Command("export"))
async def cmd_export(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    fmt = (command.args or "md").strip().lower()
    if fmt not in ("md", "markdown", "pdf"):
        await message.answer("Использование: /export (Markdown) или /export pdf")
        return
    rows = await app.db.all_messages(turn.chat_id)
    if not rows:
        await message.answer("📤 Экспортировать нечего — диалог пуст.")
        return
    tz = app.assistant.tz(turn.user)
    messages = [ExportMessage(*r) for r in rows]
    now = datetime.datetime.now(tz)
    title = f"Fox AI — диалог от {now:%d.%m.%Y}"
    stamp = f"{now:%Y-%m-%d}"
    if fmt == "pdf":
        try:
            data = await asyncio.to_thread(to_pdf, messages, title, tz)
        except ExportError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        name = f"fox_ai_{stamp}.pdf"
    else:
        data, name = to_markdown(messages, title, tz).encode(), f"fox_ai_{stamp}.md"
    await message.answer_document(BufferedInputFile(data, name),
                                  caption=f"📤 {len(messages)} сообщений. Очистить диалог: /reset")


@router.message(Command("ics"))
async def cmd_ics(message: Message, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    events = [CalendarEvent(f"reminder-{r.id}", f"⏰ {r.text}", r.due_at)
              for r in await app.db.list_reminders(turn.user_id)]
    homes = app.assistant.aquariums
    aq = await homes.for_user(turn.user_id) if homes is not None else None
    if aq is not None:
        today = aq.now().date()
        # Свои пункты графика и общие; поручённые другим участникам — не в его календарь
        for item in [i for i in await aq.schedule() if i.assignee_id in (None, turn.user_id)]:
            # Первое событие — ближайший подходящий день, дальше повторяет RRULE
            first = next(today + datetime.timedelta(days=i) for i in range(7)
                         if (today + datetime.timedelta(days=i)).weekday() in item.days)
            events.append(CalendarEvent(f"aquarium-{item.id}", await aq.task_name(item), aq.at(first, item.at),
                                        rrule=weekly_rule(item.days), alarm=False))
    if not events:
        await message.answer("📅 Нет напоминаний и графиков для календаря.")
        return
    await message.answer_document(
        BufferedInputFile(to_ics(events, "Fox AI"), "fox_ai.ics"),
        caption=f"📅 Событий: {len(events)}. Открой файл на телефоне или импортируй в Google Calendar "
                "(Настройки → Импорт). При повторном импорте события обновятся, а не задвоятся.",
    )

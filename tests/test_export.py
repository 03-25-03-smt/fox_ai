"""Экспорт диалога и календаря."""

import datetime
import zoneinfo

import pytest
from aiogram.methods import SendDocument

from bot.export import FONT_CANDIDATES, CalendarEvent, ExportMessage, _font, to_ics, to_markdown, to_pdf, weekly_rule

from .conftest import ADMIN

PARIS = zoneinfo.ZoneInfo("Europe/Paris")
MSGS = [
    ExportMessage("user", "Привет! Что такое указатель? 🐰", "2026-10-01 08:00:00"),
    ExportMessage("assistant", "Это адрес.\n```c\nint *p = &x;\n```\n**Важно**: не NULL.", "2026-10-01 08:00:05",
                  "qwen2.5:7b"),
    ExportMessage("user", "Спасибо", "2026-10-02 09:30:00"),
]


def test_markdown():
    md = to_markdown(MSGS, "Диалог", PARIS)
    assert md.startswith("# Диалог")
    assert "## 01.10.2026" in md and "## 02.10.2026" in md
    assert "**🐰 Fox AI (qwen2.5:7b)** · 10:00" in md  # UTC+2 летом
    assert "int *p = &x;" in md


@pytest.mark.skipif(_font(FONT_CANDIDATES) is None, reason="нет шрифта DejaVu")
def test_pdf_with_cyrillic_and_code():
    data = to_pdf(MSGS * 30, "Диалог с Fox AI", PARIS)
    assert data.startswith(b"%PDF") and len(data) > 5000


def test_ics_reminder_and_weekly():
    events = [
        CalendarEvent("reminder-1", "⏰ Защита, проект; libft", datetime.datetime(2026, 10, 3, 14, 0, tzinfo=PARIS)),
        CalendarEvent("aq-2", "💧 Подмена воды " + "очень длинное название " * 5,
                      datetime.datetime(2026, 10, 4, 12, 0, tzinfo=PARIS), rrule=weekly_rule((6,)), alarm=False),
    ]
    ics = to_ics(events, "Fox AI", datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)).decode()
    assert ics.startswith("BEGIN:VCALENDAR\r\n") and ics.endswith("END:VCALENDAR\r\n")
    assert "DTSTART:20261003T120000Z" in ics and "SUMMARY:⏰ Защита\\, проект\\; libft" in ics
    assert "DTSTART;TZID=Europe/Paris:20261004T120000" in ics and "RRULE:FREQ=WEEKLY;BYDAY=SU" in ics
    assert ics.count("BEGIN:VALARM") == 1
    assert all(len(line.encode()) <= 75 for line in ics.split("\r\n"))
    assert weekly_rule(tuple(range(7))) == "FREQ=DAILY"


async def test_export_commands(env):
    await env.send(ADMIN, "/export")
    assert "пуст" in env.last_text()
    await env.send(ADMIN, "привет")
    await env.send(ADMIN, "/export")
    doc = env.session.of_type(SendDocument)[-1]
    assert doc.document.filename.endswith(".md") and b"\xd0\xbf\xd1\x80\xd0\xb8\xd0\xb2\xd0\xb5\xd1\x82" in doc.document.data
    if _font(FONT_CANDIDATES):
        await env.send(ADMIN, "/export pdf")
        assert env.session.of_type(SendDocument)[-1].document.data.startswith(b"%PDF")


async def test_ics_command(env):
    await env.send(ADMIN, "/ics")
    assert "Нет напоминаний" in env.last_text()
    await env.send(ADMIN, "/remind завтра в 9 защита")
    aq = await env.assistant.aquariums.for_user(ADMIN)
    tank = (await aq.tanks())[0]
    await aq.add_item(tank.id, "Подмена 30%", datetime.time(12, 0), (6,), "water")
    await env.send(ADMIN, "/ics")
    data = env.session.of_type(SendDocument)[-1].document.data.decode()
    assert "SUMMARY:⏰ защита" in data and "Подмена 30%" in data and "BYDAY=SU" in data

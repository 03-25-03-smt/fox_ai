"""Экспорт: диалог в Markdown или PDF, напоминания и график ухода за аквариумами в календарь (.ics)."""

import datetime
import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

# Шрифт с кириллицей для PDF. В Docker-образе — пакет fonts-dejavu-core
FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
    "C:/Windows/Fonts/arial.ttf",
)
MONO_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu-sans-mono-fonts/DejaVuSansMono.ttf",
)


class ExportError(Exception):
    pass


@dataclass(frozen=True)
class ExportMessage:
    role: str
    content: str
    created_at: str  # UTC 'YYYY-MM-DD HH:MM:SS'
    model: str | None = None


def _local(ts: str, tz: datetime.tzinfo) -> datetime.datetime:
    return datetime.datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=datetime.UTC).astimezone(tz)


def to_markdown(messages: list[ExportMessage], title: str, tz: datetime.tzinfo) -> str:
    lines = [f"# {title}", ""]
    day = None
    for m in messages:
        when = _local(m.created_at, tz)
        if when.date() != day:
            day = when.date()
            lines += [f"## {day:%d.%m.%Y}", ""]
        who = "🧑 Я" if m.role == "user" else f"🦊 Fox AI{f' ({m.model})' if m.model else ''}"
        lines += [f"**{who}** · {when:%H:%M}", "", m.content.strip(), ""]
    return "\n".join(lines).rstrip() + "\n"


def _font(candidates: Iterable[str]) -> str | None:
    return next((p for p in candidates if Path(p).is_file()), None)


def _strip_md(text: str) -> str:
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)[*_](.+?)[*_](?!\w)", r"\1", text)
    text = re.sub(r"`([^`\n]+)`", r"\1", text)
    text = re.sub(r"^#{1,6}\s*", "", text, flags=re.MULTILINE)
    return text


def _pdf_safe(text: str) -> str:
    # В DejaVu нет цветных эмодзи — заменяем их, чтобы PDF не падал
    return "".join(c if ord(c) < 0x2600 or 0x2C00 <= ord(c) < 0xD800 else "·" for c in text)


def to_pdf(messages: list[ExportMessage], title: str, tz: datetime.tzinfo) -> bytes:
    from fpdf import FPDF  # тяжёлый импорт — только при экспорте

    font, mono = _font(FONT_CANDIDATES), _font(MONO_CANDIDATES) or _font(FONT_CANDIDATES)
    if font is None:
        raise ExportError("нет шрифта с кириллицей (в образе бота — пакет fonts-dejavu-core)")
    pdf = FPDF(format="A4")
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_font("body", fname=font)
    pdf.add_font("mono", fname=mono)
    pdf.set_title(title)
    pdf.add_page()
    pdf.set_font("body", size=16)
    pdf.multi_cell(0, 9, _pdf_safe(title), new_x="LMARGIN", new_y="NEXT")
    pdf.ln(3)
    width = pdf.w - pdf.l_margin - pdf.r_margin
    for m in messages:
        when = _local(m.created_at, tz)
        who = "Я" if m.role == "user" else "Fox AI"
        pdf.set_font("body", size=9)
        pdf.set_text_color(110, 110, 110)
        pdf.multi_cell(0, 5, f"{who} · {when:%d.%m.%Y %H:%M}", new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)
        for i, part in enumerate(re.split(r"```[\w+-]*\n?", m.content.strip())):
            if not part.strip():
                continue
            is_code = i % 2 == 1  # нечётные куски — внутри ```…```
            pdf.set_font("mono" if is_code else "body", size=8.5 if is_code else 10.5)
            if is_code:
                pdf.set_fill_color(242, 242, 242)
            pdf.multi_cell(width, 4.6 if is_code else 5.6, _pdf_safe(part.rstrip() if is_code else _strip_md(part)),
                           fill=is_code, new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
    return bytes(pdf.output())


# ---------------------------------------------------------------- календарь


def _ics_escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _fold(line: str) -> str:
    """RFC 5545: строки длиннее 75 байт переносятся с пробелом в начале."""
    raw = line.encode()
    if len(raw) <= 75:
        return line
    parts, current = [], b""
    for ch in line:
        b = ch.encode()
        if len(current) + len(b) > (75 if not parts else 74):
            parts.append(current.decode())
            current = b""
        current += b
    parts.append(current.decode())
    return "\r\n ".join(parts)


@dataclass(frozen=True)
class CalendarEvent:
    uid: str
    title: str
    start: datetime.datetime  # aware
    minutes: int = 15
    rrule: str | None = None  # «FREQ=WEEKLY;BYDAY=MO,WE»
    alarm: bool = True


ICS_DAYS = ("MO", "TU", "WE", "TH", "FR", "SA", "SU")


def weekly_rule(days: tuple[int, ...]) -> str:
    if len(days) == 7:
        return "FREQ=DAILY"
    return "FREQ=WEEKLY;BYDAY=" + ",".join(ICS_DAYS[d] for d in sorted(days))


def to_ics(events: list[CalendarEvent], name: str, now: datetime.datetime | None = None) -> bytes:
    stamp = (now or datetime.datetime.now(datetime.UTC)).astimezone(datetime.UTC).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Fox AI//RU", "CALSCALE:GREGORIAN",
             f"X-WR-CALNAME:{_ics_escape(name)}"]
    for ev in events:
        tzid = getattr(ev.start.tzinfo, "key", None)
        if ev.rrule and tzid:
            # Повторяющиеся события — в местном времени, иначе при переходе на летнее время съедут
            end = ev.start + datetime.timedelta(minutes=ev.minutes)
            when = [f"DTSTART;TZID={tzid}:{ev.start:%Y%m%dT%H%M%S}", f"DTEND;TZID={tzid}:{end:%Y%m%dT%H%M%S}"]
        else:
            start = ev.start.astimezone(datetime.UTC)
            end = start + datetime.timedelta(minutes=ev.minutes)
            when = [f"DTSTART:{start:%Y%m%dT%H%M%SZ}", f"DTEND:{end:%Y%m%dT%H%M%SZ}"]
        lines += ["BEGIN:VEVENT", f"UID:{ev.uid}@fox-ai", f"DTSTAMP:{stamp}", *when,
                  f"SUMMARY:{_ics_escape(ev.title)}"]
        if ev.rrule:
            lines.append(f"RRULE:{ev.rrule}")
        if ev.alarm:
            lines += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{_ics_escape(ev.title)}",
                      "TRIGGER:PT0M", "END:VALARM"]
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(_fold(line) for line in lines) + "\r\n").encode()

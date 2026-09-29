"""Разбор времени для напоминаний: «через 20 минут», «завтра в 9», «в пятницу в 18:30»,
«31.12 23:59», «in 2 hours». Время может стоять в начале или в конце фразы."""

import datetime
import re
from dataclasses import dataclass

UNITS = {
    "с": "seconds", "сек": "seconds", "секунд": "seconds", "секунду": "seconds", "секунды": "seconds",
    "s": "seconds", "sec": "seconds", "second": "seconds", "seconds": "seconds",
    "м": "minutes", "мин": "minutes", "минут": "minutes", "минуту": "minutes", "минуты": "minutes",
    "m": "minutes", "min": "minutes", "mins": "minutes", "minute": "minutes", "minutes": "minutes",
    "ч": "hours", "час": "hours", "часа": "hours", "часов": "hours",
    "h": "hours", "hour": "hours", "hours": "hours",
    "д": "days", "день": "days", "дня": "days", "дней": "days",
    "d": "days", "day": "days", "days": "days",
    "нед": "weeks", "неделю": "weeks", "недели": "weeks", "недель": "weeks",
    "week": "weeks", "weeks": "weeks",
}
WEEKDAYS = {
    "понедельник": 0, "пн": 0, "monday": 0, "mon": 0,
    "вторник": 1, "вт": 1, "tuesday": 1, "tue": 1,
    "среду": 2, "среда": 2, "ср": 2, "wednesday": 2, "wed": 2,
    "четверг": 3, "чт": 3, "thursday": 3, "thu": 3,
    "пятницу": 4, "пятница": 4, "пт": 4, "friday": 4, "fri": 4,
    "субботу": 5, "суббота": 5, "сб": 5, "saturday": 5, "sat": 5,
    "воскресенье": 6, "вс": 6, "sunday": 6, "sun": 6,
}
DEFAULT_HOUR = 10

_UNIT_RE = "|".join(sorted(map(re.escape, UNITS), key=len, reverse=True))
_REL_START = re.compile(r"^(через|in)\s+", re.IGNORECASE)
_REL_PART = re.compile(rf"^(\d+(?:[.,]\d+)?)?\s*({_UNIT_RE})\b\.?\s*(и\s+|and\s+)?", re.IGNORECASE)
_HALF_HOUR = re.compile(r"^(полчаса|half an hour)\b\s*", re.IGNORECASE)
_DAY_WORD = re.compile(r"^(сегодня|today|завтра|tomorrow|послезавтра)\b\s*", re.IGNORECASE)
_WEEKDAY = re.compile(
    r"^(?:(?:в|во|on)\s+)?(" + "|".join(sorted(WEEKDAYS, key=len, reverse=True)) + r")\b\.?\s*",
    re.IGNORECASE,
)
_DATE_DOT = re.compile(r"^(\d{1,2})\.(\d{1,2})(?:\.(\d{2}|\d{4}))?\b\s*")
_DATE_ISO = re.compile(r"^(\d{4})-(\d{2})-(\d{2})(?:[ T]|\b)\s*")
_TIME = re.compile(
    r"^(?:(?:в|во|at)\s+)?(\d{1,2})(?::(\d{2}))?"
    r"(?:\s*(утра|дня|вечера|ночи|am|pm|час(?:а|ов)?|ч)\b\.?)?\s*",
    re.IGNORECASE,
)
_PREFIX = re.compile(r"^(напомни(ть)?(\s+мне)?|remind(\s+me)?)\s+", re.IGNORECASE)
_FILLER = re.compile(r"^(что(бы)?|to)\s+|^[,:\-—–\s]+", re.IGNORECASE)
_ANCHOR = re.compile(
    r"(?<!\w)(через|in|сегодня|today|завтра|tomorrow|послезавтра|в|во|on|at|\d{1,2}\.\d{1,2}|\d{4}-\d{2}-\d{2})(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ParsedReminder:
    due: datetime.datetime
    text: str


def _parse_relative(s: str, now: datetime.datetime) -> tuple[datetime.datetime, str] | None:
    m = _REL_START.match(s)
    if not m:
        return None
    rest = s[m.end():]
    delta = datetime.timedelta()
    matched = False
    while True:
        if half := _HALF_HOUR.match(rest):
            delta += datetime.timedelta(minutes=30)
            rest, matched = rest[half.end():], True
            continue
        part = _REL_PART.match(rest)
        if not part:
            break
        amount = float((part.group(1) or "1").replace(",", "."))
        delta += datetime.timedelta(**{UNITS[part.group(2).lower()]: amount})
        rest, matched = rest[part.end():], True
    if not matched or delta.total_seconds() <= 0:
        return None
    return now + delta, rest


def _apply_time(m: re.Match[str], base: datetime.date, tz) -> datetime.datetime | None:
    hour, minute = int(m.group(1)), int(m.group(2) or 0)
    suffix = (m.group(3) or "").lower()
    if suffix in ("дня", "вечера", "pm") and hour < 12:
        hour += 12
    elif suffix in ("ночи", "am") and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return datetime.datetime.combine(base, datetime.time(hour, minute), tzinfo=tz)


def _parse_absolute(s: str, now: datetime.datetime) -> tuple[datetime.datetime, str] | None:
    tz = now.tzinfo
    today = now.date()
    date: datetime.date | None = None
    explicit_today = False
    rest = s

    if m := _DAY_WORD.match(rest):
        word = m.group(1).lower()
        offset = {"сегодня": 0, "today": 0, "завтра": 1, "tomorrow": 1, "послезавтра": 2}[word]
        date, explicit_today, rest = today + datetime.timedelta(days=offset), offset == 0, rest[m.end():]
    elif m := _WEEKDAY.match(rest):
        target = WEEKDAYS[m.group(1).lower()]
        days = (target - today.weekday()) % 7
        date, rest = today + datetime.timedelta(days=days), rest[m.end():]
    elif m := _DATE_ISO.match(rest):
        try:
            date = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            return None
        rest = rest[m.end():]
    elif m := _DATE_DOT.match(rest):
        year = int(m.group(3)) if m.group(3) else today.year
        year += 2000 if year < 100 else 0
        try:
            date = datetime.date(year, int(m.group(2)), int(m.group(1)))
        except ValueError:
            return None
        if not m.group(3) and date < today:
            date = date.replace(year=year + 1)
        rest = rest[m.end():]

    time_match = _TIME.match(rest)
    # Без даты время принимаем только в явном виде: «в 18», «at 9», «18:30»
    if time_match and date is None:
        has_prefix = rest[: time_match.start(1)].strip() != ""
        if not (has_prefix or time_match.group(2)):
            time_match = None

    if time_match:
        due = _apply_time(time_match, date or today, tz)
        if due is None:
            return None
        rest = rest[time_match.end():]
        if date is None and due <= now:
            due += datetime.timedelta(days=1)
        elif date is not None and not explicit_today and due <= now and _WEEKDAY.match(s):
            due += datetime.timedelta(days=7)  # «в пятницу в 9», а сейчас пятница 10:00
    elif date is not None:
        if explicit_today:
            return None  # «сегодня» без времени — непонятно когда
        due = datetime.datetime.combine(date, datetime.time(DEFAULT_HOUR), tzinfo=tz)
        if due <= now and _WEEKDAY.match(s):
            due += datetime.timedelta(days=7)
    else:
        return None
    return due, rest


def _parse_at_start(s: str, now: datetime.datetime) -> tuple[datetime.datetime, str] | None:
    return _parse_relative(s, now) or _parse_absolute(s, now)


def _clean(text: str) -> str:
    text = text.strip()
    while (m := _FILLER.match(text)) and m.end() > 0:
        text = text[m.end():]
    return text.strip(" .,!")


def parse_reminder(text: str, now: datetime.datetime) -> ParsedReminder | None:
    """now — текущее время в часовом поясе пользователя (aware)."""
    s = _PREFIX.sub("", text.strip())
    if parsed := _parse_at_start(s, now):
        due, rest = parsed
        if due > now and (body := _clean(rest)):
            return ParsedReminder(due, body)
    # Время в конце: «снять пасту через 10 минут»
    for anchor in _ANCHOR.finditer(s):
        if anchor.start() == 0:
            continue
        parsed = _parse_at_start(s[anchor.start():], now)
        if parsed and not parsed[1].strip(" .,!") and parsed[0] > now:
            if body := _clean(s[: anchor.start()]):
                return ParsedReminder(parsed[0], body)
    return None

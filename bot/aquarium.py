"""Аквариум (бывший fish_helper): расписание ухода, напоминания, просрочки, отчёты,
серии и достижения. Данные — в таблицах aq_* общей базы Fox AI.

Время задач хранится ISO-строкой с часовым поясом (как в старом боте), даты — по
часовому поясу аквариума (AQUARIUM_TIMEZONE), а не сервера.
"""

import datetime
import html
import sqlite3
from dataclasses import dataclass
from typing import Any

from .db import Database

# ---------------------------------------------------------------- задачи и расписание

TASKS = {
    "feed": "🐟 Покормить рыбок",
    "air_on": "💨 Включить воздух",
    "light_on": "💡 Включить свет",
    "light_off": "💡 Выключить свет",
    "air_off": "💨 Выключить воздух",
    "water": "💧 Подмена воды",
    "filter": "🧽 Почистить губку фильтра",
}
# Тестовые задачи не попадают в «сегодня», историю, отчёт и статистику
TEST_TASKS = {
    "test": "🧪 Тестовая задача",
    "overdue_test": "🧪 Тестовая просроченная задача",
}
ALL_TASKS = {**TASKS, **TEST_TASKS}

REMIND_AFTER = datetime.timedelta(minutes=30)  # повторное напоминание
OVERDUE_AFTER = datetime.timedelta(hours=1)  # предупреждение о просрочке всем
SNOOZE_FOR = datetime.timedelta(minutes=15)
MAX_SNOOZES = 3
MISSED_WINDOW = datetime.timedelta(hours=3)  # досылать задачи, пропущенные при перезапуске
LATE_NOTE_AFTER = datetime.timedelta(minutes=5)
REPORT_TIME = datetime.time(23, 15)
WEEKLY_TIME = datetime.time(23, 20)

# Дни недели как в Python: пн = 0 … вс = 6
MON, TUE, WED, THU, FRI, SAT, SUN = range(7)
ALL_DAYS = tuple(range(7))
FASTING_DAYS = (THU,)  # разгрузочный день: рыбок не кормим
FEED_DAYS = tuple(d for d in ALL_DAYS if d not in FASTING_DAYS)

DEFAULT_SCHEDULE: list[tuple[str, tuple[int, int], tuple[int, ...]]] = [
    ("feed", (7, 0), FEED_DAYS),
    ("air_on", (7, 0), ALL_DAYS),
    ("light_on", (14, 30), ALL_DAYS),
    ("light_off", (21, 0), ALL_DAYS),
    ("air_off", (22, 0), ALL_DAYS),
    ("water", (12, 0), (SUN,)),
    # Одна задача может стоять в разные дни в разное время
    ("filter", (19, 0), (WED,)),
    ("filter", (12, 0), (SUN,)),
]

WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]

CANT_REASONS = {
    "away": "🏠 Не дома",
    "nothing": "📦 Нечем (нет корма/средств)",
    "broken": "🛠 Что-то сломалось",
    "other": "✍️ Другая причина",
}

ACHIEVEMENTS = {
    3: "🥉 3 дня без пропусков",
    7: "🥈 Неделя без пропусков",
    14: "🥇 Две недели без пропусков",
    30: "🏆 Месяц без пропусков",
    100: "👑 100 дней без пропусков",
}

PAUSE_FOREVER = "forever"


def days_text(days: tuple[int, ...]) -> str:
    if tuple(days) == ALL_DAYS:
        return "каждый день"
    missing = [d for d in ALL_DAYS if d not in days]
    if len(missing) == 1:
        return f"каждый день, кроме {DAY_SHORT[missing[0]]}"
    return ", ".join(DAY_SHORT[d] for d in ALL_DAYS if d in days)


def parse_dt(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def hhmm(value: str | None) -> str:
    dt = parse_dt(value)
    return dt.strftime("%H:%M") if dt else "—"


def _e(text: Any) -> str:
    return html.escape(str(text))


@dataclass(frozen=True)
class ScheduleItem:
    task_id: str
    at: datetime.time
    days: tuple[int, ...]


Row = dict[str, Any]


def remind_at_of(row: Row) -> datetime.datetime:
    return parse_dt(row["remind_at"]) or parse_dt(row["sent_at"]) + REMIND_AFTER


def overdue_at_of(row: Row) -> datetime.datetime:
    return parse_dt(row["overdue_at"]) or parse_dt(row["sent_at"]) + OVERDUE_AFTER


def format_task_row(row: Row, indent: str = "", pending_icon: str = "⏳") -> str:
    """Одна задача для «сегодня», истории и отчёта (HTML)."""
    name = _e(row["task_name"])
    if row["completed_at"]:
        who = _e(row["completed_by_name"] or "Неизвестно")
        return f"{indent}✅ {name}\n{indent}   👤 {who} — 🕐 {hhmm(row['completed_at'])}"
    if row["cant_at"]:
        who = _e(row["cant_by_name"] or "Неизвестно")
        reason = _e(row["cant_reason"] or "без причины")
        return f"{indent}🚫 {name}\n{indent}   👤 {who} не смог: {reason}"
    extra = f" (откладывал {row['snooze_count']} р.)" if row["snooze_count"] else ""
    return f"{indent}{pending_icon} {name}\n{indent}   Не выполнено{extra}"


class Aquarium:
    def __init__(self, db: Database, tz: datetime.tzinfo) -> None:
        self.db = db
        self.tz = tz

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(self.tz)

    async def _rows(self, sql: str, params: tuple = ()) -> list[Row]:
        async with self.db.conn.execute(sql, params) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    async def _row(self, sql: str, params: tuple = ()) -> Row | None:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------ настройки

    async def get_setting(self, key: str) -> str | None:
        row = await self.db._fetchone("SELECT value FROM aq_settings WHERE key = ?", (key,))
        return row[0] if row else None

    async def set_setting(self, key: str, value: str) -> None:
        await self.db._exec("INSERT OR REPLACE INTO aq_settings (key, value) VALUES (?, ?)", (key, value))

    async def delete_setting(self, key: str) -> None:
        await self.db._exec("DELETE FROM aq_settings WHERE key = ?", (key,))

    async def once(self, key: str, day: str) -> bool:
        """True, если событие key сегодня ещё не случалось (и отмечает его)."""
        if await self.get_setting(key) == day:
            return False
        await self.set_setting(key, day)
        return True

    # ------------------------------------------------------------ пауза

    async def pause_until(self) -> str | datetime.datetime | None:
        value = await self.get_setting("paused_until")
        if value is None or value == PAUSE_FOREVER:
            return value
        return datetime.datetime.fromisoformat(value)

    async def is_paused(self, now: datetime.datetime | None = None) -> bool:
        until = await self.pause_until()
        if until is None:
            return False
        return until == PAUSE_FOREVER or (now or self.now()) < until

    async def pause_text(self) -> str:
        until = await self.pause_until()
        if until == PAUSE_FOREVER:
            return "до команды /aqresume"
        return f"до {until.astimezone(self.tz):%d.%m.%Y %H:%M}" if until else ""

    async def pause(self, days: int | None) -> None:
        value = PAUSE_FOREVER if days is None else (self.now() + datetime.timedelta(days=days)).isoformat()
        await self.set_setting("paused_until", value)

    async def resume(self, now: datetime.datetime | None = None) -> None:
        await self.delete_setting("paused_until")
        # Задачи, время которых пришлось на паузу, задним числом не досылаем
        await self.set_setting("resumed_at", (now or self.now()).isoformat())

    # ------------------------------------------------------------ расписание

    async def schedule(self) -> list[ScheduleItem]:
        overrides = dict(await self.db._fetchall("SELECT task_id, time FROM aq_schedule"))
        items = []
        for task_id, (hour, minute), days in DEFAULT_SCHEDULE:
            if task_id in overrides:
                hour, minute = map(int, overrides[task_id].split(":"))
            items.append(ScheduleItem(task_id, datetime.time(hour, minute), days))
        return items

    async def set_override(self, task_id: str, value: str | None) -> None:
        if value is None:
            await self.db._exec("DELETE FROM aq_schedule WHERE task_id = ?", (task_id,))
        else:
            await self.db._exec("INSERT OR REPLACE INTO aq_schedule (task_id, time) VALUES (?, ?)",
                                (task_id, value))

    async def tasks_for(self, day: datetime.date) -> list[tuple[datetime.time, str]]:
        items = [(i.at, i.task_id) for i in await self.schedule() if day.weekday() in i.days]
        return sorted(items)

    @staticmethod
    def is_fasting(day: datetime.date) -> bool:
        return day.weekday() in FASTING_DAYS

    async def feed_time(self) -> datetime.time:
        return next(i.at for i in await self.schedule() if i.task_id == "feed")

    def at(self, day: datetime.date, time: datetime.time) -> datetime.datetime:
        return datetime.datetime.combine(day, time, tzinfo=self.tz)

    # ------------------------------------------------------------ задачи

    async def save_task(self, task_id: str, now: datetime.datetime | None = None) -> tuple[str, bool]:
        """Создаёт задачу на сегодня. (дата, False) — такая задача сегодня уже была."""
        now = now or self.now()
        date = now.date().isoformat()
        # Тестовую задачу можно отправлять сколько угодно раз, настоящую — раз в день
        verb = "INSERT OR REPLACE" if task_id in TEST_TASKS else "INSERT OR IGNORE"
        cur = await self.db._exec(
            f"{verb} INTO aq_tasks (date, task_id, task_name, sent_at, remind_at, overdue_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (date, task_id, ALL_TASKS[task_id], now.isoformat(),
             (now + REMIND_AFTER).isoformat(), (now + OVERDUE_AFTER).isoformat()),
        )
        return date, cur.rowcount > 0

    async def get_task(self, date: str, task_id: str) -> Row | None:
        return await self._row("SELECT * FROM aq_tasks WHERE date = ? AND task_id = ?", (date, task_id))

    async def complete(self, date: str, task_id: str, user_id: int, name: str) -> tuple[Row | None, bool]:
        """(задача, True) — отмечена именно сейчас; False — уже была выполнена."""
        cur = await self.db._exec(
            "UPDATE aq_tasks SET completed_at = ?, completed_by = ?, completed_by_name = ? "
            "WHERE date = ? AND task_id = ? AND completed_at IS NULL",
            (self.now().isoformat(), user_id, name, date, task_id),
        )
        return await self.get_task(date, task_id), cur.rowcount > 0

    async def snooze(self, date: str, task_id: str) -> tuple[Row | None, bool]:
        row = await self.get_task(date, task_id)
        if row is None or row["completed_at"] or row["cant_at"] or row["snooze_count"] >= MAX_SNOOZES:
            return row, False
        remind_at = self.now() + SNOOZE_FOR
        # После отложенного напоминания у человека должно остаться ещё полчаса до просрочки
        overdue_at = max(overdue_at_of(row), remind_at + REMIND_AFTER)
        await self.db._exec(
            "UPDATE aq_tasks SET remind_at = ?, reminded = 0, overdue_at = ?, "
            "snooze_count = snooze_count + 1 WHERE date = ? AND task_id = ?",
            (remind_at.isoformat(), overdue_at.isoformat(), date, task_id),
        )
        return await self.get_task(date, task_id), True

    async def cant(self, date: str, task_id: str, name: str, reason: str) -> tuple[Row | None, bool]:
        cur = await self.db._exec(
            "UPDATE aq_tasks SET cant_at = ?, cant_reason = ?, cant_by_name = ? "
            "WHERE date = ? AND task_id = ? AND completed_at IS NULL AND cant_at IS NULL",
            (self.now().isoformat(), reason, name, date, task_id),
        )
        return await self.get_task(date, task_id), cur.rowcount > 0

    def _no_tests(self) -> tuple[str, tuple]:
        marks = ", ".join("?" for _ in TEST_TASKS)
        return f"task_id NOT IN ({marks})", tuple(TEST_TASKS)

    async def day_tasks(self, date: str) -> list[Row]:
        cond, params = self._no_tests()
        return await self._rows(f"SELECT * FROM aq_tasks WHERE date = ? AND {cond} ORDER BY sent_at",
                                (date, *params))

    async def pending(self, date: str) -> list[Row]:
        """Невыполненные задачи за день (включая тестовые), без «не могу»."""
        return await self._rows(
            "SELECT * FROM aq_tasks WHERE date = ? AND completed_at IS NULL AND cant_at IS NULL "
            "ORDER BY sent_at", (date,)
        )

    async def overdue(self, now: datetime.datetime, include_test: bool = True) -> list[Row]:
        return [r for r in await self.pending(now.date().isoformat())
                if (include_test or r["task_id"] not in TEST_TASKS) and now >= overdue_at_of(r)]

    async def set_flag(self, date: str, task_id: str, column: str) -> None:
        assert column in ("reminded", "overdue_notified")
        await self.db._exec(f"UPDATE aq_tasks SET {column} = 1 WHERE date = ? AND task_id = ?",
                            (date, task_id))

    async def since(self, start: str) -> list[Row]:
        cond, params = self._no_tests()
        return await self._rows(
            f"SELECT * FROM aq_tasks WHERE date >= ? AND {cond} ORDER BY date DESC, sent_at DESC",
            (start, *params),
        )

    async def insert_overdue_test(self, now: datetime.datetime) -> None:
        """Тестовая задача, «отправленная» 2 часа назад: напоминание и просрочка придут сразу."""
        await self.db._exec(
            "INSERT OR REPLACE INTO aq_tasks (date, task_id, task_name, sent_at) VALUES (?, ?, ?, ?)",
            (now.date().isoformat(), "overdue_test", TEST_TASKS["overdue_test"],
             (now - datetime.timedelta(hours=2)).isoformat()),
        )

    # ------------------------------------------------------------ серии и статистика

    async def streaks(self, today: str) -> tuple[int, int]:
        """Серия — дни подряд, где выполнено всё. Дни без задач серию не рвут,
        сегодняшний день засчитывается, только когда всё уже сделано. (текущая, лучшая)."""
        cond, params = self._no_tests()
        rows = await self.db._fetchall(
            f"SELECT date, COUNT(*), SUM(completed_at IS NOT NULL) FROM aq_tasks WHERE {cond} "
            "GROUP BY date ORDER BY date", params,
        )
        current = best = 0
        for date, total, done in rows:
            full = done == total
            if date == today and not full:
                continue
            current = current + 1 if full else 0
            best = max(best, current)
        return current, best

    async def achieved(self) -> list[int]:
        return [r[0] for r in await self.db._fetchall("SELECT streak FROM aq_achievements ORDER BY streak")]

    async def award(self, today: str) -> list[int]:
        current, _ = await self.streaks(today)
        have = set(await self.achieved())
        new = [m for m in ACHIEVEMENTS if current >= m and m not in have]
        for m in new:
            await self.db._exec("INSERT OR IGNORE INTO aq_achievements (streak, achieved_at) VALUES (?, ?)",
                                (m, self.now().isoformat()))
        return new

    async def week_rows(self, today: datetime.date, days: int = 7) -> list[Row]:
        return await self.since((today - datetime.timedelta(days=days - 1)).isoformat())

    async def stats_text(self, today: datetime.date, days: int = 7) -> str:
        start = today - datetime.timedelta(days=days - 1)
        rows = await self.week_rows(today, days)
        lines = [f"📊 <b>Статистика за {days} дней</b>", f"{start:%d.%m} — {today:%d.%m.%Y}", ""]
        if not rows:
            return "\n".join([*lines, "Данных пока нет."])
        done = [r for r in rows if r["completed_at"]]
        cant = [r for r in rows if not r["completed_at"] and r["cant_at"]]
        reaction = [parse_dt(r["completed_at"]) - parse_dt(r["sent_at"]) for r in done]
        lines.append(f"✅ Выполнено: {len(done)} из {len(rows)} ({round(len(done) * 100 / len(rows))}%)")
        if reaction:
            avg = int(sum(d.total_seconds() for d in reaction) / len(reaction) // 60)
            lines.append(f"⏱ Вовремя (в течение часа): {sum(d <= OVERDUE_AFTER for d in reaction)}")
            lines.append(f"🕐 Среднее время реакции: {avg} мин")
        lines += [
            f"⏰ Откладывал: {sum(r['snooze_count'] or 0 for r in rows)} р.",
            f"🚫 Не смог: {len(cant)}",
            f"❌ Не выполнено: {len(rows) - len(done) - len(cant)}",
            "",
        ]
        current, best = await self.streaks(today.isoformat())
        lines += [f"🔥 Текущая серия: {current} дн.", f"🏆 Лучшая серия: {best} дн."]
        if achieved := await self.achieved():
            lines += ["", "🎖 <b>Достижения:</b>"] + [f"   {ACHIEVEMENTS.get(m, f'{m} дней')}" for m in achieved]
        return "\n".join(lines)

    async def care_summary(self, today: datetime.date) -> str:
        """Коротко об уходе за неделю — для контекста аквариумиста и советов."""
        rows = await self.week_rows(today)
        if not rows:
            return ""
        done = sum(1 for r in rows if r["completed_at"])
        lines = [f"Уход за последние 7 дней: выполнено {done} из {len(rows)} задач."]
        missed: dict[str, int] = {}
        for r in rows:
            if not r["completed_at"]:
                missed[r["task_name"]] = missed.get(r["task_name"], 0) + 1
        if missed:
            lines.append("Пропущено: " + ", ".join(f"{n} ×{c}" for n, c in missed.items()))
        reasons = [r["cant_reason"] for r in rows if r["cant_reason"]]
        if reasons:
            lines.append("Причины «не могу»: " + "; ".join(reasons))
        last = await self._row(
            "SELECT date FROM aq_tasks WHERE task_id = 'water' AND completed_at IS NOT NULL "
            "ORDER BY date DESC LIMIT 1"
        )
        if last:
            lines.append(f"Последняя подмена воды: {last['date']}.")
        current, _ = await self.streaks(today.isoformat())
        lines.append(f"Серия дней без пропусков: {current}.")
        return " ".join(lines)

    # ------------------------------------------------------------ импорт из fish_helper

    async def import_legacy(self, path: str) -> dict[str, int]:
        """Переносит базу старого fish_helper (aquarium.db). Повторный импорт ничего не дублирует."""
        src = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        src.row_factory = sqlite3.Row
        try:
            tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            if "tasks" not in tables:
                raise ValueError("это не база fish_helper: нет таблицы tasks")
            cols = ("date", "task_id", "task_name", "sent_at", "remind_at", "overdue_at", "snooze_count",
                    "completed_at", "completed_by", "completed_by_name", "cant_at", "cant_reason", "cant_by_name")
            have = {r[1] for r in src.execute("PRAGMA table_info(tasks)")}
            rows = [{c: (r[c] if c in have else None) for c in cols} for r in src.execute("SELECT * FROM tasks")]
            overrides = list(src.execute("SELECT task_id, time FROM schedule_overrides")) \
                if "schedule_overrides" in tables else []
            achievements = list(src.execute("SELECT streak, achieved_at FROM achievements")) \
                if "achievements" in tables else []
            paused = src.execute("SELECT value FROM settings WHERE key = 'paused_until'").fetchone() \
                if "settings" in tables else None
        finally:
            src.close()

        imported = 0
        for r in rows:
            r["snooze_count"] = r["snooze_count"] or 0
            # Старые задачи уже напоминались — повторно не тревожим
            cur = await self.db.conn.execute(
                f"INSERT OR IGNORE INTO aq_tasks ({', '.join(cols)}, reminded, overdue_notified) "
                f"VALUES ({', '.join('?' for _ in cols)}, 1, 1)",
                tuple(r[c] for c in cols),
            )
            imported += cur.rowcount
        for task_id, value in overrides:
            if task_id in TASKS:
                await self.db.conn.execute("INSERT OR REPLACE INTO aq_schedule (task_id, time) VALUES (?, ?)",
                                           (task_id, value))
        for streak, at in achievements:
            await self.db.conn.execute(
                "INSERT OR IGNORE INTO aq_achievements (streak, achieved_at) VALUES (?, ?)", (streak, at)
            )
        if paused and paused[0]:
            await self.db.conn.execute(
                "INSERT OR REPLACE INTO aq_settings (key, value) VALUES ('paused_until', ?)", (paused[0],)
            )
        await self.db.conn.commit()
        return {"tasks": imported, "total": len(rows), "schedule": len(overrides),
                "achievements": len(achievements)}

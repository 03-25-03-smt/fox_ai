"""Аквариумы владельца: аквариумы, график ухода, задачи, напоминания, статистика, серии.

Данные — в таблицах aq_* общей базы. Время задач — ISO-строка с часовым поясом,
даты — по часовому поясу аквариумов (AQUARIUM_TIMEZONE). Дни недели — как в Python:
пн = 0 … вс = 6. График ухода для каждого аквариума свой: его предлагает агент
(/aqplan) по тому, что узнал об аквариуме, или задаёт владелец (/aqadd).
"""

import datetime
import html
import re
from dataclasses import dataclass
from typing import Any

from .db import Database

TEST_TASKS = {
    "test": "🧪 Тестовая задача",
    "overdue_test": "🧪 Тестовая просроченная задача",
}

REMIND_AFTER = datetime.timedelta(minutes=30)
OVERDUE_AFTER = datetime.timedelta(hours=1)
SNOOZE_FOR = datetime.timedelta(minutes=15)
MAX_SNOOZES = 3
MISSED_WINDOW = datetime.timedelta(hours=3)  # досылать задачи, пропущенные при перезапуске
LATE_NOTE_AFTER = datetime.timedelta(minutes=5)
REPORT_TIME = datetime.time(23, 15)
WEEKLY_TIME = datetime.time(23, 20)

ALL_DAYS = tuple(range(7))
SUN = 6
WEEKDAYS_RU = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
DAY_SHORT = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
DAY_ALIASES = {
    **{d: i for i, d in enumerate(DAY_SHORT)},
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}

# Вид задачи — для иконки и статистики (что пропускается чаще)
KINDS = {
    "feed": "🐟", "water": "💧", "filter": "🧽", "light": "💡", "air": "💨",
    "glass": "🪟", "plants": "🌿", "test": "🧪", "other": "📝",
}

CANT_REASONS = {
    "away": "🏠 Не дома",
    "nothing": "📦 Нечем (нет корма/средств)",
    "broken": "🛠 Что-то сломалось",
    "skip": "🙅 Не нужно сегодня",
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


def parse_days(text: str) -> tuple[int, ...] | None:
    """«пн,ср,пт», «каждый», «ежедневно», «пн-пт», «вс» -> кортеж дней; None — не понял."""
    text = text.strip().lower()
    if text in ("каждый", "ежедневно", "daily", "все", "*"):
        return ALL_DAYS
    days: set[int] = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            if a not in DAY_ALIASES or b not in DAY_ALIASES:
                return None
            i, j = DAY_ALIASES[a], DAY_ALIASES[b]
            days.update(range(i, j + 1) if i <= j else [*range(i, 7), *range(0, j + 1)])
        elif part in DAY_ALIASES:
            days.add(DAY_ALIASES[part])
        else:
            return None
    return tuple(sorted(days)) or None


def parse_dt(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value else None


def hhmm(value: str | None) -> str:
    dt = parse_dt(value)
    return dt.strftime("%H:%M") if dt else "—"


def _e(text: Any) -> str:
    return html.escape(str(text))


@dataclass(frozen=True)
class Tank:
    id: int
    name: str
    volume: float

    @property
    def label(self) -> str:
        return f"{self.name} ({self.volume:g} л)"


@dataclass(frozen=True)
class ScheduleItem:
    id: int
    tank_id: int
    title: str
    kind: str
    at: datetime.time
    days: tuple[int, ...]

    @property
    def key(self) -> str:
        return f"s{self.id}"

    @property
    def icon(self) -> str:
        return KINDS.get(self.kind, "📝")


Row = dict[str, Any]


def remind_at_of(row: Row) -> datetime.datetime:
    return parse_dt(row["remind_at"]) or parse_dt(row["sent_at"]) + REMIND_AFTER


def overdue_at_of(row: Row) -> datetime.datetime:
    return parse_dt(row["overdue_at"]) or parse_dt(row["sent_at"]) + OVERDUE_AFTER


def format_task_row(row: Row, indent: str = "", pending_icon: str = "⏳") -> str:
    name = _e(row["task_name"])
    if row["completed_at"]:
        return f"{indent}✅ {name} — {hhmm(row['completed_at'])}"
    if row["cant_at"]:
        return f"{indent}🚫 {name} — {_e(row['cant_reason'] or 'без причины')}"
    extra = f" (откладывал {row['snooze_count']} р.)" if row["snooze_count"] else ""
    return f"{indent}{pending_icon} {name}{extra}"


def parse_tanks_spec(spec: str) -> list[tuple[str, float]]:
    """«Большой:85, Малый:5» -> [("Большой", 85.0), ("Малый", 5.0)]."""
    out = []
    for part in spec.split(","):
        name, _, volume = part.strip().rpartition(":")
        try:
            out.append((name.strip() or f"{volume} л", float(volume.replace(",", "."))))
        except ValueError:
            continue
    return out


class Aquarium:
    def __init__(self, db: Database, tz: datetime.tzinfo) -> None:
        self.db = db
        self.tz = tz

    def now(self) -> datetime.datetime:
        return datetime.datetime.now(self.tz)

    def at(self, day: datetime.date, time: datetime.time) -> datetime.datetime:
        return datetime.datetime.combine(day, time, tzinfo=self.tz)

    async def _rows(self, sql: str, params: tuple = ()) -> list[Row]:
        async with self.db.conn.execute(sql, params) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, r, strict=True)) for r in await cur.fetchall()]

    async def _row(self, sql: str, params: tuple = ()) -> Row | None:
        rows = await self._rows(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------ аквариумы

    async def seed(self, spec: str) -> None:
        """Создаёт аквариумы из AQUARIUM_TANKS, если их ещё нет."""
        if await self.tanks():
            return
        for name, volume in parse_tanks_spec(spec):
            await self.add_tank(name, volume)

    async def tanks(self) -> list[Tank]:
        return [Tank(*r) for r in await self.db._fetchall("SELECT id, name, volume FROM aq_tanks ORDER BY id")]

    async def tank(self, tank_id: int) -> Tank | None:
        row = await self.db._fetchone("SELECT id, name, volume FROM aq_tanks WHERE id = ?", (tank_id,))
        return Tank(*row) if row else None

    async def add_tank(self, name: str, volume: float) -> Tank:
        cur = await self.db._exec("INSERT INTO aq_tanks (name, volume) VALUES (?, ?)", (name[:40], volume))
        return Tank(int(cur.lastrowid), name[:40], volume)

    async def find_tank(self, ref: str) -> Tank | None:
        """По номеру, объёму («85», «85л») или началу имени."""
        ref = ref.strip().lower().removesuffix("л").strip().replace(",", ".")
        tanks = await self.tanks()
        for test in (lambda t: ref == f"{t.volume:g}", lambda t: ref == f"#{t.id}" or ref == str(t.id)):
            found = [t for t in tanks if test(t)]
            if len(found) == 1:
                return found[0]
        matches = [t for t in tanks if t.name.lower().startswith(ref)] if ref else []
        return matches[0] if len(matches) == 1 else None

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

    # ------------------------------------------------------------ график ухода

    async def schedule(self, tank_id: int | None = None) -> list[ScheduleItem]:
        sql = "SELECT id, tank_id, title, kind, time, days FROM aq_plan"
        params: tuple = ()
        if tank_id is not None:
            sql, params = sql + " WHERE tank_id = ?", (tank_id,)
        items = []
        for sid, tid, title, kind, time, days in await self.db._fetchall(sql + " ORDER BY time, id", params):
            hour, minute = map(int, time.split(":"))
            items.append(ScheduleItem(sid, tid, title, kind, datetime.time(hour, minute),
                                      tuple(int(d) for d in days.split(",") if d)))
        return items

    async def item(self, item_id: int) -> ScheduleItem | None:
        return next((i for i in await self.schedule() if i.id == item_id), None)

    async def add_item(self, tank_id: int, title: str, at: datetime.time, days: tuple[int, ...],
                       kind: str = "other") -> int:
        cur = await self.db._exec(
            "INSERT INTO aq_plan (tank_id, title, kind, time, days) VALUES (?, ?, ?, ?, ?)",
            (tank_id, title[:80], kind if kind in KINDS else "other", at.strftime("%H:%M"),
             ",".join(map(str, sorted(set(days))))),
        )
        return int(cur.lastrowid)

    async def delete_item(self, item_id: int) -> bool:
        cur = await self.db._exec("DELETE FROM aq_plan WHERE id = ?", (item_id,))
        return cur.rowcount > 0

    async def set_item_time(self, item_id: int, at: datetime.time) -> bool:
        cur = await self.db._exec("UPDATE aq_plan SET time = ? WHERE id = ?", (at.strftime("%H:%M"), item_id))
        return cur.rowcount > 0

    async def replace_schedule(self, tank_id: int, items: list[dict[str, Any]]) -> int:
        await self.db._exec("DELETE FROM aq_plan WHERE tank_id = ?", (tank_id,))
        for it in items:
            await self.add_item(tank_id, it["title"], it["time"], it["days"], it.get("kind", "other"))
        return len(items)

    async def tasks_for(self, day: datetime.date) -> list[ScheduleItem]:
        return [i for i in await self.schedule() if day.weekday() in i.days]

    async def task_name(self, item: ScheduleItem) -> str:
        tank = await self.tank(item.tank_id)
        prefix = f"{tank.name}: " if tank and len(await self.tanks()) > 1 else ""
        return f"{item.icon} {prefix}{item.title}"

    # ------------------------------------------------------------ задачи

    async def save_task(self, key: str, name: str, tank_id: int | None,
                        now: datetime.datetime | None = None) -> tuple[str, bool]:
        """Создаёт задачу на сегодня. (дата, False) — такая задача сегодня уже была."""
        now = now or self.now()
        date = now.date().isoformat()
        verb = "INSERT OR REPLACE" if key in TEST_TASKS else "INSERT OR IGNORE"
        cur = await self.db._exec(
            f"{verb} INTO aq_tasks (date, task_id, task_name, tank_id, sent_at, remind_at, overdue_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (date, key, name, tank_id, now.isoformat(),
             (now + REMIND_AFTER).isoformat(), (now + OVERDUE_AFTER).isoformat()),
        )
        return date, cur.rowcount > 0

    async def get_task(self, date: str, key: str) -> Row | None:
        return await self._row("SELECT * FROM aq_tasks WHERE date = ? AND task_id = ?", (date, key))

    async def complete(self, date: str, key: str, user_id: int, name: str) -> tuple[Row | None, bool]:
        cur = await self.db._exec(
            "UPDATE aq_tasks SET completed_at = ?, completed_by = ?, completed_by_name = ? "
            "WHERE date = ? AND task_id = ? AND completed_at IS NULL AND cant_at IS NULL",
            (self.now().isoformat(), user_id, name, date, key),
        )
        return await self.get_task(date, key), cur.rowcount > 0

    async def snooze(self, date: str, key: str) -> tuple[Row | None, bool]:
        row = await self.get_task(date, key)
        if row is None or row["completed_at"] or row["cant_at"] or row["snooze_count"] >= MAX_SNOOZES:
            return row, False
        remind_at = self.now() + SNOOZE_FOR
        overdue_at = max(overdue_at_of(row), remind_at + REMIND_AFTER)
        await self.db._exec(
            "UPDATE aq_tasks SET remind_at = ?, reminded = 0, overdue_at = ?, "
            "snooze_count = snooze_count + 1 WHERE date = ? AND task_id = ?",
            (remind_at.isoformat(), overdue_at.isoformat(), date, key),
        )
        return await self.get_task(date, key), True

    async def cant(self, date: str, key: str, name: str, reason: str) -> tuple[Row | None, bool]:
        cur = await self.db._exec(
            "UPDATE aq_tasks SET cant_at = ?, cant_reason = ?, cant_by_name = ? "
            "WHERE date = ? AND task_id = ? AND completed_at IS NULL AND cant_at IS NULL",
            (self.now().isoformat(), reason, name, date, key),
        )
        return await self.get_task(date, key), cur.rowcount > 0

    def _no_tests(self) -> tuple[str, tuple]:
        marks = ", ".join("?" for _ in TEST_TASKS)
        return f"task_id NOT IN ({marks})", tuple(TEST_TASKS)

    async def day_tasks(self, date: str) -> list[Row]:
        cond, params = self._no_tests()
        return await self._rows(f"SELECT * FROM aq_tasks WHERE date = ? AND {cond} ORDER BY sent_at",
                                (date, *params))

    async def pending(self, date: str) -> list[Row]:
        return await self._rows(
            "SELECT * FROM aq_tasks WHERE date = ? AND completed_at IS NULL AND cant_at IS NULL "
            "ORDER BY sent_at", (date,)
        )

    async def overdue(self, now: datetime.datetime, include_test: bool = True) -> list[Row]:
        return [r for r in await self.pending(now.date().isoformat())
                if (include_test or r["task_id"] not in TEST_TASKS) and now >= overdue_at_of(r)]

    async def set_flag(self, date: str, key: str, column: str) -> None:
        assert column in ("reminded", "overdue_notified")
        await self.db._exec(f"UPDATE aq_tasks SET {column} = 1 WHERE date = ? AND task_id = ?", (date, key))

    async def since(self, start: str, tank_id: int | None = None) -> list[Row]:
        cond, params = self._no_tests()
        if tank_id is not None:
            cond, params = cond + " AND tank_id = ?", (*params, tank_id)
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
        """Серия — дни подряд, где выполнено всё (или осознанно пропущено). Дни без задач серию
        не рвут, сегодняшний день считается, только когда всё уже закрыто. (текущая, лучшая)."""
        cond, params = self._no_tests()
        rows = await self.db._fetchall(
            f"SELECT date, COUNT(*), SUM(completed_at IS NOT NULL OR cant_reason = ?) FROM aq_tasks "
            f"WHERE {cond} GROUP BY date ORDER BY date", (CANT_REASONS["skip"], *params),
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

    async def stats_text(self, today: datetime.date, days: int = 7) -> str:
        start = today - datetime.timedelta(days=days - 1)
        rows = await self.since(start.isoformat())
        lines = [f"📊 <b>Статистика за {days} дней</b>", f"{start:%d.%m} — {today:%d.%m.%Y}", ""]
        if not rows:
            return "\n".join([*lines, "Данных пока нет."])
        done = [r for r in rows if r["completed_at"]]
        cant = [r for r in rows if not r["completed_at"] and r["cant_at"]]
        lines.append(f"✅ Выполнено: {len(done)} из {len(rows)} ({round(len(done) * 100 / len(rows))}%)")
        reaction = [parse_dt(r["completed_at"]) - parse_dt(r["sent_at"]) for r in done]
        if reaction:
            avg = int(sum(d.total_seconds() for d in reaction) / len(reaction) // 60)
            lines.append(f"🕐 Среднее время реакции: {avg} мин")
        lines += [
            f"⏰ Откладывал: {sum(r['snooze_count'] or 0 for r in rows)} р.",
            f"🚫 Пропущено с причиной: {len(cant)}",
            f"❌ Не выполнено: {len(rows) - len(done) - len(cant)}",
        ]
        for tank in await self.tanks():
            own = [r for r in rows if r["tank_id"] == tank.id]
            if own:
                ok = sum(1 for r in own if r["completed_at"])
                lines.append(f"   {_e(tank.label)}: {ok}/{len(own)}")
        current, best = await self.streaks(today.isoformat())
        lines += ["", f"🔥 Текущая серия: {current} дн.", f"🏆 Лучшая серия: {best} дн."]
        if achieved := await self.achieved():
            lines += ["", "🎖 <b>Достижения:</b>"] + [f"   {ACHIEVEMENTS.get(m, f'{m} дней')}" for m in achieved]
        return "\n".join(lines)

    async def daily_completion(self, today: datetime.date, days: int = 30) -> list[tuple[str, int, int]]:
        """[(дата, выполнено, всего)] за последние days дней — для графика."""
        start = (today - datetime.timedelta(days=days - 1)).isoformat()
        cond, params = self._no_tests()
        rows = await self.db._fetchall(
            f"SELECT date, SUM(completed_at IS NOT NULL), COUNT(*) FROM aq_tasks WHERE date >= ? AND {cond} "
            "GROUP BY date ORDER BY date", (start, *params),
        )
        return [(d, done, total) for d, done, total in rows]

    async def care_summary(self, today: datetime.date, tank_id: int | None = None) -> str:
        """Коротко об уходе за неделю — для контекста аквариумиста и советов."""
        rows = await self.since((today - datetime.timedelta(days=6)).isoformat(), tank_id)
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
            lines.append("Причины пропусков: " + "; ".join(reasons))
        return " ".join(lines)

"""Что агент знает об аквариумах и как он это узнаёт.

Знания — короткие факты по аквариуму и теме (жители, растения, оборудование…).
Пополняются:
- из ответов на «вопрос дня»: агент выбирает аквариум и тему, о которой знает меньше всего
  (или давно не обновлял), и спрашивает;
- из обычных разговоров в режиме «Аквариумист» и из /tank add.
Устаревшие факты модель помечает при извлечении, и они удаляются.
По знаниям агент предлагает график ухода для каждого аквариума (/aqplan).
"""

import datetime
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

from .aquarium import KINDS, Aquarium, Tank, days_text, parse_days
from .db import Database
from .llm import LLMError, OllamaClient

log = logging.getLogger(__name__)

TOPICS: dict[str, tuple[str, str]] = {
    "tank": ("🫙 Аквариум", "размеры, сколько он уже запущен, где стоит (окно, солнце)"),
    "fish": ("🐟 Жители", "какие виды рыб, креветок, улиток и сколько каждого"),
    "plants": ("🌿 Растения", "какие растения, живые или искусственные, как растут"),
    "equipment": ("⚙️ Оборудование", "фильтр (тип), компрессор, обогреватель, лампа и сколько часов свет"),
    "substrate": ("🪨 Грунт и декор", "какой грунт, коряги, камни, укрытия"),
    "water": ("💧 Вода", "температура, какой водой подменяют, сколько процентов и как готовят"),
    "food": ("🍤 Корм", "какой корм, сколько и как часто дают"),
    "health": ("🩺 Здоровье", "как себя ведут жители, были ли болезни, гибель, новые жители"),
}
REFRESH_DAYS = {"health": 7, "fish": 21, "water": 21}
DEFAULT_REFRESH = 45
NOT_AGAIN_DAYS = 2  # одну и ту же тему одного аквариума не спрашивать чаще

FALLBACK_QUESTIONS = {
    "tank": "Сколько аквариум уже запущен и где стоит — далеко ли от окна?",
    "fish": "Кто сейчас живёт в аквариуме? Напиши виды (рыбы, креветки, улитки) и сколько каждого.",
    "plants": "Какие растения в аквариуме — живые или искусственные? Как они выглядят?",
    "equipment": "Какой стоит фильтр, есть ли обогреватель и компрессор? Сколько часов в день горит свет?",
    "substrate": "Какой в аквариуме грунт и что есть из декора (коряги, камни, укрытия)?",
    "water": "Какая сейчас температура воды? Какой водой подменяешь и сколько процентов за раз?",
    "food": "Каким кормом кормишь, сколько раз в день и сколько за раз?",
    "health": "Как жители в последние дни? Все активные, едят, никто не прячется и не трёт бока?",
}

# Параметры тестов воды: (название, единица, безопасный минимум, безопасный максимум)
WATER_PARAMS: dict[str, tuple[str, str, float | None, float | None]] = {
    "ph": ("pH", "", 6.5, 8.0),
    "no2": ("NO₂ (нитриты)", "мг/л", None, 0.1),
    "no3": ("NO₃ (нитраты)", "мг/л", None, 40.0),
    "nh4": ("NH₃/NH₄ (аммиак)", "мг/л", None, 0.1),
    "kh": ("KH", "°dKH", 3.0, 15.0),
    "gh": ("GH", "°dGH", 4.0, 20.0),
    "t": ("Температура", "°C", 22.0, 28.0),
    "cl2": ("Cl₂ (хлор)", "мг/л", None, 0.0),
}
WATER_ALIASES = {
    "ph": "ph", "no2": "no2", "нитриты": "no2", "no3": "no3", "нитраты": "no3",
    "nh3": "nh4", "nh4": "nh4", "аммиак": "nh4", "kh": "kh", "gh": "gh",
    "t": "t", "temp": "t", "температура": "t", "°c": "t", "cl": "cl2", "cl2": "cl2", "хлор": "cl2",
}


@dataclass(frozen=True)
class Fact:
    id: int
    tank_id: int | None
    topic: str
    text: str
    created_at: str


def parse_water(text: str) -> dict[str, float]:
    """«pH 7.2, NO2 0 no3=25 T 25,5» -> {"ph": 7.2, "no2": 0, "no3": 25, "t": 25.5}."""
    result = {}
    for name, value in re.findall(r"([A-Za-zА-Яа-я°₂₃₄]+[0-9₂₃₄]?)\s*[:=]?\s*(-?\d+(?:[.,]\d+)?)", text):
        key = WATER_ALIASES.get(name.lower().translate(str.maketrans("₂₃₄", "234")))
        if key:
            result[key] = float(value.replace(",", "."))
    return result


def water_warnings(values: dict[str, float]) -> list[str]:
    out = []
    for key, value in values.items():
        name, unit, low, high = WATER_PARAMS[key]
        unit = f" {unit}" if unit else ""
        if low is not None and value < low:
            out.append(f"{name} {value:g}{unit} — ниже нормы ({low:g})")
        elif high is not None and value > high:
            out.append(f"{name} {value:g}{unit} — выше нормы ({high:g})")
    return out


def _json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _utc(now: datetime.datetime) -> str:
    return now.astimezone(datetime.UTC).strftime("%Y-%m-%d %H:%M:%S")


def parse_plan(raw: str) -> list[dict[str, Any]]:
    """Ответ модели с графиком -> пункты {title, kind, time, days, why}. Кривые пункты пропускаются."""
    items = []
    for it in _json(raw).get("items") or []:
        if not isinstance(it, dict):
            continue
        title = str(it.get("title") or "").strip()
        try:
            hour, minute = map(int, str(it.get("time") or "").split(":"))
            at = datetime.time(hour, minute)
        except ValueError:
            continue
        raw_days = it.get("days")
        if isinstance(raw_days, list):
            days = tuple(sorted({int(d) for d in raw_days if isinstance(d, int) and 0 <= d <= 6}))
        else:
            days = parse_days(str(raw_days or "")) or ()
        if not title or not days:
            continue
        kind = str(it.get("kind") or "other")
        items.append({"title": title[:80], "kind": kind if kind in KINDS else "other", "time": at,
                      "days": days, "why": str(it.get("why") or "").strip()[:200]})
    return items[:12]


def plan_text(items: list[dict[str, Any]]) -> str:
    lines = []
    for it in sorted(items, key=lambda i: (i["time"], i["title"])):
        why = f" — <i>{it['why']}</i>" if it.get("why") else ""
        lines.append(f"{KINDS.get(it['kind'], '📝')} {it['time']:%H:%M} · {days_text(it['days'])} · "
                     f"{it['title']}{why}")
    return "\n".join(lines)


class AquariumBrain:
    def __init__(self, db: Database, rng: random.Random | None = None) -> None:
        self.db = db
        self.rng = rng or random.Random()
        self.answering: dict[int, int] = {}  # user_id -> id вопроса, на который он отвечает
        self.proposals: dict[int, list[dict[str, Any]]] = {}  # tank_id -> предложенный график

    # ------------------------------------------------------------ факты

    async def facts(self, tank_id: int | None = None, topic: str | None = None) -> list[Fact]:
        sql, params = "SELECT id, tank_id, topic, text, created_at FROM aq_facts WHERE 1 = 1", []
        if tank_id is not None:
            sql += " AND (tank_id = ? OR tank_id IS NULL)"
            params.append(tank_id)
        if topic:
            sql += " AND topic = ?"
            params.append(topic)
        return [Fact(*r) for r in await self.db._fetchall(sql + " ORDER BY tank_id, topic, id", tuple(params))]

    async def add_fact(self, tank_id: int | None, topic: str, text: str, source: str = "") -> int | None:
        if topic not in TOPICS:
            topic = "health" if topic in ("events", "event") else "tank"
        text = text.strip()[:300]
        if not text:
            return None
        dup = await self.db._fetchone(
            "SELECT id FROM aq_facts WHERE lower(text) = lower(?) AND tank_id IS ?", (text, tank_id)
        )
        if dup:
            return None
        cur = await self.db._exec("INSERT INTO aq_facts (tank_id, topic, text, source) VALUES (?, ?, ?, ?)",
                                  (tank_id, topic, text, source))
        return int(cur.lastrowid)

    async def delete_fact(self, fact_id: int) -> bool:
        cur = await self.db._exec("DELETE FROM aq_facts WHERE id = ?", (fact_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------ вода

    async def add_water(self, tank_id: int, values: dict[str, float]) -> None:
        for key, value in values.items():
            await self.db._exec("INSERT INTO aq_water (tank_id, param, value) VALUES (?, ?, ?)",
                                (tank_id, key, value))

    async def water_history(self, tank_id: int, limit: int = 5) -> dict[str, list[tuple[str, float]]]:
        """{параметр: [(дата UTC, значение), …] от новых к старым}."""
        out: dict[str, list[tuple[str, float]]] = {}
        for key in WATER_PARAMS:
            rows = await self.db._fetchall(
                "SELECT at, value FROM aq_water WHERE tank_id = ? AND param = ? ORDER BY id DESC LIMIT ?",
                (tank_id, key, limit),
            )
            if rows:
                out[key] = [(at, value) for at, value in rows]
        return out

    async def water_rows(self, tank_id: int, days: int = 90) -> list[tuple[str, str, float]]:
        """[(параметр, дата UTC, значение)] за последние days дней, по времени."""
        since = _utc(datetime.datetime.now(datetime.UTC) - datetime.timedelta(days=days))
        rows = await self.db._fetchall(
            "SELECT param, at, value FROM aq_water WHERE tank_id = ? AND at >= ? ORDER BY at, id",
            (tank_id, since),
        )
        return [(p, at, v) for p, at, v in rows]

    # ------------------------------------------------------------ вопрос дня

    async def choose_topic(self, tanks: list[Tank], now: datetime.datetime) -> tuple[Tank, str] | None:
        """(аквариум, тема), о которой агент знает меньше всего или давно не спрашивал."""
        if not tanks:
            return None
        recent = set(await self.db._fetchall(
            "SELECT tank_id, topic FROM aq_questions WHERE asked_at >= ?",
            (_utc(now - datetime.timedelta(days=NOT_AGAIN_DAYS)),),
        ))
        unknown, stale = [], []
        for tank in tanks:
            stats = dict.fromkeys(TOPICS, (0, None))
            for topic, count, newest in await self.db._fetchall(
                "SELECT topic, COUNT(*), MAX(created_at) FROM aq_facts WHERE tank_id = ? GROUP BY topic",
                (tank.id,),
            ):
                if topic in stats:
                    stats[topic] = (count, newest)
            for order, (topic, (count, newest)) in enumerate(stats.items()):
                if (tank.id, topic) in recent:
                    continue
                if count == 0:
                    unknown.append((order, tank.id, tank, topic))
                    continue
                age = now.astimezone(datetime.UTC).replace(tzinfo=None) - datetime.datetime.fromisoformat(newest)
                if age.days >= REFRESH_DAYS.get(topic, DEFAULT_REFRESH):
                    stale.append((age, tank.id, tank, topic))
        if unknown:
            # Сначала самое базовое; между аквариумами — по очереди
            _, _, tank, topic = min(unknown, key=lambda u: (u[0], u[1]))
            return tank, topic
        if stale:
            _, _, tank, topic = max(stale, key=lambda s: (s[0], -s[1]))
            return tank, topic
        return None

    async def write_question(self, llm: OllamaClient, model: str, tank: Tank, topic: str) -> str:
        known = [f for f in await self.facts(tank.id, topic) if f.tank_id == tank.id]
        title, hint = TOPICS[topic]
        if not known:
            return FALLBACK_QUESTIONS[topic]
        try:
            text = await llm.chat(model, [
                {"role": "system", "content": "Ты аквариумист и ведёшь заметки об аквариумах владельца. "
                                              "Пишешь по-русски, дружелюбно и коротко, на «ты»."},
                {"role": "user", "content": (
                    f"Аквариум: {tank.label}. Тема: {title} ({hint}). Уже известно:\n"
                    + "\n".join(f"- {f.text} (записано {f.created_at[:10]})" for f in known)
                    + "\n\nЗадай ОДИН короткий вопрос владельцу, чтобы уточнить или обновить эти сведения "
                      "(что изменилось, чего не хватает). Только вопрос, без вступления."
                )},
            ], options={"temperature": 0.6})
        except LLMError as exc:
            log.warning("aquarium question failed: %s", exc)
            return FALLBACK_QUESTIONS[topic]
        text = text.strip().strip('"«»')
        return text[:400] if len(text) > 5 else FALLBACK_QUESTIONS[topic]

    async def log_question(self, user_id: int, tank_id: int, topic: str, question: str,
                           now: datetime.datetime | None = None) -> int:
        asked = now or datetime.datetime.now(datetime.UTC)
        cur = await self.db._exec(
            "INSERT INTO aq_questions (user_id, tank_id, topic, question, asked_at) VALUES (?, ?, ?, ?, ?)",
            (user_id, tank_id, topic, question, _utc(asked)),
        )
        return int(cur.lastrowid)

    async def set_question_msg(self, question_id: int, tg_msg_id: int) -> None:
        await self.db._exec("UPDATE aq_questions SET tg_msg_id = ? WHERE id = ?", (tg_msg_id, question_id))

    async def get_question(self, question_id: int) -> tuple[int, int, int, str, str, int] | None:
        """(id, user_id, tank_id, тема, текст, answered)."""
        row = await self.db._fetchone(
            "SELECT id, user_id, tank_id, topic, question, answered FROM aq_questions WHERE id = ?",
            (question_id,),
        )
        return tuple(row) if row else None

    async def question_by_msg(self, user_id: int, tg_msg_id: int) -> int | None:
        row = await self.db._fetchone(
            "SELECT id FROM aq_questions WHERE user_id = ? AND tg_msg_id = ? AND answered = 0",
            (user_id, tg_msg_id),
        )
        return row[0] if row else None

    async def close_question(self, question_id: int, answered: bool = True) -> None:
        await self.db._exec("UPDATE aq_questions SET answered = ? WHERE id = ?",
                            (1 if answered else -1, question_id))

    # ------------------------------------------------------------ обучение

    async def learn(self, llm: OllamaClient, model: str, text: str, tanks: list[Tank], *, source: str,
                    question: str | None = None, tank: Tank | None = None) -> list[str]:
        """Извлекает из сообщения факты об аквариумах. Возвращает новые факты."""
        if not tanks:
            return []
        known = await self.facts()
        names = {t.id: t.label for t in tanks}
        known_text = "\n".join(
            f"{f.id}. [{names.get(f.tank_id, 'общее')} / {f.topic}] {f.text}" for f in known
        ) or "пока ничего"
        tanks_text = "; ".join(f"{t.id} — {t.label}" for t in tanks)
        context = f"Вопрос агента про аквариум «{tank.label}»: {question}\n" if question and tank else ""
        try:
            raw = await llm.chat(model, [
                {"role": "system", "content": "Ты ведёшь заметки об аквариумах владельца. Отвечаешь только JSON."},
                {"role": "user", "content": (
                    f"Аквариумы: {tanks_text}.\nУже известно:\n{known_text}\n\n{context}"
                    f"Сообщение владельца: {text}\n\n"
                    "Выпиши НОВЫЕ факты об этих аквариумах (жители и их число, растения, оборудование, "
                    "грунт, вода, корм, здоровье, события) — коротко, по-русски, каждый отдельно, с номером "
                    "аквариума (tank) или null, если касается всех. Не повторяй известное. Если сообщение "
                    "опровергает или обновляет известный факт — укажи его номер в outdated. "
                    "Если фактов нет — пустые списки.\n"
                    f'Темы: {", ".join(TOPICS)}.\n'
                    'JSON: {"facts": [{"tank": 1, "topic": "fish", "text": "..."}], "outdated": [номера]}'
                )},
            ], json_mode=True, options={"temperature": 0})
        except LLMError as exc:
            log.warning("aquarium learn failed: %s", exc)
            return []
        data = _json(raw)
        known_ids = {f.id for f in known}
        for fid in data.get("outdated") or []:
            if isinstance(fid, int) and fid in known_ids:
                await self.delete_fact(fid)
        default = tank.id if tank else (tanks[0].id if len(tanks) == 1 else None)
        added = []
        for item in data.get("facts") or []:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                continue
            tid = item.get("tank")
            tid = tid if isinstance(tid, int) and tid in names else default
            if await self.add_fact(tid, str(item.get("topic") or ""), item["text"], source):
                prefix = f"{names[tid]}: " if tid in names and len(tanks) > 1 else ""
                added.append(prefix + item["text"].strip())
        return added

    # ------------------------------------------------------------ контекст

    async def tank_context(self, tank: Tank) -> str:
        facts = [f for f in await self.facts(tank.id) if f.tank_id == tank.id]
        lines = [f"Аквариум «{tank.label}»:"]
        for topic, (title, _) in TOPICS.items():
            items = [f.text for f in facts if f.topic == topic]
            if items:
                lines.append(f"  {title}: " + "; ".join(items))
        missing = [TOPICS[t][0] for t in TOPICS if not any(f.topic == t for f in facts)]
        if missing:
            lines.append("  Неизвестно: " + ", ".join(missing))
        water = await self.water_history(tank.id, 1)
        if water:
            parts = []
            for key, values in water.items():
                name, unit, _, _ = WATER_PARAMS[key]
                parts.append(f"{name} {values[0][1]:g}{(' ' + unit) if unit else ''} ({values[0][0][:10]})")
            lines.append("  Последние тесты воды: " + ", ".join(parts))
        return "\n".join(lines)

    async def context(self, aquarium: Aquarium, care_summary: str = "") -> str:
        tanks = await aquarium.tanks()
        if not tanks:
            return "Аквариумы ещё не заведены (команда /aqtank add)."
        parts = ["Что известно об аквариумах владельца:"]
        for tank in tanks:
            parts.append(await self.tank_context(tank))
            plan = await aquarium.schedule(tank.id)
            if plan:
                parts.append("  График ухода: " + "; ".join(
                    f"{i.title} {i.at:%H:%M} ({days_text(i.days)})" for i in plan))
        common = [f.text for f in await self.facts() if f.tank_id is None]
        if common:
            parts.append("Общее: " + "; ".join(common))
        if care_summary:
            parts.append(care_summary)
        return "\n".join(parts)

    async def propose_plan(self, llm: OllamaClient, model: str, aquarium: Aquarium,
                           tank: Tank) -> list[dict[str, Any]]:
        """График ухода для аквариума по тому, что о нём известно."""
        current = await aquarium.schedule(tank.id)
        current_text = "; ".join(f"{i.title} {i.at:%H:%M} ({days_text(i.days)})" for i in current) or "нет"
        try:
            raw = await llm.chat(model, [
                {"role": "system", "content": "Ты опытный аквариумист. Отвечаешь только JSON."},
                {"role": "user", "content": (
                    f"{await self.tank_context(tank)}\nТекущий график: {current_text}\n\n"
                    "Составь недельный график ухода именно для этого аквариума с учётом объёма и жителей: "
                    "кормление (с разгрузочным днём, если уместно), подмены воды (процент — в названии), "
                    "чистка фильтра/губки, свет (вкл/выкл, если нужен таймер вручную), стёкла, растения, "
                    "тесты воды. Для маленького аквариума подмены чаще и меньше. Без лишних пунктов. "
                    "Время — удобное человеку, который учится днём (утро до 8:00 и вечер после 18:00, "
                    "в выходные можно днём).\n"
                    f'Виды: {", ".join(KINDS)}. Дни: 0 = пн … 6 = вс.\n'
                    'JSON: {"items": [{"title": "Подмена 25% воды", "kind": "water", "time": "12:00", '
                    '"days": [6], "why": "коротко зачем"}]}'
                )},
            ], json_mode=True, options={"temperature": 0.3})
        except LLMError as exc:
            log.warning("aquarium plan failed: %s", exc)
            return []
        items = parse_plan(raw)
        if items:
            self.proposals[tank.id] = items
        return items

    async def advice(self, llm: OllamaClient, model: str, aquarium: Aquarium, care_summary: str) -> str:
        """Советы на неделю по знаниям, графику, статистике ухода и тестам воды."""
        context = await self.context(aquarium, care_summary)
        try:
            text = await llm.chat(model, [
                {"role": "system", "content": "Ты опытный аквариумист. Пишешь по-русски, коротко и по делу."},
                {"role": "user", "content": (
                    f"{context}\n\nДай 2–4 конкретных совета на следующую неделю именно для этих аквариумов "
                    "(по пропускам ухода, параметрам воды, жителям; укажи, к какому аквариуму относится совет). "
                    "Если важных данных не хватает — один совет может быть «узнать/проверить …». "
                    "Списком, без вступления."
                )},
            ], options={"temperature": 0.5})
        except LLMError as exc:
            log.warning("aquarium advice failed: %s", exc)
            return ""
        return text.strip()


def water_chart_code(tank: Tank, rows: list[tuple[str, str, float]]) -> str:
    """Код matplotlib для песочницы: графики параметров воды по датам. rows: (param, at, value)."""
    series: dict[str, list[tuple[str, float]]] = {}
    for param, at, value in rows:
        series.setdefault(param, []).append((at[:10], value))
    meta = {k: [WATER_PARAMS[k][0], WATER_PARAMS[k][2], WATER_PARAMS[k][3]] for k in series}
    return f"""
import json, datetime
import matplotlib.pyplot as plt
series = json.loads({json.dumps(json.dumps(series))})
meta = json.loads({json.dumps(json.dumps(meta, ensure_ascii=False))})
n = len(series)
fig, axes = plt.subplots(n, 1, figsize=(8, 2.4 * n), squeeze=False, sharex=True)
for ax, (key, points) in zip(axes[:, 0], series.items()):
    name, low, high = meta[key]
    xs = [datetime.date.fromisoformat(d) for d, _ in points]
    ys = [v for _, v in points]
    ax.plot(xs, ys, marker="o")
    if low is not None:
        ax.axhline(low, color="orange", linestyle="--", linewidth=1)
    if high is not None:
        ax.axhline(high, color="red", linestyle="--", linewidth=1)
    ax.set_ylabel(name)
    ax.grid(alpha=0.3)
axes[0, 0].set_title({json.dumps(f"Вода: {tank.label}", ensure_ascii=False)})
fig.autofmt_xdate()
"""


def care_chart_code(points: list[tuple[str, int, int]]) -> str:
    """Код matplotlib: процент выполненного ухода по дням."""
    return f"""
import datetime
import matplotlib.pyplot as plt
points = {points!r}
xs = [datetime.date.fromisoformat(d) for d, _, _ in points]
ys = [round(100 * done / total) if total else 0 for _, done, total in points]
fig, ax = plt.subplots(figsize=(8, 3))
ax.bar(xs, ys, color=["#4caf50" if y == 100 else "#ff9800" if y >= 50 else "#f44336" for y in ys])
ax.set_ylim(0, 105)
ax.set_ylabel("выполнено, %")
ax.set_title("Уход за аквариумами по дням")
ax.grid(axis="y", alpha=0.3)
fig.autofmt_xdate()
"""

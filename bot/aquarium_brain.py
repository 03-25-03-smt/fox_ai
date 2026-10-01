"""Что агент знает об аквариуме и как он это узнаёт.

Знания — короткие факты по темам (рыбы, растения, оборудование…). Пополняются:
- из ответов на «вопрос дня»: агент сам выбирает тему, о которой знает меньше всего
  (или давно не обновлял), и спрашивает;
- из обычных разговоров в режиме «Аквариумист» и из /tank add.
Устаревшие факты модель помечает при извлечении, и они удаляются (рыб стало меньше и т.п.).
Всё это, плюс статистика ухода и тесты воды, подмешивается в контекст аквариумиста.
"""

import datetime
import json
import logging
import random
import re
from dataclasses import dataclass
from typing import Any

from .db import Database
from .llm import LLMError, OllamaClient

log = logging.getLogger(__name__)

# Темы, о которых агент старается знать; описание — подсказка для вопроса
TOPICS: dict[str, tuple[str, str]] = {
    "tank": ("🫙 Аквариум", "объём в литрах, размеры, сколько он уже запущен"),
    "fish": ("🐟 Рыбы", "какие виды рыб и других жителей (улитки, креветки), сколько каждого"),
    "plants": ("🌿 Растения", "какие растения, живые или искусственные, как растут"),
    "equipment": ("⚙️ Оборудование", "фильтр (тип), компрессор, обогреватель, лампа и сколько часов свет"),
    "substrate": ("🪨 Грунт и декор", "какой грунт, коряги, камни, укрытия"),
    "water": ("💧 Вода", "температура, какой водой подменяют, сколько процентов и как готовят"),
    "food": ("🍤 Корм", "какой корм, сколько и как часто дают"),
    "health": ("🩺 Здоровье", "как себя ведут рыбы, были ли болезни, гибель, новые жители"),
}
# Тема «здоровье» не бывает «известной навсегда» — спрашиваем о ней раз в неделю
REFRESH_DAYS = {"health": 7, "fish": 21, "water": 21}
DEFAULT_REFRESH = 45
NOT_AGAIN_DAYS = 2  # одну и ту же тему не спрашивать чаще

FALLBACK_QUESTIONS = {
    "tank": "Какой объём у аквариума (в литрах) и давно ли он запущен?",
    "fish": "Кто сейчас живёт в аквариуме? Напиши виды рыб (и улиток/креветок, если есть) и сколько их.",
    "plants": "Какие растения в аквариуме — живые или искусственные? Как они выглядят?",
    "equipment": "Какой стоит фильтр и есть ли обогреватель? Сколько часов в день горит свет?",
    "substrate": "Какой в аквариуме грунт и что есть из декора (коряги, камни, укрытия)?",
    "water": "Какая сейчас температура воды? Какой водой подменяете и сколько процентов за раз?",
    "food": "Каким кормом кормите и сколько даёте за раз?",
    "health": "Как рыбки в последние дни? Все активные, едят, никто не прячется и не трёт бока?",
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
        if low is not None and value < low:
            out.append(f"{name} {value:g} {unit} — ниже нормы ({low:g})".replace("  ", " "))
        elif high is not None and value > high:
            out.append(f"{name} {value:g} {unit} — выше нормы ({high:g})".replace("  ", " "))
    return out


def _json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


class AquariumBrain:
    def __init__(self, db: Database, rng: random.Random | None = None) -> None:
        self.db = db
        self.rng = rng or random.Random()
        self.answering: dict[int, int] = {}  # user_id -> id вопроса, на который он сейчас отвечает

    # ------------------------------------------------------------ факты

    async def facts(self, topic: str | None = None) -> list[Fact]:
        sql, params = "SELECT id, topic, text, created_at FROM aq_facts", ()
        if topic:
            sql, params = sql + " WHERE topic = ?", (topic,)
        return [Fact(*r) for r in await self.db._fetchall(sql + " ORDER BY topic, id", params)]

    async def add_fact(self, topic: str, text: str, source: str = "") -> int | None:
        if topic not in TOPICS:
            topic = "health" if topic in ("events", "event") else "tank"
        text = text.strip()[:300]
        if not text:
            return None
        dup = await self.db._fetchone("SELECT id FROM aq_facts WHERE lower(text) = lower(?)", (text,))
        if dup:
            return None
        cur = await self.db._exec("INSERT INTO aq_facts (topic, text, source) VALUES (?, ?, ?)",
                                  (topic, text, source))
        return int(cur.lastrowid)

    async def delete_fact(self, fact_id: int) -> bool:
        cur = await self.db._exec("DELETE FROM aq_facts WHERE id = ?", (fact_id,))
        return cur.rowcount > 0

    # ------------------------------------------------------------ вода

    async def add_water(self, values: dict[str, float]) -> None:
        for key, value in values.items():
            await self.db._exec("INSERT INTO aq_water (param, value) VALUES (?, ?)", (key, value))

    async def water_history(self, limit: int = 5) -> dict[str, list[tuple[str, float]]]:
        """{параметр: [(дата UTC, значение), …] от новых к старым}."""
        out: dict[str, list[tuple[str, float]]] = {}
        for key in WATER_PARAMS:
            rows = await self.db._fetchall(
                "SELECT at, value FROM aq_water WHERE param = ? ORDER BY id DESC LIMIT ?", (key, limit)
            )
            if rows:
                out[key] = [(at, value) for at, value in rows]
        return out

    # ------------------------------------------------------------ вопрос дня

    async def choose_topic(self, now: datetime.datetime) -> str | None:
        """Тема, о которой агент знает меньше всего или давно не спрашивал. None — всё свежее."""
        stats = {t: (0, None) for t in TOPICS}
        for topic, count, newest in await self.db._fetchall(
            "SELECT topic, COUNT(*), MAX(created_at) FROM aq_facts GROUP BY topic"
        ):
            if topic in stats:
                stats[topic] = (count, newest)
        recent = {t for (t,) in await self.db._fetchall(
            "SELECT topic FROM aq_questions WHERE asked_at >= ?",
            ((now - datetime.timedelta(days=NOT_AGAIN_DAYS)).astimezone(datetime.UTC)
             .strftime("%Y-%m-%d %H:%M:%S"),),
        )}
        unknown = [t for t, (count, _) in stats.items() if count == 0 and t not in recent]
        if unknown:
            return unknown[0]  # по порядку TOPICS: сначала самое базовое
        stale = []
        for topic, (_, newest) in stats.items():
            if topic in recent or newest is None:
                continue
            age = now.astimezone(datetime.UTC).replace(tzinfo=None) - datetime.datetime.fromisoformat(newest)
            if age.days >= REFRESH_DAYS.get(topic, DEFAULT_REFRESH):
                stale.append((age, topic))
        return max(stale)[1] if stale else None

    async def write_question(self, llm: OllamaClient, model: str, topic: str) -> str:
        known = await self.facts(topic)
        title, hint = TOPICS[topic]
        if not known:
            return FALLBACK_QUESTIONS[topic]
        try:
            text = await llm.chat(model, [
                {"role": "system", "content": "Ты аквариумист и ведёшь заметки о домашнем аквариуме. "
                                              "Пишешь по-русски, дружелюбно и коротко."},
                {"role": "user", "content": (
                    f"Тема: {title} ({hint}). Уже известно:\n"
                    + "\n".join(f"- {f.text} (записано {f.created_at[:10]})" for f in known)
                    + "\n\nЗадай ОДИН короткий вопрос владельцу аквариума, чтобы уточнить или обновить "
                      "эти сведения (что изменилось, чего не хватает). Только вопрос, без вступления."
                )},
            ], options={"temperature": 0.6})
        except LLMError as exc:
            log.warning("aquarium question failed: %s", exc)
            return FALLBACK_QUESTIONS[topic]
        text = text.strip().strip('"«»')
        return text[:400] if 5 < len(text) else FALLBACK_QUESTIONS[topic]

    async def log_question(self, user_id: int, topic: str, question: str,
                           now: datetime.datetime | None = None) -> int:
        asked = (now or datetime.datetime.now(datetime.UTC)).astimezone(datetime.UTC)
        cur = await self.db._exec(
            "INSERT INTO aq_questions (user_id, topic, question, asked_at) VALUES (?, ?, ?, ?)",
            (user_id, topic, question, asked.strftime("%Y-%m-%d %H:%M:%S")),
        )
        return int(cur.lastrowid)

    async def set_question_msg(self, question_id: int, tg_msg_id: int) -> None:
        await self.db._exec("UPDATE aq_questions SET tg_msg_id = ? WHERE id = ?", (tg_msg_id, question_id))

    async def get_question(self, question_id: int) -> tuple[int, int, str, str, int] | None:
        """(id, user_id, тема, текст, answered)."""
        row = await self.db._fetchone(
            "SELECT id, user_id, topic, question, answered FROM aq_questions WHERE id = ?", (question_id,)
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

    async def learn(self, llm: OllamaClient, model: str, text: str, *, source: str,
                    question: str | None = None) -> list[str]:
        """Извлекает из сообщения факты об аквариуме. Возвращает новые факты."""
        known = await self.facts()
        known_text = "\n".join(f"{f.id}. [{f.topic}] {f.text}" for f in known) or "пока ничего"
        asked = f"Вопрос агента: {question}\n" if question else ""
        try:
            raw = await llm.chat(model, [
                {"role": "system", "content": "Ты ведёшь заметки о домашнем аквариуме. Отвечаешь только JSON."},
                {"role": "user", "content": (
                    f"Уже известно:\n{known_text}\n\n{asked}Сообщение владельца: {text}\n\n"
                    "Выпиши НОВЫЕ факты об этом аквариуме (жители и их число, растения, оборудование, "
                    "грунт, вода, корм, здоровье, события) — коротко, по-русски, каждый отдельно. "
                    "Не повторяй известное. Если сообщение опровергает или обновляет известный факт — "
                    "укажи его номер в outdated. Если фактов нет — пустые списки.\n"
                    f'Темы: {", ".join(TOPICS)}.\n'
                    'JSON: {"facts": [{"topic": "fish", "text": "..."}], "outdated": [номера]}'
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
        added = []
        for item in data.get("facts") or []:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                if await self.add_fact(str(item.get("topic") or ""), item["text"], source):
                    added.append(item["text"].strip())
        return added

    # ------------------------------------------------------------ контекст

    async def context(self, care_summary: str = "") -> str:
        lines = ["Что известно об этом аквариуме:"]
        facts = await self.facts()
        if facts:
            for topic, (title, _) in TOPICS.items():
                items = [f.text for f in facts if f.topic == topic]
                if items:
                    lines.append(f"{title}: " + "; ".join(items))
        else:
            lines.append("пока почти ничего — расспроси владельца (объём, рыбы, оборудование).")
        missing = [TOPICS[t][0] for t in TOPICS if not any(f.topic == t for f in facts)]
        if facts and missing:
            lines.append("Неизвестно: " + ", ".join(missing))
        water = await self.water_history(3)
        if water:
            parts = []
            for key, values in water.items():
                name, unit, _, _ = WATER_PARAMS[key]
                parts.append(f"{name} {values[0][1]:g}{(' ' + unit) if unit else ''} ({values[0][0][:10]})")
            lines.append("Последние тесты воды: " + ", ".join(parts))
        if care_summary:
            lines.append(care_summary)
        return "\n".join(lines)

    async def advice(self, llm: OllamaClient, model: str, care_summary: str) -> str:
        """Советы на неделю по знаниям, статистике ухода и тестам воды."""
        context = await self.context(care_summary)
        try:
            text = await llm.chat(model, [
                {"role": "system", "content": "Ты опытный аквариумист. Пишешь по-русски, коротко и по делу."},
                {"role": "user", "content": (
                    f"{context}\n\nДай 2–3 конкретных совета на следующую неделю именно для этого "
                    "аквариума (по пропускам ухода, параметрам воды, жителям). Если важных данных "
                    "не хватает — один совет может быть «узнать/проверить …». Списком, без вступления."
                )},
            ], options={"temperature": 0.5})
        except LLMError as exc:
            log.warning("aquarium advice failed: %s", exc)
            return ""
        return text.strip()

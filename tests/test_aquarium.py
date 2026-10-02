"""Аквариумы: дома и участники, несколько аквариумов, график ухода от агента, кнопки, отчёты,
знания, вопрос дня, тесты воды и графики."""

import datetime
import json
import zoneinfo

import pytest
from aiogram.methods import EditMessageText, SendMessage, SendPhoto

from bot.aquarium import parse_days, parse_tanks_spec
from bot.aquarium_bot import tick
from bot.aquarium_brain import parse_plan, parse_water, water_chart_code, water_warnings

from .conftest import ADMIN, FRIEND, STRANGER

PRAGUE = zoneinfo.ZoneInfo("Europe/Prague")
MONDAY = datetime.date(2026, 10, 5)  # 11.10 — воскресенье


def at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=PRAGUE)


@pytest.fixture
async def aq_env(make_env):
    env = await make_env(aquarium_timezone="Europe/Prague")
    env.aq = await env.assistant.aquariums.for_user(ADMIN)  # дом AQUARIUM_OWNER_ID создаётся сам
    env.brain = env.aq.brain
    env.big, env.nano = await env.aq.tanks()
    return env


def to_owner(env) -> list[SendMessage]:
    return [m for m in env.session.of_type(SendMessage) if m.chat_id == ADMIN]


def owner_text(env) -> str:
    return "\n".join(m.text for m in to_owner(env))


async def run(env, now: datetime.datetime) -> None:
    await tick(env.bot, env.app, now)


async def add_feed(env, time=datetime.time(7, 0), days=(0, 1, 2, 3, 4, 5, 6)) -> int:
    return await env.aq.add_item(env.big.id, "Покормить рыб", time, days, "feed")


# ---------------------------------------------------------------- разбор


def test_parsers():
    assert parse_tanks_spec("Большой:85, Нано:5") == [("Большой", 85.0), ("Нано", 5.0)]
    assert parse_days("пн-пт") == (0, 1, 2, 3, 4)
    assert parse_days("сб-пн") == (0, 5, 6)
    assert parse_days("ср,вс") == (2, 6)
    assert parse_days("каждый") == tuple(range(7))
    assert parse_days("завтра") is None
    plan = parse_plan(json.dumps({"items": [
        {"title": "Подмена 30%", "kind": "water", "time": "12:00", "days": [6], "why": "нитраты"},
        {"title": "Кормить", "kind": "feed", "time": "7:30", "days": "пн-сб"},
        {"title": "Без времени", "days": [1]},
        {"title": "Странный вид", "kind": "dance", "time": "9:00", "days": [9, 1]},
    ]}))
    assert [p["title"] for p in plan] == ["Подмена 30%", "Кормить", "Странный вид"]
    assert plan[1]["days"] == (0, 1, 2, 3, 4, 5) and plan[2]["kind"] == "other" and plan[2]["days"] == (1,)
    assert parse_plan("не json") == []


def test_water_parsing():
    assert parse_water("pH 7,2 NO2 0 no3=25 KH:6 T 25.5") == {"ph": 7.2, "no2": 0, "no3": 25, "kh": 6, "t": 25.5}
    assert parse_water("NO₂ 0.5") == {"no2": 0.5}
    warnings = water_warnings({"no2": 0.5, "ph": 7.0, "t": 20})
    assert len(warnings) == 2 and "NO₂" in warnings[0] and "ниже" in warnings[1]


async def test_tanks_seeded_once(aq_env):
    env = aq_env
    assert (env.big.label, env.nano.label) == ("Большой (85 л)", "Нано (5 л)")
    await env.aq.seed("Другой:10")
    assert len(await env.aq.tanks()) == 2
    assert (await env.aq.find_tank("85")).id == env.big.id
    assert (await env.aq.find_tank("5л")).id == env.nano.id
    assert (await env.aq.find_tank("нано")).id == env.nano.id
    assert await env.aq.find_tank("pH") is None


# ---------------------------------------------------------------- доступ


async def test_access_without_home(aq_env):
    env = aq_env
    await env.send(FRIEND, "/aq")
    assert "/aqstart" in env.last_text() and "/aqjoin" in env.last_text()
    await env.send(FRIEND, "/water 85 pH 7")  # чужие аквариумы не видны
    assert "/aqstart" in env.last_text() and not await env.brain.water_history(env.big.id)
    await env.send(FRIEND, "/help")
    assert "/aqstart 30" in env.last_text()
    await env.send(FRIEND, "/mode")
    assert "Аквариумист" in str(env.session.of_type(SendMessage)[-1].reply_markup)
    await env.send(STRANGER, "/aqstart 30")
    assert "Доступ закрыт" in env.last_text()
    await env.send(ADMIN, "/aq")
    menu = env.session.of_type(SendMessage)[-1]
    assert "Мои аквариумы" in menu.text and menu.reply_markup.keyboard[0][0].text == "📋 Сегодня"


# ---------------------------------------------------------------- график и задачи


async def test_schedule_sends_once_and_buttons(aq_env):
    env = aq_env
    await add_feed(env)
    await env.aq.add_item(env.nano.id, "Подмена 20% воды", datetime.time(7, 0), (0,), "water")
    await run(env, at(MONDAY, 6, 59))
    assert not to_owner(env)
    await run(env, at(MONDAY, 7, 0))
    text = owner_text(env)
    assert "Большой: Покормить рыб" in text and "Нано: Подмена 20% воды" in text
    await run(env, at(MONDAY, 7, 1))
    assert len(to_owner(env)) == 2

    key = (await env.aq.schedule(env.big.id))[0].key
    await env.click(ADMIN, f"aq:done:{MONDAY}:{key}")
    assert "Сделано" in env.session.of_type(EditMessageText)[-1].text
    row = await env.aq.get_task(MONDAY.isoformat(), key)
    assert row["completed_at"] and row["tank_id"] == env.big.id


async def test_missed_tasks_resent_after_restart(aq_env):
    env = aq_env
    await add_feed(env)
    await env.aq.add_item(env.big.id, "Включить свет", datetime.time(14, 30), tuple(range(7)), "light")
    await run(env, at(MONDAY, 9, 30))
    assert "С опозданием" in owner_text(env) and "07:00" in owner_text(env)
    await run(env, at(MONDAY, 17, 31))  # свет в 14:30 — больше 3 часов назад, уже не шлём
    assert "Включить свет" not in owner_text(env)


async def test_reminder_overdue_snooze_and_reasons(aq_env):
    env = aq_env
    item = await add_feed(env, datetime.time(2, 0))
    key = f"s{item}"
    await run(env, at(MONDAY, 2, 0))
    await run(env, at(MONDAY, 2, 31))
    assert "Напоминание" in owner_text(env)
    await run(env, at(MONDAY, 3, 1))
    assert "Просрочено" in owner_text(env)
    await run(env, at(MONDAY, 3, 2))
    assert owner_text(env).count("Просрочено") == 1

    date = MONDAY.isoformat()
    for _ in range(4):
        await env.click(ADMIN, f"aq:snooze:{date}:{key}")
    assert (await env.aq.get_task(date, key))["snooze_count"] == 3
    await env.click(ADMIN, f"aq:why:{date}:{key}:other")
    await env.send(ADMIN, "корм закончился")
    assert (await env.aq.get_task(date, key))["cant_reason"] == "корм закончился"
    env.llm.calls.clear()
    await env.send(ADMIN, "привет")  # дальше обычный текст снова идёт в чат
    assert env.llm.calls


async def test_pause_and_resume(aq_env):
    env = aq_env
    await add_feed(env)
    await env.send(ADMIN, "/aqpause")
    await run(env, at(MONDAY, 7, 0))
    assert "Покормить" not in owner_text(env)
    await env.aq.resume(at(MONDAY, 8, 0))
    await run(env, at(MONDAY, 8, 1))
    assert "Покормить" not in owner_text(env)  # задачи времён паузы не досылаем


async def test_manual_schedule_commands(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqadd 5 19:00 ср,вс Почистить губку фильтра")
    assert "🧽" in env.last_text() and "ср, вс" in env.last_text()
    (item,) = await env.aq.schedule(env.nano.id)
    assert item.kind == "filter" and item.days == (2, 6)
    await env.send(ADMIN, f"/aqsettime {item.id} 20:15")
    assert (await env.aq.item(item.id)).at == datetime.time(20, 15)
    await env.send(ADMIN, "📋 Сегодня")
    await env.send(ADMIN, "🗂 График ухода")
    assert "Почистить губку фильтра" in env.last_text() and "пусто" in env.last_text()
    await env.send(ADMIN, "/aqadd 19:00 ср Почистить")  # без аквариума при двух аквариумах
    assert "Укажи аквариум" in env.last_text()
    await env.send(ADMIN, f"/aqdel {item.id}")
    assert not await env.aq.schedule()


async def test_agent_proposes_plan(aq_env):
    env = aq_env
    plan = {"items": [
        {"title": "Подмена 25% воды", "kind": "water", "time": "11:00", "days": [6], "why": "5 л быстро грязнится"},
        {"title": "Покормить петушка", "kind": "feed", "time": "07:30", "days": [0, 1, 2, 3, 4, 5]},
    ]}
    env.llm.scripted.append(("недельный график", json.dumps(plan)))
    await env.brain.add_fact(env.nano.id, "fish", "Живёт петушок")
    await env.send(ADMIN, "/aqplan нано")
    offer = to_owner(env)[-1]
    assert "Предлагаю график ухода: Нано (5 л)" in offer.text and "Подмена 25% воды" in offer.text
    prompt = [c for c in env.llm.chat_calls if "недельный график" in c["messages"][-1]["content"]][0]
    assert "Живёт петушок" in prompt["messages"][-1]["content"]
    await env.click(ADMIN, f"aqp:ok:{env.nano.id}")
    titles = [i.title for i in await env.aq.schedule(env.nano.id)]
    assert titles == ["Покормить петушка", "Подмена 25% воды"]
    await env.click(ADMIN, f"aqp:ok:{env.nano.id}")
    assert "устарело" in env.last_text()


async def test_plan_offered_when_agent_knows_enough(aq_env):
    env = aq_env
    env.llm.scripted.append(("недельный график", json.dumps(
        {"items": [{"title": "Кормить", "kind": "feed", "time": "07:00", "days": [0]}]})))
    await env.brain.add_fact(env.big.id, "fish", "10 неонов")
    await env.brain.add_fact(env.big.id, "equipment", "Внешний фильтр")
    await run(env, at(MONDAY, 18, 0))
    assert "Предлагаю график ухода: Большой (85 л)" in owner_text(env)
    assert "Вопрос про" not in owner_text(env)  # в этот день вместо вопроса
    await run(env, at(MONDAY + datetime.timedelta(days=1), 18, 0))
    assert "Вопрос про" in owner_text(env)  # повторно график не навязываем


async def test_report_weekly_advice_and_chart(aq_env):
    env = aq_env
    env.llm.chat_reply = "1. Нано: подменяй 20% дважды в неделю."
    item = await add_feed(env, datetime.time(7, 0), (4, 5, 6))
    sunday = MONDAY + datetime.timedelta(days=6)
    for offset in (2, 1, 0):
        day = sunday - datetime.timedelta(days=offset)
        date, _ = await env.aq.save_task(f"s{item}", "Покормить", env.big.id, at(day, 7))
        await env.aq.complete(date, f"s{item}", ADMIN, "Влад")
    await run(env, at(sunday, 23, 15))
    assert "Серия без пропусков: 3" in owner_text(env) and "3 дня без пропусков" in owner_text(env)
    await run(env, at(sunday, 23, 20))
    weekly = owner_text(env)
    assert "Статистика за 7 дней" in weekly and "Советы на неделю" in weekly and "Большой (85 л): 3/3" in weekly
    assert env.session.of_type(SendPhoto)  # график ухода через песочницу
    assert "fig" in env.sandbox.python_runs[-1] or "plt" in env.sandbox.python_runs[-1]
    await run(env, at(sunday, 23, 25))
    assert owner_text(env).count("Статистика за 7 дней") == 1


async def test_overdue_test_command(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqoverdue_test")
    await env.send(ADMIN, "⚠️ Просрочки")
    assert "Тестовая просроченная" in env.last_text()
    await env.send(ADMIN, "/aqtest")
    assert "Тестовая задача" in owner_text(env)


# ---------------------------------------------------------------- знания и вопросы


async def test_aquarist_mode_context_and_learning(aq_env):
    env = aq_env
    await env.db.add_user(ADMIN)
    await env.db.set_mode(ADMIN, "aquarium")
    await env.brain.add_fact(env.big.id, "fish", "10 неонов")
    await env.aq.add_item(env.big.id, "Подмена 30%", datetime.time(12, 0), (6,), "water")
    env.llm.scripted.append(("Сообщение владельца", json.dumps(
        {"facts": [{"tank": env.nano.id, "topic": "fish", "text": "Живёт петушок"}], "outdated": []})))
    await env.send(ADMIN, "В нано теперь живёт петушок")
    system = env.system_prompt()
    assert "аквариумист" in system and "Большой (85 л)" in system and "10 неонов" in system
    assert "Подмена 30% 12:00 (вс)" in system
    assert [f.text for f in await env.brain.facts(env.nano.id) if f.tank_id == env.nano.id] == ["Живёт петушок"]

    fact = (await env.brain.facts(env.big.id, "fish"))[0]
    env.llm.scripted.append(("Сообщение владельца", json.dumps(
        {"facts": [{"tank": env.big.id, "topic": "fish", "text": "Неонов 9"}], "outdated": [fact.id]})))
    await env.send(ADMIN, "один неон погиб")
    assert [f.text for f in await env.brain.facts(env.big.id, "fish") if f.tank_id] == ["Неонов 9"]


async def test_question_of_the_day_alternates_tanks(aq_env):
    env = aq_env
    now = at(MONDAY, 18, 0)
    assert await env.brain.choose_topic([env.big, env.nano], now) == (env.big, "tank")
    await run(env, now)
    question = to_owner(env)[-1]
    assert "Вопрос про Большой (85 л)" in question.text
    await run(env, now + datetime.timedelta(minutes=5))
    assert owner_text(env).count("Вопрос про") == 1
    assert await env.brain.choose_topic([env.big, env.nano], now) == (env.nano, "tank")

    env.llm.scripted.append(("Вопрос агента", json.dumps(
        {"facts": [{"tank": None, "topic": "tank", "text": "Запущен год назад"}], "outdated": []})))
    (msg_id,) = await env.db._fetchone("SELECT tg_msg_id FROM aq_questions")
    await env.send(ADMIN, "просто про C")  # не ответ на вопрос — обычный чат
    assert not await env.brain.facts(env.big.id, "tank")
    asked = env.message(ADMIN, text=question.text).model_copy(update={"message_id": msg_id})
    await env.send(ADMIN, "запущен год назад, стоит у стены", reply_to_message=asked)
    facts = await env.brain.facts(env.big.id, "tank")
    assert [(f.tank_id, f.text) for f in facts] == [(env.big.id, "Запущен год назад")]  # tank из вопроса
    assert "запущен год назад, стоит у стены" in env.last_user_prompt()
    assert await env.brain.question_by_msg(ADMIN, msg_id) is None


async def test_question_buttons(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqask")
    (q,) = await env.db._fetchone("SELECT id FROM aq_questions")
    await env.click(ADMIN, f"aqq:answer:{q}")
    env.llm.scripted.append(("Вопрос агента", json.dumps(
        {"facts": [{"topic": "tank", "text": "Стоит у окна"}], "outdated": []})))
    await env.send(ADMIN, "у окна")
    assert [f.text for f in await env.brain.facts(env.big.id, "tank")] == ["Стоит у окна"]
    await env.click(ADMIN, f"aqq:answer:{q}")
    assert "уже закрыт" in str(env.session.requests[-1])
    await env.send(ADMIN, "/aqask")
    (q2,) = await env.db._fetchone("SELECT id FROM aq_questions ORDER BY id DESC")
    await env.click(ADMIN, f"aqq:skip:{q2}")
    assert (await env.brain.get_question(q2))[5] == -1


async def test_question_voice_answer(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqask")
    (q, msg_id) = await env.db._fetchone("SELECT id, tg_msg_id FROM aq_questions")
    assert "голосовым" in to_owner(env)[-1].text
    env.speech.text = "живут десять неонов и два сомика"
    env.llm.scripted.append(("Вопрос агента", json.dumps(
        {"facts": [{"topic": "fish", "text": "Неонов 10, сомиков 2"}], "outdated": []})))
    asked = env.message(ADMIN, text="вопрос").model_copy(update={"message_id": msg_id})
    await env.send_voice(ADMIN, reply_to=asked)  # голосовое reply на вопрос
    assert [f.text for f in await env.brain.facts(env.big.id, "fish")] == ["Неонов 10, сомиков 2"]
    assert "живут десять неонов" in env.last_user_prompt()
    assert (await env.brain.get_question(q))[5] != 0  # вопрос закрыт

    await env.send(ADMIN, "/aqask")  # через кнопку «Ответить» — тоже голосом
    (q2,) = await env.db._fetchone("SELECT id FROM aq_questions ORDER BY id DESC")
    await env.click(ADMIN, f"aqq:answer:{q2}")
    assert "голосовое" in env.last_text()
    env.speech.text = ""
    await env.send_voice(ADMIN)
    assert "Не расслышал" in str(env.session.requests[-1])
    assert (await env.brain.get_question(q2))[5] == 0  # вопрос ещё открыт
    env.llm.scripted.append(("Вопрос агента", json.dumps({"facts": [], "outdated": []})))
    await env.send(ADMIN, "фильтр внешний")  # можно ответить текстом
    assert (await env.brain.get_question(q2))[5] != 0


async def test_tank_commands(aq_env):
    env = aq_env
    await env.send(ADMIN, "/tank add 5 фильтр-губка от компрессора")  # модель ничего не извлекла
    assert "Запомнил" in env.last_text()
    (fact,) = await env.brain.facts(env.nano.id)
    assert fact.tank_id == env.nano.id
    await env.send(ADMIN, "🐠 Что я знаю")
    assert "фильтр-губка" in env.last_text() and "Ещё не знаю" in env.last_text()
    await env.send(ADMIN, f"/tank del {fact.id}")
    assert not await env.brain.facts()
    await env.send(ADMIN, "/aqtank add Креветочник 20")
    assert len(await env.aq.tanks()) == 3
    await env.send(ADMIN, "/aqtank")
    assert "Креветочник (20 л)" in env.last_text()


async def test_water_per_tank_with_chart(aq_env):
    env = aq_env
    await env.send(ADMIN, "/water pH 7")
    assert "Укажи аквариум" in env.last_text()
    await env.send(ADMIN, "/water 5 pH 7 NO2 0.5")
    assert "Нано (5 л)" in env.last_text() and "выше нормы" in env.last_text()
    await env.send(ADMIN, "/water 5 pH 7.2 NO2 0")
    assert "Всё в норме" in env.last_text()
    await env.send(ADMIN, "/water 85 pH 6.8")
    await env.send(ADMIN, "/water 5")
    texts = "\n".join(env.texts())
    assert "0 ← 0.5" in texts and "6.8" not in texts.split("Вода: Нано")[-1]
    assert env.session.of_type(SendPhoto)
    code = env.sandbox.python_runs[-1]
    assert "matplotlib" in code and "Нано" in code
    await env.send(ADMIN, "💧 Вода")
    assert "последний тест" in env.last_text()


def test_water_chart_code_runs():
    """Код графика корректен: компилируется и правильно экранирует данные."""
    from bot.aquarium import Tank

    code = water_chart_code(Tank(1, 'Нано "5"', 5), [("ph", "2026-10-01 10:00:00", 7.0),
                                                     ("ph", "2026-10-03 10:00:00", 7.4)])
    compile(code, "chart", "exec")
    assert '"ph": [["2026-10-01", 7.0]' in code.replace("\\", "")



# ---------------------------------------------------------------- дома и участники


def code_from(text: str) -> str:
    import re

    return re.search(r"/aqjoin ([0-9A-F]{6})", text).group(1)


def to_user(env, uid) -> str:
    return "\n".join(m.text for m in env.session.of_type(SendMessage) if m.chat_id == uid)


async def test_separate_homes(aq_env):
    """У брата свой дом с 30 л: свои задачи, свои знания, чужое не видно."""
    env = aq_env
    await env.send(FRIEND, "/aqstart 30")
    assert "Аквариум (30 л)" in env.last_text()
    friend = await env.assistant.aquariums.for_user(FRIEND)
    assert friend.id != env.aq.id and friend.is_owner(FRIEND)
    (tank,) = await friend.tanks()
    await env.send(FRIEND, "/aqadd 19:00 каждый Покормить гуппи")  # один аквариум — указывать не нужно
    await add_feed(env)
    await env.send(FRIEND, "/aqschedule")
    assert "Покормить гуппи" in env.last_text() and "Покормить рыб" not in env.last_text()
    await env.send(FRIEND, "/water 30 pH 7")
    assert await friend.brain.water_history(tank.id) and not await env.brain.water_history(env.big.id)
    await env.send(FRIEND, "/aqstart 50")
    assert "уже есть дом" in env.last_text()

    await run(env, at(MONDAY, 7, 0))
    await run(env, at(MONDAY, 19, 0))
    assert "Покормить рыб" in to_user(env, ADMIN) and "Покормить рыб" not in to_user(env, FRIEND)
    assert "Покормить гуппи" in to_user(env, FRIEND) and "Покормить гуппи" not in to_user(env, ADMIN)
    # Пункт графика другого дома ни удалить, ни отметить нельзя
    item = (await env.aq.schedule())[0]
    await env.send(FRIEND, f"/aqdel {item.id}")
    assert await env.aq.item(item.id)


async def test_invite_shared_tasks_and_assign(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqinvite")
    code = code_from(env.last_text())
    await env.send(FRIEND, "/aqjoin WRONG")
    assert "Не нашёл" in env.last_text()
    await env.send(FRIEND, f"/aqjoin {code}")
    assert "Ты в доме" in to_user(env, FRIEND) and "присоединился" in to_user(env, ADMIN)
    assert [m.user_id for m in await env.aq.members()] == [ADMIN, FRIEND]
    await env.send(FRIEND, "/aqadd 85 19:00 ср Почистить")  # помощник график не меняет
    assert "только хозяин" in env.last_text() and not await env.aq.schedule()

    item = await add_feed(env)
    await run(env, at(MONDAY, 7, 0))
    assert "Покормить рыб" in to_user(env, ADMIN) and "Покормить рыб" in to_user(env, FRIEND)
    await env.click(FRIEND, f"aq:done:{MONDAY}:s{item}")
    edits = env.session.of_type(EditMessageText)
    assert any(e.chat_id == ADMIN and "Сделал(а)" in e.text for e in edits)  # у брата — кнопки убраны
    row = await env.aq.get_task(MONDAY.isoformat(), f"s{item}")
    assert row["completed_by"] == FRIEND
    await env.click(ADMIN, f"aq:done:{MONDAY}:s{item}")  # нажал позже — видит, кто сделал
    assert "Сделано</b> (u)" in env.session.of_type(EditMessageText)[-1].text

    await env.send(ADMIN, f"/aqassign {item} u20")
    assert "делает: u20" in env.last_text()
    tuesday = MONDAY + datetime.timedelta(days=1)
    before_admin = to_user(env, ADMIN).count("Покормить рыб")
    await run(env, at(tuesday, 7, 0))
    assert to_user(env, FRIEND).count("Покормить рыб") == 2
    assert to_user(env, ADMIN).count("Покормить рыб") == before_admin
    await run(env, at(tuesday, 8, 1))  # просрочка — исполнителю и хозяину
    assert "Просрочено" in to_user(env, FRIEND) and "Просрочено" in to_user(env, ADMIN)
    assert "Исполнитель: u20" in to_user(env, ADMIN)
    await env.send(FRIEND, "🗓 Завтра")
    assert "→ u20" in env.last_text()
    await env.send(ADMIN, "/aqmembers")
    assert "хозяин" in env.last_text() and "помощник" in env.last_text()


async def test_question_rotates_and_report_to_all(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqinvite")
    await env.send(FRIEND, f"/aqjoin {code_from(env.last_text())}")
    await run(env, at(MONDAY, 18, 0))
    await run(env, at(MONDAY + datetime.timedelta(days=1), 18, 0))
    assert "Вопрос про" in to_user(env, ADMIN) and "Вопрос про" in to_user(env, FRIEND)
    item = await add_feed(env, datetime.time(7, 0))
    date, _ = await env.aq.save_task(f"s{item}", "Покормить", env.big.id, at(MONDAY, 7))
    await env.aq.complete(date, f"s{item}", FRIEND, "Брат")
    await run(env, at(MONDAY, 23, 15))
    assert "Аквариумы за день" in to_user(env, FRIEND) and "(Брат)" in to_user(env, ADMIN)


async def test_leave_transfer_and_delete(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqinvite")
    await env.send(FRIEND, f"/aqjoin {code_from(env.last_text())}")
    await env.send(ADMIN, "/aqleave")
    assert "хозяин ты" in to_user(env, FRIEND)
    assert env.aq.is_owner(FRIEND) and await env.assistant.aquariums.for_user(ADMIN) is None
    await env.send(FRIEND, "/aqleave")
    assert "последний" in env.last_text() and await env.assistant.aquariums.for_user(FRIEND)
    await env.send(FRIEND, "/aqleave да")
    assert await env.assistant.aquariums.for_user(FRIEND) is None
    assert not await env.db._fetchall("SELECT * FROM aq_tanks")
    await env.send(ADMIN, "/aqstart Большой:85,Нано:5")  # завести заново
    assert len(await (await env.assistant.aquariums.for_user(ADMIN)).tanks()) == 2


async def test_home_timezone(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqtz Mars/Base")
    assert "Не знаю" in env.last_text()
    await env.send(ADMIN, "/aqtz Europe/Kyiv")
    assert str(env.aq.tz) == "Europe/Kyiv" and env.aq.home.tz == "Europe/Kyiv"
    env.assistant.aquariums._cache.clear()
    assert str((await env.assistant.aquariums.for_user(ADMIN)).tz) == "Europe/Kyiv"


async def test_legacy_single_owner_data_migrated(tmp_path, make_env):
    """База версии с одним владельцем: всё переезжает в дом AQUARIUM_OWNER_ID без потерь."""
    import sqlite3

    con = sqlite3.connect(tmp_path / "e2e.sqlite3")
    con.executescript("""
        CREATE TABLE aq_tanks (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, volume REAL NOT NULL);
        CREATE TABLE aq_plan (id INTEGER PRIMARY KEY AUTOINCREMENT, tank_id INTEGER NOT NULL, title TEXT NOT NULL,
            kind TEXT NOT NULL DEFAULT 'other', time TEXT NOT NULL, days TEXT NOT NULL);
        CREATE TABLE aq_tasks (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT NOT NULL, task_id TEXT NOT NULL,
            task_name TEXT NOT NULL, tank_id INTEGER, sent_at TEXT NOT NULL, remind_at TEXT, overdue_at TEXT,
            reminded INTEGER NOT NULL DEFAULT 0, overdue_notified INTEGER NOT NULL DEFAULT 0,
            snooze_count INTEGER NOT NULL DEFAULT 0, completed_at TEXT, completed_by INTEGER,
            completed_by_name TEXT, cant_at TEXT, cant_reason TEXT, cant_by_name TEXT, UNIQUE (date, task_id));
        CREATE TABLE aq_settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE aq_achievements (streak INTEGER PRIMARY KEY, achieved_at TEXT NOT NULL);
        CREATE TABLE aq_facts (id INTEGER PRIMARY KEY AUTOINCREMENT, tank_id INTEGER, topic TEXT NOT NULL,
            text TEXT NOT NULL, source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT (datetime('now')));
        INSERT INTO aq_tanks (name, volume) VALUES ('Большой', 85), ('Нано', 5);
        INSERT INTO aq_plan (tank_id, title, kind, time, days) VALUES (1, 'Покормить', 'feed', '07:00', '0,1,2');
        INSERT INTO aq_tasks (date, task_id, task_name, tank_id, sent_at, completed_at)
            VALUES ('2026-10-01', 's1', 'Покормить', 1, '2026-10-01T07:00:00+02:00', '2026-10-01T07:05:00+02:00');
        INSERT INTO aq_settings VALUES ('paused_until', 'forever');
        INSERT INTO aq_achievements VALUES (3, '2026-10-01');
        INSERT INTO aq_facts (tank_id, topic, text) VALUES (1, 'fish', '10 неонов');
    """)
    con.commit()
    con.close()
    env = await make_env(aquarium_timezone="Europe/Prague")
    aq = await env.assistant.aquariums.for_user(ADMIN)
    assert [t.label for t in await aq.tanks()] == ["Большой (85 л)", "Нано (5 л)"]  # без повторного seed
    assert [i.title for i in await aq.schedule()] == ["Покормить"]
    assert (await aq.get_task("2026-10-01", "s1"))["completed_at"]
    assert await aq.is_paused() and await aq.achieved() == [3]
    assert [f.text for f in await aq.brain.facts()] == ["10 неонов"]
    await env.send(FRIEND, "/aqstart 30")  # у брата — свой пустой дом
    friend = await env.assistant.aquariums.for_user(FRIEND)
    assert [t.volume for t in await friend.tanks()] == [30] and not await friend.brain.facts()

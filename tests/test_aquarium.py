"""Аквариум (бывший fish_helper): расписание, кнопки, отчёты, знания, вопросы, импорт."""

import datetime
import json
import sqlite3
import zoneinfo

import pytest
from aiogram.methods import EditMessageText, SendMessage

from bot.aquarium_bot import tick
from bot.aquarium_brain import parse_water, water_warnings

from .conftest import ADMIN, FRIEND

BROTHER = 40
PRAGUE = zoneinfo.ZoneInfo("Europe/Prague")
# 2026-10-05 — понедельник, 08 — четверг (разгрузочный), 11 — воскресенье
MONDAY = datetime.date(2026, 10, 5)


def at(day: datetime.date, hour: int, minute: int = 0) -> datetime.datetime:
    return datetime.datetime.combine(day, datetime.time(hour, minute), tzinfo=PRAGUE)


@pytest.fixture
async def aq_env(make_env):
    env = await make_env(aquarium_caretaker_ids=str(BROTHER), aquarium_timezone="Europe/Prague",
                         aquarium_owner_name="Владу")
    env.aq = env.assistant.aquarium
    env.brain = env.assistant.aquarium_brain
    return env


def sent_to(env, chat_id: int) -> list[SendMessage]:
    return [m for m in env.session.of_type(SendMessage) if m.chat_id == chat_id]


def texts_to(env, chat_id: int) -> str:
    return "\n".join(m.text for m in sent_to(env, chat_id))


async def run(env, now: datetime.datetime) -> None:
    await tick(env.bot, env.app, now)


# ---------------------------------------------------------------- расписание и кнопки


async def test_schedule_sends_once_and_done_notifies_owner(aq_env):
    env = aq_env
    await run(env, at(MONDAY, 6, 59))
    assert not sent_to(env, BROTHER)
    await run(env, at(MONDAY, 7, 0))
    msgs = sent_to(env, BROTHER)
    assert len(msgs) == 2  # кормление и воздух
    assert "Покормить" in msgs[0].text or "Покормить" in msgs[1].text
    assert all("опозданием" not in m.text for m in msgs)
    await run(env, at(MONDAY, 7, 1))
    assert len(sent_to(env, BROTHER)) == 2  # повторно не шлём
    assert not sent_to(env, ADMIN)  # владельцу задачи не приходят

    await env.click(BROTHER, f"aq:done:{MONDAY}:feed")
    edit = env.session.of_type(EditMessageText)[-1]
    assert "Выполнено" in edit.text
    assert "выполнил" in texts_to(env, ADMIN)
    await env.click(BROTHER, f"aq:done:{MONDAY}:feed")  # второе нажатие — без второго уведомления
    assert texts_to(env, ADMIN).count("выполнил") == 1


async def test_missed_tasks_resent_after_restart(aq_env):
    env = aq_env
    await run(env, at(MONDAY, 9, 30))  # бот лежал с 7:00 — досылаем, это меньше 3 часов
    text = texts_to(env, BROTHER)
    assert "Покормить" in text and "С опозданием" in text and "07:00" in text
    assert "Досланы пропущенные" in texts_to(env, ADMIN)

    other = await env.app.assistant.aquarium.get_task(MONDAY.isoformat(), "light_on")
    assert other is None
    await run(env, at(MONDAY, 17, 31))  # свет в 14:30 — больше 3 часов назад, уже не шлём
    assert await env.aq.get_task(MONDAY.isoformat(), "light_on") is None


async def test_fasting_day(aq_env):
    env = aq_env
    thursday = MONDAY + datetime.timedelta(days=3)
    await run(env, at(thursday, 7, 0))
    text = texts_to(env, BROTHER)
    assert "разгрузочный день" in text and "Покормить" not in text and "воздух" in text
    await run(env, at(thursday, 7, 5))
    assert texts_to(env, BROTHER).count("разгрузочный") == 1


async def test_reminder_and_overdue(aq_env):
    env = aq_env
    now = at(MONDAY, 3, 0)  # ночью по расписанию ничего нет
    await env.aq.save_task("feed", now - datetime.timedelta(minutes=31))
    await run(env, now)
    assert "Напоминание" in texts_to(env, BROTHER) and not sent_to(env, ADMIN)
    await run(env, now + datetime.timedelta(minutes=30))
    assert "Просроченная задача" in texts_to(env, BROTHER) and "Просроченная задача" in texts_to(env, ADMIN)
    await run(env, now + datetime.timedelta(minutes=31))
    assert texts_to(env, ADMIN).count("Просроченная") == 1



async def test_overdue_test_command(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqoverdue_test")
    await env.send(ADMIN, "⚠️ Просрочки")
    assert "Тестовая просроченная" in env.last_text()
    await env.send(BROTHER, "⚠️ Просрочки")  # тестовые видит только владелец
    assert "Просроченных задач нет" in env.last_text()
    await run(env, env.aq.now())
    assert "Тестовая просроченная" in texts_to(env, BROTHER)


async def test_snooze_and_cant(aq_env):
    env = aq_env
    now = env.aq.now()
    date, _ = await env.aq.save_task("feed", now)
    for _ in range(3):
        await env.click(BROTHER, f"aq:snooze:{date}:feed")
    assert "Можно отложить ещё: 0" in env.session.of_type(EditMessageText)[-1].text
    await env.click(BROTHER, f"aq:snooze:{date}:feed")
    assert (await env.aq.get_task(date, "feed"))["snooze_count"] == 3

    await env.click(BROTHER, f"aq:why:{date}:feed:nothing")
    assert "Я передал Владу" in env.session.of_type(EditMessageText)[-1].text
    assert "не может выполнить" in texts_to(env, ADMIN) and "Нечем" in texts_to(env, ADMIN)

    date, _ = await env.aq.save_task("air_on", now)
    await env.click(BROTHER, f"aq:why:{date}:air_on:other")
    await env.send(BROTHER, "компрессор сломался")
    assert (await env.aq.get_task(date, "air_on"))["cant_reason"] == "компрессор сломался"
    assert "компрессор сломался" in texts_to(env, ADMIN)
    env.llm.calls.clear()
    await env.send(BROTHER, "привет")  # дальше обычный текст снова идёт в чат
    assert env.llm.calls


async def test_pause_skips_and_resume_does_not_backfill(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqpause")
    assert "на паузе" in texts_to(env, BROTHER)
    await run(env, at(MONDAY, 7, 0))
    assert "Покормить" not in texts_to(env, BROTHER)
    await env.aq.resume(at(MONDAY, 8, 0))
    await run(env, at(MONDAY, 8, 1))
    assert "Покормить" not in texts_to(env, BROTHER)  # задачи времён паузы не досылаем
    await run(env, at(MONDAY, 14, 30))
    assert "Включить свет" in texts_to(env, BROTHER)

    await env.send(BROTHER, "/aqpause")  # команды владельца брату недоступны
    assert not await env.aq.is_paused()


async def test_daily_report_streaks_and_weekly_advice(aq_env):
    env = aq_env
    env.llm.chat_reply = "1. Подменяй 20% воды."
    sunday = MONDAY + datetime.timedelta(days=6)
    for offset in range(3):
        day = sunday - datetime.timedelta(days=2 - offset)
        tasks = await env.aq.tasks_for(day) if day == sunday else [(None, "feed")]
        for _, task_id in tasks:
            date, _ = await env.aq.save_task(task_id, at(day, 7))
            await env.aq.complete(date, task_id, BROTHER, "Брат")
    await run(env, at(sunday, 23, 15))
    report = texts_to(env, ADMIN)
    assert "Отчёт по аквариуму" in report and "Серия без пропусков: 3" in report
    assert "3 дня без пропусков" in texts_to(env, BROTHER)
    await run(env, at(sunday, 23, 20))
    weekly = texts_to(env, BROTHER)
    assert "Статистика за 7 дней" in weekly and "Советы на неделю" in weekly and "20%" in weekly
    advice_prompt = env.llm.chat_calls[-1]["messages"][-1]["content"]
    assert "выполнено 9 из 9" in advice_prompt
    await run(env, at(sunday, 23, 25))
    assert texts_to(env, BROTHER).count("Статистика за 7 дней") == 1


# ---------------------------------------------------------------- доступ и аквариумист


async def test_brother_gets_aquarium_bot(aq_env):
    env = aq_env
    await env.send(BROTHER, "/start")
    first = sent_to(env, BROTHER)[-1]
    assert "аквариум" in first.text and "norminette" not in first.text
    assert first.reply_markup.keyboard[0][0].text == "📋 Сегодня"
    assert (await env.db.get_user(BROTHER)).mode == "aquarium"

    await env.send(FRIEND, "/aq")
    assert "Не знаю такую команду" in env.last_text()

    await env.brain.add_fact("fish", "6 гуппи и 2 сомика")
    await env.send(BROTHER, "Рыбки плавают у поверхности, что делать?")
    system = env.system_prompt()
    assert "аквариумист" in system and "6 гуппи и 2 сомика" in system


async def test_learns_from_conversation(aq_env):
    env = aq_env
    env.llm.scripted.append(("Сообщение владельца", json.dumps(
        {"facts": [{"topic": "fish", "text": "Появились 3 неона"}], "outdated": []}
    )))
    await env.send(BROTHER, "Мы купили 3 неона!")
    facts = await env.brain.facts("fish")
    assert [f.text for f in facts] == ["Появились 3 неона"]

    env.llm.scripted.append(("Сообщение владельца", json.dumps(
        {"facts": [{"topic": "fish", "text": "Неонов осталось 2"}], "outdated": [facts[0].id]}
    )))
    await env.send(BROTHER, "один неон погиб")
    assert [f.text for f in await env.brain.facts("fish")] == ["Неонов осталось 2"]

    await env.send(ADMIN, "/mode")  # у владельца режим аквариумиста тоже есть, у друга — нет
    owner_buttons = str(env.session.of_type(SendMessage)[-1].reply_markup)
    await env.send(FRIEND, "/mode")
    assert "Аквариумист" in owner_buttons
    assert "Аквариумист" not in str(env.session.of_type(SendMessage)[-1].reply_markup)


async def test_question_of_the_day(aq_env):
    env = aq_env
    now = at(MONDAY, 18, 0)
    assert await env.brain.choose_topic(now) == "tank"  # сначала самое базовое
    await run(env, now)
    question = sent_to(env, ADMIN)[-1]
    assert "Вопрос про аквариум" in question.text and "объём" in question.text
    await run(env, now + datetime.timedelta(minutes=5))
    assert texts_to(env, ADMIN).count("Вопрос про аквариум") == 1  # раз в день
    assert await env.brain.choose_topic(now) == "fish"  # «tank» только что спрашивали

    env.llm.scripted.append(("Вопрос агента", json.dumps(
        {"facts": [{"topic": "tank", "text": "Объём 60 литров"}], "outdated": []}
    )))
    (msg_id,) = await env.db._fetchone("SELECT tg_msg_id FROM aq_questions")
    await env.send(ADMIN, "просто про C")  # без ответа на вопрос — обычный чат
    assert not await env.brain.facts("tank")
    asked = env.message(ADMIN, text=question.text).model_copy(update={"message_id": msg_id})
    await env.send(ADMIN, "60 литров, запущен год назад", reply_to_message=asked)
    assert "Объём 60 литров" in "\n".join(env.texts())
    assert [f.text for f in await env.brain.facts("tank")] == ["Объём 60 литров"]
    assert "60 литров, запущен год назад" in env.last_user_prompt()
    assert await env.brain.question_by_msg(ADMIN, msg_id) is None


async def test_question_buttons(aq_env):
    env = aq_env
    await env.send(ADMIN, "/aqask")
    q = (await env.db._fetchone("SELECT id FROM aq_questions"))[0]
    await env.click(ADMIN, f"aqq:answer:{q}")
    env.llm.scripted.append(("Вопрос агента", json.dumps(
        {"facts": [{"topic": "tank", "text": "Объём 100 литров"}], "outdated": []}
    )))
    await env.send(ADMIN, "100 литров")
    assert [f.text for f in await env.brain.facts("tank")] == ["Объём 100 литров"]
    await env.click(ADMIN, f"aqq:answer:{q}")
    assert "уже закрыт" in str(env.session.requests[-1])

    await env.send(ADMIN, "/aqask")
    q2 = (await env.db._fetchone("SELECT id FROM aq_questions ORDER BY id DESC"))[0]
    await env.click(ADMIN, f"aqq:skip:{q2}")
    assert (await env.brain.get_question(q2))[4] == -1
    await env.click(BROTHER, f"aqq:answer:{q}")  # чужой вопрос
    assert "уже закрыт" in str(env.session.requests[-1])


async def test_tank_command(aq_env):
    env = aq_env
    await env.send(BROTHER, "/tank add фильтр внутренний, 300 л/ч")  # модель ничего не вернула
    assert "Запомнил" in env.last_text()
    await env.send(BROTHER, "🐠 Что я знаю")
    assert "фильтр внутренний" in env.last_text()
    fid = (await env.brain.facts())[0].id
    await env.send(BROTHER, f"/tank del {fid}")
    assert "только владелец" in env.last_text()
    await env.send(ADMIN, f"/tank del {fid}")
    assert not await env.brain.facts()


# ---------------------------------------------------------------- вода


def test_parse_water():
    assert parse_water("pH 7,2 NO2 0 no3=25 KH:6 T 25.5") == {"ph": 7.2, "no2": 0, "no3": 25, "kh": 6, "t": 25.5}
    assert parse_water("NO₂ 0.5") == {"no2": 0.5}
    assert parse_water("просто текст") == {}
    warnings = water_warnings({"no2": 0.5, "ph": 7.0, "t": 20})
    assert len(warnings) == 2 and "NO₂" in warnings[0] and "ниже" in warnings[1]


async def test_water_command_alerts_owner(aq_env):
    env = aq_env
    await env.send(BROTHER, "/water pH 7 NO2 0.5")
    assert "выше нормы" in env.last_text()
    assert "Тест воды" in texts_to(env, ADMIN)
    await env.send(BROTHER, "/water pH 7.2 NO2 0")
    assert "Всё в норме" in env.last_text()
    await env.send(BROTHER, "/water")
    assert "0 ← 0.5" in env.last_text()


# ---------------------------------------------------------------- перенос со старого бота


def make_legacy_db(path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE tasks (id INTEGER PRIMARY KEY, date TEXT, task_id TEXT, task_name TEXT, sent_at TEXT,
            completed_at TEXT, completed_by INTEGER, completed_by_name TEXT, overdue_notified INTEGER,
            reminded INTEGER, remind_at TEXT, overdue_at TEXT, snooze_count INTEGER, cant_at TEXT,
            cant_reason TEXT, cant_by_name TEXT, UNIQUE(date, task_id));
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE schedule_overrides (task_id TEXT PRIMARY KEY, time TEXT);
        CREATE TABLE achievements (streak INTEGER PRIMARY KEY, achieved_at TEXT);
        INSERT INTO tasks (date, task_id, task_name, sent_at, completed_at, completed_by_name, snooze_count)
            VALUES ('2026-09-30', 'feed', '🐟 Покормить рыбок', '2026-09-30T07:00:00+02:00',
                    '2026-09-30T07:10:00+02:00', 'Брат', 0),
                   ('2026-09-30', 'air_on', '💨 Включить воздух', '2026-09-30T07:00:00+02:00',
                    NULL, NULL, NULL);
        INSERT INTO schedule_overrides VALUES ('light_on', '15:00');
        INSERT INTO achievements VALUES (3, '2026-09-20T23:15:00+02:00');
    """)
    conn.commit()
    conn.close()


async def test_import_legacy_database(aq_env, tmp_path):
    env = aq_env
    make_legacy_db(tmp_path / "aquarium.db")
    data = (tmp_path / "aquarium.db").read_bytes()
    await env.send_file(ADMIN, "aquarium.db", data, caption="/aqimport")
    assert "Перенесено задач: 2 из 2" in env.last_text()
    row = await env.aq.get_task("2026-09-30", "feed")
    assert row["completed_by_name"] == "Брат" and row["reminded"] == 1
    assert {i.task_id: i.at.strftime("%H:%M") for i in await env.aq.schedule()}["light_on"] == "15:00"
    assert await env.aq.achieved() == [3]
    await env.send_file(ADMIN, "aquarium.db", data, caption="/aqimport")
    assert "Перенесено задач: 0 из 2" in env.last_text()

    await env.send_file(BROTHER, "aquarium.db", data, caption="/aqimport")  # не владелец
    assert "Перенесено" not in env.last_text()
    await env.send_file(ADMIN, "aquarium.db", b"not sqlite", caption="/aqimport")
    assert "не база SQLite" in env.last_text()

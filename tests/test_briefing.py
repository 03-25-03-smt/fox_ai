"""Утренняя сводка."""

import datetime

import respx

from bot.briefing import City, Weather, WeatherClient
from bot.handlers.briefing import send_briefings

from .conftest import ADMIN

PARIS = datetime.timezone(datetime.timedelta(hours=2))


def test_weather_text_and_tips():
    w = Weather(now_temp=26, code=0, t_min=18, t_max=31, rain_chance=0, rain_mm=0, wind=5)
    assert "ясно" in w.text("Прага") and "жарко" in w.text("Прага")
    rainy = Weather(now_temp=None, code=63, t_min=5, t_max=9, rain_chance=70, rain_mm=3, wind=45)
    text = rainy.text("Париж")
    assert "зонт" in text and "ветер" in text and "сейчас" not in text


async def test_open_meteo_client():
    with respx.mock:
        respx.get("https://geocoding-api.open-meteo.com/v1/search").respond(json={"results": [
            {"name": "Прага", "country": "Чехия", "latitude": 50.08, "longitude": 14.42}]})
        respx.get("https://api.open-meteo.com/v1/forecast").respond(json={
            "current": {"temperature_2m": 18.1, "weather_code": 3, "wind_speed_10m": 2.9},
            "daily": {"weather_code": [3], "temperature_2m_max": [25.1], "temperature_2m_min": [11.5],
                      "precipitation_probability_max": [0], "precipitation_sum": [0.0]}})
        client = WeatherClient()
        city = await client.geocode("Praha")
        assert city == City("Прага, Чехия", 50.08, 14.42)
        w = await client.forecast(city)
        assert (w.t_max, w.code, w.now_temp) == (25.1, 3, 18.1)
        await client.close()


async def test_morning_command_and_contents(env):
    env.llm.chat_reply = "• Вышла новая модель — example.com"
    await env.send(ADMIN, "/morning city Прага")
    assert "Прага, Чехия" in env.last_text()
    await env.send(ADMIN, "/morning news ИИ, Чехия")
    await env.send(ADMIN, "/remind через 1 минуту позвонить маме")
    await env.send(ADMIN, "/morning now")
    text = env.last_text()
    assert "дождь" in text and "зонт" in text
    assert "позвонить маме" in text
    assert "Новости" in text and "новая модель" in text
    assert env.web.queries[-2:] == ["ИИ", "Чехия"]


async def test_daily_send_once_in_window(env):
    await env.send(ADMIN, "/morning on 7:30")
    assert "07:30" in env.last_text()
    paris = env.assistant.tz(None)
    day = datetime.date(2026, 10, 5)
    early = datetime.datetime.combine(day, datetime.time(7, 0), tzinfo=paris)
    assert await send_briefings(env.bot, env.app, early) == 0
    on_time = early.replace(minute=31)
    assert await send_briefings(env.bot, env.app, on_time) == 1
    assert "Доброе утро" in env.last_text() and "Город для погоды не задан" in env.last_text()
    assert await send_briefings(env.bot, env.app, on_time) == 0
    late = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(12, 0), tzinfo=paris)
    assert await send_briefings(env.bot, env.app, late) == 0  # бот был выключен утром — днём уже не шлём
    await env.send(ADMIN, "/morning off")
    assert await send_briefings(env.bot, env.app, on_time + datetime.timedelta(days=2)) == 0

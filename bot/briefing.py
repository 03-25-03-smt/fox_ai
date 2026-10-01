"""Утренняя сводка: погода (Open-Meteo, без ключа), напоминания на сегодня,
задачи по аквариумам, слова на повторение, blackhole из интры и новости по темам."""

import html
import logging
from dataclasses import dataclass

import httpx

from .db import Database

log = logging.getLogger(__name__)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Коды погоды WMO -> (иконка, описание)
WMO = {
    0: ("☀️", "ясно"), 1: ("🌤", "почти ясно"), 2: ("⛅", "переменная облачность"), 3: ("☁️", "пасмурно"),
    45: ("🌫", "туман"), 48: ("🌫", "изморозь"),
    51: ("🌦", "лёгкая морось"), 53: ("🌦", "морось"), 55: ("🌧", "сильная морось"),
    56: ("🌧", "ледяная морось"), 57: ("🌧", "ледяная морось"),
    61: ("🌦", "небольшой дождь"), 63: ("🌧", "дождь"), 65: ("🌧", "ливень"),
    66: ("🌧", "ледяной дождь"), 67: ("🌧", "ледяной дождь"),
    71: ("🌨", "небольшой снег"), 73: ("🌨", "снег"), 75: ("❄️", "сильный снег"), 77: ("🌨", "снежная крупа"),
    80: ("🌦", "кратковременный дождь"), 81: ("🌧", "ливни"), 82: ("⛈", "сильные ливни"),
    85: ("🌨", "снегопад"), 86: ("❄️", "сильный снегопад"),
    95: ("⛈", "гроза"), 96: ("⛈", "гроза с градом"), 99: ("⛈", "сильная гроза с градом"),
}


class WeatherError(Exception):
    pass


@dataclass(frozen=True)
class City:
    name: str
    lat: float
    lon: float


@dataclass(frozen=True)
class BriefingSettings:
    enabled: bool = False
    time: str = "07:30"
    city: City | None = None
    topics: str = ""
    last_sent: str | None = None


@dataclass(frozen=True)
class Weather:
    now_temp: float | None
    code: int
    t_min: float
    t_max: float
    rain_chance: int | None
    rain_mm: float
    wind: float | None

    def text(self, city: str) -> str:
        icon, desc = WMO.get(self.code, ("🌡", "погода"))
        line = f"{icon} <b>{html.escape(city)}</b>: {desc}, {self.t_min:.0f}…{self.t_max:.0f}°C"
        if self.now_temp is not None:
            line += f" (сейчас {self.now_temp:.0f}°)"
        if self.rain_chance:
            line += f", дождь {self.rain_chance}%"
        tips = []
        if (self.rain_chance or 0) >= 50 or self.rain_mm >= 1:
            tips.append("☂️ возьми зонт")
        if self.t_max <= 5:
            tips.append("🧣 одевайся теплее")
        elif self.t_max >= 28:
            tips.append("💧 жарко — возьми воду")
        if self.wind and self.wind >= 40:
            tips.append("💨 сильный ветер")
        return line + (" — " + ", ".join(tips) if tips else "")


class WeatherClient:
    def __init__(self, timeout: float = 15.0) -> None:
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=10.0))

    async def close(self) -> None:
        await self._client.aclose()

    async def _get(self, url: str, params: dict) -> dict:
        try:
            resp = await self._client.get(url, params=params)
            resp.raise_for_status()
            return resp.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise WeatherError(f"Open-Meteo недоступен: {exc}") from exc

    async def geocode(self, name: str) -> City | None:
        data = await self._get(GEOCODE_URL, {"name": name, "count": 1, "language": "ru", "format": "json"})
        results = data.get("results") or []
        if not results:
            return None
        r = results[0]
        label = r.get("name") or name
        if r.get("country"):
            label += f", {r['country']}"
        return City(label, float(r["latitude"]), float(r["longitude"]))

    async def forecast(self, city: City) -> Weather:
        data = await self._get(FORECAST_URL, {
            "latitude": city.lat, "longitude": city.lon, "timezone": "auto", "forecast_days": 1,
            "current": "temperature_2m,weather_code,wind_speed_10m",
            "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
                     "precipitation_sum",
        })
        daily, current = data.get("daily") or {}, data.get("current") or {}
        try:
            return Weather(
                now_temp=current.get("temperature_2m"),
                code=int(daily["weather_code"][0]),
                t_min=float(daily["temperature_2m_min"][0]),
                t_max=float(daily["temperature_2m_max"][0]),
                rain_chance=(daily.get("precipitation_probability_max") or [None])[0],
                rain_mm=float((daily.get("precipitation_sum") or [0])[0] or 0),
                wind=current.get("wind_speed_10m"),
            )
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise WeatherError("Open-Meteo вернул непонятный ответ") from exc


class BriefingStore:
    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, user_id: int) -> BriefingSettings:
        row = await self.db._fetchone(
            "SELECT enabled, time, city, lat, lon, topics, last_sent FROM briefing WHERE user_id = ?", (user_id,)
        )
        if row is None:
            return BriefingSettings()
        enabled, time, city, lat, lon, topics, last = row
        place = City(city, lat, lon) if city and lat is not None else None
        return BriefingSettings(bool(enabled), time, place, topics or "", last)

    async def update(self, user_id: int, **fields) -> None:
        await self.db._exec("INSERT OR IGNORE INTO briefing (user_id) VALUES (?)", (user_id,))
        if "city" in fields:
            city: City | None = fields.pop("city")
            fields.update(city=city.name if city else None, lat=city.lat if city else None,
                          lon=city.lon if city else None)
        sets = ", ".join(f"{k} = ?" for k in fields)  # имена полей — только из кода
        await self.db._exec(f"UPDATE briefing SET {sets} WHERE user_id = ?", (*fields.values(), user_id))

    async def enabled_users(self) -> list[int]:
        return [r[0] for r in await self.db._fetchall("SELECT user_id FROM briefing WHERE enabled = 1")]


NEWS_PROMPT = (
    "Вот свежие заголовки по темам «{topics}». Выбери 3–5 самых важных и интересных и перескажи "
    "каждую одной строкой по-русски: «• суть — источник». Не выдумывай ничего сверх заголовков. "
    "Если новостей нет — ответь «—».\n\n{items}"
)

"""/morning — утренняя сводка: погода, напоминания, аквариумы, слова, blackhole, новости."""

import datetime
import html
import logging
import re

from aiogram import Bot
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from ..app import App
from ..assistant import Turn
from ..briefing import NEWS_PROMPT, BriefingStore, WeatherError
from ..db import User
from ..intra import IntraError
from ..lang import LANGS
from ..llm import LLMError
from ..web import WebError
from .common import need_registered
from .registry import Routes

log = logging.getLogger(__name__)
router = Routes("briefing")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3])[:.]([0-5]\d)$")
DEFAULT_TOPICS = "технологии, искусственный интеллект"


def _store(app: App) -> BriefingStore:
    return app.assistant.briefing


async def _news(app: App, topics: str) -> str:
    web = app.assistant.web
    if web is None or not topics.strip():
        return ""
    items = []
    for topic in [t.strip() for t in topics.split(",") if t.strip()][:4]:
        try:
            for r in await web.search(topic, limit=5, news=True):
                items.append(f"[{topic}] {r.title} — {r.snippet[:200]} ({r.url})")
        except WebError as exc:
            log.warning("briefing news %s failed: %s", topic, exc)
    if not items:
        return ""
    try:
        async with app.queue.slot():
            text = await app.llm.chat(app.settings.default_model, [
                {"role": "user", "content": NEWS_PROMPT.format(topics=topics, items="\n".join(items[:20]))},
            ], options={"temperature": 0.2})
    except LLMError as exc:
        log.warning("briefing news summary failed: %s", exc)
        return ""
    text = text.strip()
    return "" if text in ("", "—", "-") else text


async def compose(app: App, user: User, now: datetime.datetime | None = None) -> str:
    """Текст сводки (HTML)."""
    tz = app.assistant.tz(user)
    now = (now or datetime.datetime.now(tz)).astimezone(tz)
    s = await _store(app).get(user.id)
    weekday = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"][now.weekday()]
    hello = "Доброе утро" if now.hour < 12 else "Привет"
    lines = [f"🌅 <b>{hello}!</b> Сегодня {weekday}, {now:%d.%m}.", ""]

    if s.city:
        try:
            lines.append((await app.weather.forecast(s.city)).text(s.city.name))
        except WeatherError as exc:
            lines.append(f"🌡 Погода: {html.escape(str(exc))}")
    else:
        lines.append("🌡 Город для погоды не задан: /morning city Прага")

    end = now.replace(hour=23, minute=59, second=59)
    today = [r for r in await app.db.list_reminders(user.id) if r.due_at.astimezone(tz) <= end]
    if today:
        lines += ["", "⏰ <b>Напоминания</b>"]
        lines += [f"• {r.due_at.astimezone(tz):%H:%M} {html.escape(r.text)}" for r in today]

    if user.id in app.settings.aquarium_members:
        aq = app.assistant.aquarium
        tasks = await aq.tasks_for(now.astimezone(aq.tz).date())
        if tasks and not await aq.is_paused():
            lines += ["", "🐠 <b>Аквариумы</b>"]
            lines += [f"• {i.at:%H:%M} {html.escape(await aq.task_name(i))}" for i in tasks]

    if user.id in app.settings.lang_users:
        counts = await app.assistant.lang.counts(user.id, now.date().isoformat())
        due = [f"{LANGS[c].flag} {d}" for c, (_, d) in counts.items() if d]
        if due:
            lines += ["", f"📚 Слов на повторение: {' · '.join(due)} — /quiz"]

    if app.intra and user.intra_login:
        try:
            profile = await app.intra.get_profile(user.intra_login, max_age=6 * 3600)
            days = profile.blackhole_days()
            if days is not None:
                mark = "🔴" if days <= 7 else "🟡" if days <= 30 else "🟢"
                lines += ["", f"🕳 {mark} До blackhole: {days} дн."]
        except IntraError as exc:
            log.warning("briefing intra failed: %s", exc)

    news = await _news(app, s.topics)
    if news:
        lines += ["", f"📰 <b>Новости</b> ({html.escape(s.topics)})", html.escape(news)]
    return "\n".join(lines)


async def send_briefings(bot: Bot, app: App, now: datetime.datetime | None = None) -> int:
    """Фоновая рассылка: в заданное время, раз в день (если бот был выключен — до 11:00)."""
    sent = 0
    for user_id in await _store(app).enabled_users():
        user = await app.db.get_user(user_id)
        if user is None:
            continue
        tz = app.assistant.tz(user)
        local = (now or datetime.datetime.now(tz)).astimezone(tz)
        s = await _store(app).get(user_id)
        hour, minute = map(int, s.time.split(":"))
        due = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
        day = local.date().isoformat()
        if s.last_sent == day or local < due or local > due + datetime.timedelta(hours=3):
            continue
        await _store(app).update(user_id, last_sent=day)
        try:
            await bot.send_message(user_id, await compose(app, user, local), parse_mode=ParseMode.HTML,
                                   disable_web_page_preview=True)
            sent += 1
        except TelegramAPIError as exc:
            log.warning("briefing for %s not delivered: %s", user_id, exc)
    return sent


@router.message(Command("morning", "brief"))
async def cmd_morning(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    if not await need_registered(message, turn):
        return
    store = _store(app)
    action, _, rest = (command.args or "").strip().partition(" ")
    action, rest = action.lower(), rest.strip()

    if action == "on":
        when = rest or (await store.get(turn.user_id)).time
        m = _TIME_RE.match(when)
        if not m:
            await message.answer("Использование: /morning on 7:30")
            return
        await store.update(turn.user_id, enabled=1, time=f"{int(m[1]):02d}:{m[2]}")
        s = await store.get(turn.user_id)
        if not s.topics:
            await store.update(turn.user_id, topics=DEFAULT_TOPICS)
        hint = "" if s.city else "\nЗадай город для погоды: /morning city Прага"
        await message.answer(f"🌅 Сводка каждый день в {int(m[1]):02d}:{m[2]}.{hint}")
    elif action == "off":
        await store.update(turn.user_id, enabled=0)
        await message.answer("🌅 Утренняя сводка выключена.")
    elif action == "city" and rest:
        try:
            city = await app.weather.geocode(rest)
        except WeatherError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        if city is None:
            await message.answer("Не нашёл такой город 🤔")
            return
        await store.update(turn.user_id, city=city)
        await message.answer(f"📍 Погода для: {city.name}")
    elif action in ("news", "topics"):
        await store.update(turn.user_id, topics=rest[:200])
        await message.answer(f"📰 Темы новостей: {rest}" if rest else "📰 Новости в сводке выключены.")
    elif action in ("", "now"):
        s = await store.get(turn.user_id)
        status = await message.answer("🌅 Собираю сводку…")
        text = await compose(app, turn.user)
        await status.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
        if not action:
            state = f"каждый день в {s.time}" if s.enabled else "выключена"
            await message.answer(
                f"⚙️ Сводка {state}. Темы новостей: {s.topics or '—'}\n"
                "/morning on 7:30 · /morning off · /morning city Прага · /morning news 42, ИИ, Чехия"
            )
    else:
        await message.answer("/morning — сводка сейчас · on 7:30 · off · city Прага · news темы через запятую")

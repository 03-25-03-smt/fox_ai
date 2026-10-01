"""Фоновые задачи: напоминания, алерт температуры GPU, blackhole из интры, бэкапы,
ежедневное повторение слов."""

import asyncio
import datetime
import html
import logging
import time

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .app import App
from .aquarium_bot import tick as aquarium_tick
from .backup import backup_database
from .gpu import query_gpus
from .intra import IntraError
from .lang import LANGS

log = logging.getLogger(__name__)

REMINDER_INTERVAL = 15
GPU_INTERVAL = 60
GPU_ALERT_COOLDOWN = 30 * 60
SCHEDULE_INTERVAL = 300
BLACKHOLE_WARN_DAYS = (30, 14, 7, 3, 2, 1, 0)
LANG_INTERVAL = 60
AQUARIUM_INTERVAL = 30
LANG_LATEST_HOUR = 21  # если бот был выключен в назначенное время — позже 21:00 не будим


async def _loop(name: str, interval: float, step) -> None:
    while True:
        try:
            await step()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("background task %s failed", name)
        await asyncio.sleep(interval)


async def notify_admins(bot: Bot, app: App, text: str) -> None:
    for admin_id in app.settings.admins:
        try:
            await bot.send_message(admin_id, text, parse_mode="HTML")
        except TelegramAPIError as exc:
            log.warning("cannot notify admin %s: %s", admin_id, exc)


# ---------------------------------------------------------------- напоминания


async def deliver_reminders(bot: Bot, app: App, now: datetime.datetime | None = None) -> int:
    now = now or datetime.datetime.now(datetime.UTC)
    sent = 0
    for reminder in await app.db.due_reminders(now):
        try:
            await bot.send_message(reminder.chat_id, f"⏰ Напоминание: {reminder.text}")
            sent += 1
        except TelegramAPIError as exc:
            log.warning("reminder %s not delivered: %s", reminder.id, exc)
        await app.db.complete_reminder(reminder.id)  # не спамим повторно даже при ошибке
    return sent


# ---------------------------------------------------------------- GPU


async def check_gpu_temperature(bot: Bot, app: App) -> bool:
    gpus = await query_gpus(app.settings.gpu_stats_file)
    if not gpus:
        return False
    limit = app.settings.gpu_temp_alert
    hot = [g for g in gpus if g.temperature is not None and g.temperature >= limit]
    if not hot or time.monotonic() - app.last_gpu_alert < GPU_ALERT_COOLDOWN:
        return False
    app.last_gpu_alert = time.monotonic()
    lines = [f"🔥 <b>GPU перегрев</b> (порог {limit}°C)"]
    lines += [f"[{g.index}] {html.escape(g.name)}: {g.temperature}°C, загрузка {g.utilization}%" for g in hot]
    lines.append("Проверь охлаждение (у P100 нет своего вентилятора!).")
    await notify_admins(bot, app, "\n".join(lines))
    return True


# ---------------------------------------------------------------- blackhole


async def check_blackholes(bot: Bot, app: App, now: datetime.datetime | None = None) -> int:
    if app.intra is None:
        return 0
    now = now or datetime.datetime.now(datetime.UTC)
    today = now.date().isoformat()
    sent = 0
    for user in await app.db.list_users():
        if not user.intra_login or user.intra_notified == today:
            continue
        try:
            profile = await app.intra.get_profile(user.intra_login, max_age=3600)
        except IntraError as exc:
            log.warning("intra check for %s failed: %s", user.intra_login, exc)
            continue
        days = profile.blackhole_days(now)
        if days is None or days not in BLACKHOLE_WARN_DAYS:
            continue
        in_progress = ", ".join(p.name for p in profile.in_progress) or "нет"
        text = (f"🕳 До blackhole осталось {days} дн. ({profile.blackhole_at:%d.%m.%Y}).\n"
                f"В процессе: {in_progress}.\nНе забудь сдать проект! /42")
        try:
            await bot.send_message(user.id, text)
            sent += 1
        except TelegramAPIError as exc:
            log.warning("blackhole notify %s failed: %s", user.id, exc)
        await app.db.set_user_field(user.id, "intra_notified", today)
        await asyncio.sleep(0.6)  # лимит API интры — 2 запроса в секунду
    return sent


# ---------------------------------------------------------------- повторение слов


async def send_daily_reviews(bot: Bot, app: App, now: datetime.datetime | None = None) -> int:
    """Раз в день в случайное время окна LANG_DAILY_FROM..TO присылает 5–15 слов на повторение."""
    s, store = app.settings, app.assistant.lang
    now = now or datetime.datetime.now(datetime.UTC)
    sent = 0
    for user_id in sorted(s.lang_users):
        user = await app.db.get_user(user_id)
        if user is None:
            continue
        state = await store.get_state(user_id)
        if not state.daily:
            continue
        tz = app.assistant.tz(user)
        local = now.astimezone(tz)
        day = local.date().isoformat()
        if state.plan_day != day:
            at = store.plan_time(local.date(), tz, s.lang_daily_from, s.lang_daily_to)
            count = store.rng.randint(s.lang_daily_min, max(s.lang_daily_min, s.lang_daily_max))
            await store.save_plan(user_id, day, at, count)
            state = await store.get_state(user_id)
        if state.plan_sent or state.plan_at is None or now < state.plan_at:
            continue
        words = await store.review_words(user_id, day, state.plan_count)
        await store.mark_plan_sent(user_id, [w.id for w in words])
        if local.hour >= LANG_LATEST_HOUR:
            continue
        if words:
            per_lang = {code: sum(w.lang == code for w in words) for code in LANGS}
            split = " · ".join(f"{LANGS[c].flag} {n}" for c, n in per_lang.items() if n)
            text = f"📚 Время повторить слова! Сегодня {len(words)}: {split}"
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="▶️ Начать", callback_data="lq:daily")]])
        else:
            text, markup = "📚 Пора учить слова, но словарь пока пуст. Добавь первое: /w Hund", None
        if state.streak:
            text += f"\n🔥 Серия: {state.streak} дн. — не прерывай!"
        try:
            await bot.send_message(user_id, text, reply_markup=markup)
            sent += 1
        except TelegramAPIError as exc:
            log.warning("daily review for %s not delivered: %s", user_id, exc)
    return sent


# ---------------------------------------------------------------- запуск


def start_background(bot: Bot, app: App) -> None:
    s = app.settings
    state = {"blackhole": "", "backup": ""}

    async def reminders_step() -> None:
        await deliver_reminders(bot, app)

    async def gpu_step() -> None:
        await check_gpu_temperature(bot, app)

    async def schedule_step() -> None:
        local = datetime.datetime.now(app.assistant.tz(None))
        day = local.date().isoformat()
        if app.intra and local.hour == s.intra_check_hour and state["blackhole"] != day:
            state["blackhole"] = day
            await check_blackholes(bot, app)
        if s.backup_dir and local.hour == s.backup_hour and state["backup"] != day:
            state["backup"] = day
            path = await backup_database(app.db.path, s.backup_dir, s.backup_keep)
            log.info("backup created: %s", path)

    app.spawn(_loop("reminders", REMINDER_INTERVAL, reminders_step))
    app.spawn(_loop("gpu", GPU_INTERVAL, gpu_step))
    async def lang_step() -> None:
        await send_daily_reviews(bot, app)

    app.spawn(_loop("schedule", SCHEDULE_INTERVAL, schedule_step))
    app.spawn(_loop("lang", LANG_INTERVAL, lang_step))

    if s.aquarium_enabled:
        async def aquarium_step() -> None:
            await aquarium_tick(bot, app)

        app.spawn(_loop("aquarium", AQUARIUM_INTERVAL, aquarium_step))

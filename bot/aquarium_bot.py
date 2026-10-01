"""Аквариум в Telegram: клавиатуры, рассылка задач, фоновый цикл (расписание,
напоминания, просрочки, отчёт, недельная статистика с советами, вопрос дня)."""

import datetime
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .app import App
from .aquarium import (
    ACHIEVEMENTS,
    ALL_TASKS,
    CANT_REASONS,
    LATE_NOTE_AFTER,
    MAX_SNOOZES,
    MISSED_WINDOW,
    REPORT_TIME,
    SNOOZE_FOR,
    SUN,
    TASKS,
    WEEKLY_TIME,
    Aquarium,
    format_task_row,
    hhmm,
    overdue_at_of,
    remind_at_of,
)
from .aquarium_brain import TOPICS

log = logging.getLogger(__name__)

CB = "aq:"  # aq:<действие>:<дата>:<задача>[:<причина>]
CB_Q = "aqq:"  # aqq:<действие>:<id вопроса>

MENU_TODAY = "📋 Сегодня"
MENU_TOMORROW = "🗓 Завтра"
MENU_OVERDUE = "⚠️ Просрочки"
MENU_HISTORY = "📚 История"
MENU_STATS = "📊 Статистика"
MENU_TANK = "🐠 Что я знаю"
MENU_HELP = "❓ Помощь"
MENU_HIDE = "⌨️ Скрыть меню"
MENU_ITEMS = (MENU_TODAY, MENU_TOMORROW, MENU_OVERDUE, MENU_HISTORY, MENU_STATS, MENU_TANK, MENU_HELP,
              MENU_HIDE)


def main_menu(owner: bool) -> ReplyKeyboardMarkup:
    rows = [
        [MENU_TODAY, MENU_TOMORROW],
        [MENU_OVERDUE, MENU_HISTORY],
        [MENU_STATS, MENU_TANK],
        [MENU_HELP, MENU_HIDE] if owner else [MENU_HELP],
    ]
    return ReplyKeyboardMarkup(
        keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
        resize_keyboard=True, is_persistent=not owner,
    )


def task_keyboard(date: str, task_id: str, snooze_count: int = 0) -> InlineKeyboardMarkup:
    # Дата в кнопке: нажатие после полуночи отметит задачу за правильный день
    second = []
    if snooze_count < MAX_SNOOZES:
        minutes = int(SNOOZE_FOR.total_seconds() // 60)
        second.append(InlineKeyboardButton(text=f"⏰ Отложить на {minutes} мин",
                                           callback_data=f"{CB}snooze:{date}:{task_id}"))
    second.append(InlineKeyboardButton(text="❌ Не могу", callback_data=f"{CB}cant:{date}:{task_id}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Выполнено", callback_data=f"{CB}done:{date}:{task_id}")],
        second,
    ])


def reasons_keyboard(date: str, task_id: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=text, callback_data=f"{CB}why:{date}:{task_id}:{code}")]
            for code, text in CANT_REASONS.items()]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"{CB}back:{date}:{task_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_keyboard(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"{CB_Q}answer:{question_id}"),
        InlineKeyboardButton(text="🤷 Не знаю", callback_data=f"{CB_Q}skip:{question_id}"),
    ]])


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Не роняет фоновый цикл, если человек не нажал /start или заблокировал бота."""
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
        return True
    except TelegramAPIError as exc:
        log.warning("aquarium: cannot send to %s: %s", chat_id, exc)
        return False


def _aq(app: App) -> Aquarium:
    return app.assistant.aquarium


async def send_task(bot: Bot, app: App, task_id: str, note: str | None = None,
                    now: datetime.datetime | None = None) -> list[int]:
    """Рассылает задачу ухаживающим. Настоящую задачу — раз в день. Возвращает, кому дошло."""
    date, created = await _aq(app).save_task(task_id, now)
    if not created:
        return []
    text = f"🐠 <b>Уход за аквариумом</b>\n\n{html.escape(ALL_TASKS[task_id])}\n\n"
    if note:
        text += f"{note}\n\n"
    text += "Когда выполнишь — нажми кнопку ниже."
    return [uid for uid in sorted(app.settings.aquarium_caretakers)
            if await safe_send(bot, uid, text, reply_markup=task_keyboard(date, task_id))]


# ---------------------------------------------------------------- фоновый цикл


async def tick(bot: Bot, app: App, now: datetime.datetime | None = None) -> None:
    s, aq = app.settings, _aq(app)
    now = (now or aq.now()).astimezone(aq.tz)
    today = now.date()
    day = today.isoformat()
    caretakers, owner = sorted(s.aquarium_caretakers), s.aquarium_owner
    everyone = sorted(s.aquarium_members)

    # 1. Пауза закончилась
    until = await aq.pause_until()
    if isinstance(until, datetime.datetime) and now >= until:
        await aq.resume(until)
        for uid in everyone:
            await safe_send(bot, uid, "▶️ Пауза закончилась. Напоминания снова включены.")
    paused = await aq.is_paused(now)

    # 2. Задачи по расписанию (и досылка пропущенных, пока бот был выключен)
    if not paused:
        resumed = await aq.get_setting("resumed_at")
        resumed_at = datetime.datetime.fromisoformat(resumed) if resumed else None
        late = []
        for at, task_id in await aq.tasks_for(today):
            planned = aq.at(today, at)
            if not planned <= now <= planned + MISSED_WINDOW:
                continue
            if (resumed_at and planned < resumed_at) or await aq.get_task(day, task_id):
                continue
            note = None
            if now - planned > LATE_NOTE_AFTER:
                note = f"⚠️ С опозданием: должно было прийти в {planned:%H:%M} (бот перезапускался)."
            if await send_task(bot, app, task_id, note, now) and note:
                late.append(f"{planned:%H:%M} {TASKS[task_id]}")
        if late:
            await safe_send(bot, owner, "🔄 Досланы пропущенные задачи:\n"
                            + "\n".join(f"• {html.escape(x)}" for x in late))

        # Разгрузочный день — в обычное время кормления
        feed = aq.at(today, await aq.feed_time())
        if aq.is_fasting(today) and feed <= now <= feed + MISSED_WINDOW and await aq.once("fasting_sent", day):
            for uid in caretakers:
                await safe_send(bot, uid, "🚫 <b>Сегодня разгрузочный день</b>\n\nРыбок сегодня <b>НЕ кормим</b> — "
                                "это полезно для их пищеварения и чистоты воды.\n\nОстальные задачи — как обычно.")

    # 3. Повторные напоминания и просрочки
    if not paused:
        for row in await aq.pending(day):
            date, task_id = row["date"], row["task_id"]
            name = html.escape(row["task_name"])
            if not row["reminded"] and now >= remind_at_of(row):
                for uid in caretakers:
                    await safe_send(bot, uid, f"🔔 <b>Напоминание</b>\n\n{name}\n\n"
                                    f"Задача пришла в {hhmm(row['sent_at'])} и ещё не выполнена.",
                                    reply_markup=task_keyboard(date, task_id, row["snooze_count"]))
                await aq.set_flag(date, task_id, "reminded")  # даже при ошибке отправки — без спама
            if not row["overdue_notified"] and now >= overdue_at_of(row):
                for uid in everyone:
                    await safe_send(bot, uid, f"⚠️ <b>Просроченная задача</b>\n\n{name}\n\n"
                                    f"Запланировано: {hhmm(row['sent_at'])}\nЗадача ещё не выполнена.")
                await aq.set_flag(date, task_id, "overdue_notified")

    # 4. Отчёт за день — владельцу; достижения — всем
    if now.time() >= REPORT_TIME and await aq.once("report_sent", day) and not paused:
        await safe_send(bot, owner, await day_report(app, today))
        for streak in await aq.award(day):
            for uid in everyone:
                await safe_send(bot, uid, f"🎉 <b>Новое достижение!</b>\n\n{ACHIEVEMENTS[streak]}\n\nТак держать! 🐠")

    # 5. Недельная статистика и советы — по воскресеньям
    if today.weekday() == SUN and now.time() >= WEEKLY_TIME and await aq.once("weekly_sent", day):
        text = await aq.stats_text(today)
        brain = app.assistant.aquarium_brain
        async with app.queue.slot():
            tips = await brain.advice(app.llm, s.default_model, await aq.care_summary(today))
        if tips:
            text += "\n\n🐠 <b>Советы на неделю</b>\n" + html.escape(tips)
        for uid in everyone:
            await safe_send(bot, uid, text)

    # 6. Вопрос дня: агент расспрашивает об аквариуме, чтобы давать советы точнее
    if now.hour >= s.aquarium_ask_hour and now.hour < 22 and await aq.once("ask_sent", day):
        for uid in sorted(s.aquarium_askees):
            await ask_question(bot, app, uid, now)


async def ask_question(bot: Bot, app: App, user_id: int, now: datetime.datetime | None = None,
                       *, force: bool = False) -> bool:
    """Задаёт вопрос дня. False — спрашивать пока нечего (всё известно и свежо)."""
    brain = app.assistant.aquarium_brain
    now = now or _aq(app).now()
    topic = await brain.choose_topic(now)
    if topic is None and force:
        topic = "health"
    if topic is None:
        return False
    async with app.queue.slot():
        question = await brain.write_question(app.llm, app.settings.default_model, topic)
    qid = await brain.log_question(user_id, topic, question, now)
    title = TOPICS[topic][0]
    try:
        msg = await bot.send_message(
            user_id,
            f"🐠 <b>Вопрос про аквариум</b> · {title}\n\n{html.escape(question)}\n\n"
            "<i>Ответь на это сообщение или нажми «Ответить» — я запомню и учту в советах.</i>",
            parse_mode="HTML", reply_markup=question_keyboard(qid),
        )
    except TelegramAPIError as exc:
        log.warning("aquarium question to %s failed: %s", user_id, exc)
        return False
    await brain.set_question_msg(qid, msg.message_id)
    return True


async def day_report(app: App, today: datetime.date) -> str:
    aq = _aq(app)
    rows = await aq.day_tasks(today.isoformat())
    lines = ["📊 <b>Отчёт по аквариуму</b>", f"📅 {today:%d.%m.%Y}", ""]
    if not rows:
        lines.append("Сегодня задач ещё не было.")
    else:
        for row in rows:
            lines += [format_task_row(row, pending_icon="❌"), ""]
        lines.append(f"📈 Выполнено: {sum(1 for r in rows if r['completed_at'])}/{len(rows)}")
    current, _ = await aq.streaks(today.isoformat())
    lines.append(f"🔥 Серия без пропусков: {current} дн.")
    return "\n".join(lines)

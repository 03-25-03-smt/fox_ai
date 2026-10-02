"""Аквариумы в Telegram: клавиатуры, рассылка задач и фоновый цикл (график ухода,
напоминания, просрочки, отчёт, недельная статистика с советами и графиком,
вопрос дня, предложение графика ухода)."""

import datetime
import html
import logging

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)

from .app import App
from .aquarium import (
    ACHIEVEMENTS,
    CANT_REASONS,
    LATE_NOTE_AFTER,
    MAX_SNOOZES,
    MISSED_WINDOW,
    REPORT_TIME,
    SNOOZE_FOR,
    SUN,
    TEST_TASKS,
    WEEKLY_TIME,
    Aquarium,
    Tank,
    format_task_row,
    hhmm,
    overdue_at_of,
    remind_at_of,
)
from .aquarium_brain import TOPICS, care_chart_code, plan_text
from .services import ServiceError

log = logging.getLogger(__name__)

CB = "aq:"  # aq:<действие>:<дата>:<задача>[:<причина>]
CB_Q = "aqq:"  # aqq:<действие>:<id вопроса>
CB_PLAN = "aqp:"  # aqp:<действие>:<id аквариума>

MENU_TODAY = "📋 Сегодня"
MENU_TOMORROW = "🗓 Завтра"
MENU_OVERDUE = "⚠️ Просрочки"
MENU_PLAN = "🗂 График ухода"
MENU_STATS = "📊 Статистика"
MENU_TANK = "🐠 Что я знаю"
MENU_WATER = "💧 Вода"
MENU_HIDE = "⌨️ Скрыть"
MENU_ITEMS = (MENU_TODAY, MENU_TOMORROW, MENU_OVERDUE, MENU_PLAN, MENU_STATS, MENU_TANK, MENU_WATER, MENU_HIDE)


def main_menu() -> ReplyKeyboardMarkup:
    rows = [[MENU_TODAY, MENU_TOMORROW], [MENU_OVERDUE, MENU_PLAN], [MENU_STATS, MENU_TANK],
            [MENU_WATER, MENU_HIDE]]
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text=t) for t in row] for row in rows],
                               resize_keyboard=True)


def task_keyboard(date: str, key: str, snooze_count: int = 0) -> InlineKeyboardMarkup:
    # Дата в кнопке: нажатие после полуночи отметит задачу за правильный день
    second = []
    if snooze_count < MAX_SNOOZES:
        minutes = int(SNOOZE_FOR.total_seconds() // 60)
        second.append(InlineKeyboardButton(text=f"⏰ +{minutes} мин", callback_data=f"{CB}snooze:{date}:{key}"))
    second.append(InlineKeyboardButton(text="❌ Не сделаю", callback_data=f"{CB}cant:{date}:{key}"))
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Сделано", callback_data=f"{CB}done:{date}:{key}")], second,
    ])


def reasons_keyboard(date: str, key: str) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=text, callback_data=f"{CB}why:{date}:{key}:{code}")]
            for code, text in CANT_REASONS.items()]
    rows.append([InlineKeyboardButton(text="↩️ Назад", callback_data=f"{CB}back:{date}:{key}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def question_keyboard(question_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✍️ Ответить", callback_data=f"{CB_Q}answer:{question_id}"),
        InlineKeyboardButton(text="🤷 Не знаю", callback_data=f"{CB_Q}skip:{question_id}"),
    ]])


def plan_keyboard(tank_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✅ Принять", callback_data=f"{CB_PLAN}ok:{tank_id}"),
        InlineKeyboardButton(text="🔄 Другой", callback_data=f"{CB_PLAN}again:{tank_id}"),
        InlineKeyboardButton(text="✖️", callback_data=f"{CB_PLAN}no:{tank_id}"),
    ]])


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> bool:
    """Не роняет фоновый цикл, если владелец заблокировал бота и т.п."""
    try:
        await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
        return True
    except TelegramAPIError as exc:
        log.warning("aquarium: cannot send to %s: %s", chat_id, exc)
        return False


def _aq(app: App) -> Aquarium:
    return app.assistant.aquarium


async def send_task(bot: Bot, app: App, key: str, name: str, tank_id: int | None,
                    note: str | None = None, now: datetime.datetime | None = None) -> bool:
    """Присылает задачу владельцу. Настоящую задачу — раз в день."""
    date, created = await _aq(app).save_task(key, name, tank_id, now)
    if not created:
        return False
    text = f"🐠 <b>Уход за аквариумом</b>\n\n{html.escape(name)}"
    if note:
        text += f"\n\n{note}"
    return await safe_send(bot, app.settings.aquarium_owner, text, reply_markup=task_keyboard(date, key))


async def send_test_task(bot: Bot, app: App) -> bool:
    return await send_task(bot, app, "test", TEST_TASKS["test"], None)


async def chart(app: App, code: str) -> bytes | None:
    """PNG-график через Python в песочнице; None — песочница выключена или ошибка."""
    if app.sandbox is None:
        return None
    try:
        result = await app.sandbox.python(code)
    except ServiceError as exc:
        log.warning("aquarium chart failed: %s", exc)
        return None
    if not result.images:
        log.warning("aquarium chart: no image: %s", result.run.stderr[-500:])
    return result.images[0] if result.images else None


# ---------------------------------------------------------------- фоновый цикл


async def tick(bot: Bot, app: App, now: datetime.datetime | None = None) -> None:
    s, aq = app.settings, _aq(app)
    owner = s.aquarium_owner
    if not owner:
        return
    await aq.seed(s.aquarium_tanks)
    now = (now or aq.now()).astimezone(aq.tz)
    today = now.date()
    day = today.isoformat()

    # 1. Пауза закончилась
    until = await aq.pause_until()
    if isinstance(until, datetime.datetime) and now >= until:
        await aq.resume(until)
        await safe_send(bot, owner, "▶️ Пауза закончилась. Напоминания об аквариумах снова включены.")
    paused = await aq.is_paused(now)

    if not paused:
        # 2. Задачи по графику (и досылка пропущенных, пока бот был выключен)
        resumed = await aq.get_setting("resumed_at")
        resumed_at = datetime.datetime.fromisoformat(resumed) if resumed else None
        for item in await aq.tasks_for(today):
            planned = aq.at(today, item.at)
            if not planned <= now <= planned + MISSED_WINDOW:
                continue
            if (resumed_at and planned < resumed_at) or await aq.get_task(day, item.key):
                continue
            note = None
            if now - planned > LATE_NOTE_AFTER:
                note = f"⚠️ С опозданием: по графику в {planned:%H:%M} (бот был выключен)."
            await send_task(bot, app, item.key, await aq.task_name(item), item.tank_id, note, now)

        # 3. Повторное напоминание и просрочка
        for row in await aq.pending(day):
            date, key = row["date"], row["task_id"]
            name = html.escape(row["task_name"])
            if not row["reminded"] and now >= remind_at_of(row):
                await safe_send(bot, owner, f"🔔 <b>Напоминание</b>\n\n{name}\n\n"
                                f"Пришло в {hhmm(row['sent_at'])} и ещё не отмечено.",
                                reply_markup=task_keyboard(date, key, row["snooze_count"]))
                await aq.set_flag(date, key, "reminded")  # даже при ошибке отправки — без спама
            if not row["overdue_notified"] and now >= overdue_at_of(row):
                await safe_send(bot, owner, f"⚠️ <b>Просрочено</b>\n\n{name}\n\nПо графику: {hhmm(row['sent_at'])}")
                await aq.set_flag(date, key, "overdue_notified")

    # 4. Отчёт за день и достижения
    if now.time() >= REPORT_TIME and await aq.once("report_sent", day) and not paused:
        if await aq.day_tasks(day):
            await safe_send(bot, owner, await day_report(app, today))
        for streak in await aq.award(day):
            await safe_send(bot, owner, f"🎉 <b>Новое достижение!</b>\n\n{ACHIEVEMENTS[streak]}\n\nТак держать! 🐠")

    # 5. Воскресенье: статистика, график выполнения и советы на неделю
    if today.weekday() == SUN and now.time() >= WEEKLY_TIME and await aq.once("weekly_sent", day):
        await send_weekly(bot, app, today)

    # 6. Вечером: агент предлагает график, если его нет, или задаёт вопрос дня
    if s.aquarium_ask_hour <= now.hour < 22 and await aq.once("ask_sent", day):
        if not await offer_missing_plan(bot, app):
            await ask_question(bot, app, owner, now)


async def send_weekly(bot: Bot, app: App, today: datetime.date) -> None:
    aq, brain, owner = _aq(app), app.assistant.aquarium_brain, app.settings.aquarium_owner
    text = await aq.stats_text(today)
    async with app.queue.slot():
        tips = await brain.advice(app.llm, app.settings.default_model, aq, await aq.care_summary(today))
    if tips:
        text += "\n\n🐠 <b>Советы на неделю</b>\n" + html.escape(tips)
    await safe_send(bot, owner, text)
    points = await aq.daily_completion(today, 30)
    if len(points) >= 3 and (png := await chart(app, care_chart_code(points))):
        try:
            await bot.send_photo(owner, BufferedInputFile(png, "care.png"), caption="Уход за 30 дней")
        except TelegramAPIError as exc:
            log.warning("aquarium chart not sent: %s", exc)


async def offer_missing_plan(bot: Bot, app: App) -> bool:
    """Если у аквариума нет графика, а агент уже кое-что о нём знает, — предлагает график."""
    aq, brain = _aq(app), app.assistant.aquarium_brain
    for tank in await aq.tanks():
        if await aq.schedule(tank.id) or await aq.get_setting(f"plan_offered:{tank.id}"):
            continue
        known = [f for f in await brain.facts(tank.id) if f.tank_id == tank.id]
        if len({f.topic for f in known}) < 2:
            continue  # пока знает слишком мало — сначала поспрашивает
        await aq.set_setting(f"plan_offered:{tank.id}", "1")
        return await send_plan(bot, app, app.settings.aquarium_owner, tank)
    return False


async def send_plan(bot: Bot, app: App, chat_id: int, tank: Tank) -> bool:
    brain = app.assistant.aquarium_brain
    async with app.queue.slot():
        items = await brain.propose_plan(app.llm, app.settings.default_model, _aq(app), tank)
    if not items:
        return await safe_send(bot, chat_id, f"⚠️ Не получилось составить график для {html.escape(tank.label)}. "
                               "Расскажи мне больше об аквариуме (/tank add …) и попробуй /aqplan ещё раз.")
    return await safe_send(
        bot, chat_id,
        f"🗂 <b>Предлагаю график ухода: {html.escape(tank.label)}</b>\n\n{plan_text(items)}\n\n"
        "Принять — заменит текущий график этого аквариума. Поправить потом: /aqschedule",
        reply_markup=plan_keyboard(tank.id),
    )


async def ask_question(bot: Bot, app: App, user_id: int, now: datetime.datetime | None = None,
                       *, force: bool = False) -> bool:
    """Задаёт вопрос дня. False — спрашивать пока нечего."""
    aq, brain = _aq(app), app.assistant.aquarium_brain
    now = now or aq.now()
    tanks = await aq.tanks()
    choice = await brain.choose_topic(tanks, now)
    if choice is None and force and tanks:
        choice = (tanks[0], "health")
    if choice is None:
        return False
    tank, topic = choice
    async with app.queue.slot():
        question = await brain.write_question(app.llm, app.settings.default_model, tank, topic)
    qid = await brain.log_question(user_id, tank.id, topic, question, now)
    try:
        msg = await bot.send_message(
            user_id,
            f"🐠 <b>Вопрос про {html.escape(tank.label)}</b> · {TOPICS[topic][0]}\n\n{html.escape(question)}\n\n"
            "<i>Ответь на это сообщение текстом или голосовым (или нажми «Ответить») — запомню и учту "
            "в советах и графике.</i>",
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
    lines = ["📊 <b>Аквариумы за день</b>", f"📅 {today:%d.%m.%Y}", ""]
    lines += [format_task_row(row, pending_icon="❌") for row in rows]
    lines += ["", f"📈 Выполнено: {sum(1 for r in rows if r['completed_at'])}/{len(rows)}"]
    current, _ = await aq.streaks(today.isoformat())
    lines.append(f"🔥 Серия без пропусков: {current} дн.")
    return "\n".join(lines)

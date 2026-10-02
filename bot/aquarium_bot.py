"""Аквариумы в Telegram: клавиатуры, рассылка задач и фоновый цикл по всем домам (график
ухода, напоминания, просрочки, отчёт, недельная статистика с советами и графиком,
вопрос дня по очереди участникам, предложение графика ухода).

Задача приходит исполнителю пункта графика или, если он не назначен, всем участникам дома.
Кто первым нажал «Сделано» — тот и закрыл: у остальных сообщение обновится. Просрочка
приходит ещё и хозяину. Отчёты и недельная статистика — всем участникам."""

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
    Message,
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


async def safe_send(bot: Bot, chat_id: int, text: str, **kwargs) -> Message | None:
    """Не роняет фоновый цикл, если участник заблокировал бота и т.п."""
    try:
        return await bot.send_message(chat_id, text, parse_mode="HTML", **kwargs)
    except TelegramAPIError as exc:
        log.warning("aquarium: cannot send to %s: %s", chat_id, exc)
        return None


async def send_all(bot: Bot, user_ids: list[int], text: str, **kwargs) -> int:
    sent = 0
    for uid in user_ids:
        sent += bool(await safe_send(bot, uid, text, **kwargs))
    return sent


async def member_ids(aq: Aquarium) -> list[int]:
    return [m.user_id for m in await aq.members()]


async def send_task(bot: Bot, aq: Aquarium, key: str, name: str, tank_id: int | None,
                    note: str | None = None, now: datetime.datetime | None = None,
                    assignee_id: int | None = None) -> bool:
    """Присылает задачу исполнителю или всем в доме. Настоящую задачу — раз в день."""
    date, created = await aq.save_task(key, name, tank_id, now, assignee_id)
    if not created:
        return False
    text = f"🐠 <b>Уход за аквариумом</b>\n\n{html.escape(name)}"
    if note:
        text += f"\n\n{note}"
    sent = False
    for uid in await aq.recipients(assignee_id):
        if msg := await safe_send(bot, uid, text, reply_markup=task_keyboard(date, key)):
            await aq.add_msg(date, key, uid, msg.message_id)
            sent = True
    return sent


async def send_test_task(bot: Bot, aq: Aquarium, user_id: int) -> bool:
    return await send_task(bot, aq, "test", TEST_TASKS["test"], None, assignee_id=user_id)


async def close_for_others(bot: Bot, aq: Aquarium, row: dict, user_id: int, text: str) -> None:
    """Задачу закрыл один участник — у остальных убираем кнопки и пишем, кто это сделал."""
    for uid, msg_id in aq.task_msgs(row).items():
        if uid == user_id:
            continue
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=msg_id, parse_mode="HTML")
        except TelegramAPIError as exc:
            log.debug("aquarium: cannot update %s/%s: %s", uid, msg_id, exc)


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
    homes = app.assistant.aquariums
    if homes is None:
        return
    for aq in await homes.all():
        try:
            await tick_home(bot, app, aq, now)
        except Exception:  # один сломанный дом не должен мешать остальным
            log.exception("aquarium tick failed for home %s", aq.id)


async def tick_home(bot: Bot, app: App, aq: Aquarium, now: datetime.datetime | None = None) -> None:
    s = app.settings
    members = await member_ids(aq)
    if not members:
        return
    owner = aq.home.owner_id
    now = (now or aq.now()).astimezone(aq.tz)
    today = now.date()
    day = today.isoformat()

    # 1. Пауза закончилась
    until = await aq.pause_until()
    if isinstance(until, datetime.datetime) and now >= until:
        await aq.resume(until)
        await send_all(bot, members, "▶️ Пауза закончилась. Напоминания об аквариумах снова включены.")
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
            await send_task(bot, aq, item.key, await aq.task_name(item), item.tank_id, note, now,
                            item.assignee_id)

        # 3. Повторное напоминание и просрочка (просрочка — ещё и хозяину)
        for row in await aq.pending(day):
            date, key = row["date"], row["task_id"]
            name = html.escape(row["task_name"])
            to = await aq.recipients(row["assignee_id"])
            if not row["reminded"] and now >= remind_at_of(row):
                for uid in to:
                    msg = await safe_send(bot, uid, f"🔔 <b>Напоминание</b>\n\n{name}\n\n"
                                          f"Пришло в {hhmm(row['sent_at'])} и ещё не отмечено.",
                                          reply_markup=task_keyboard(date, key, row["snooze_count"]))
                    if msg:
                        await aq.add_msg(date, key, uid, msg.message_id)
                await aq.set_flag(date, key, "reminded")  # даже при ошибке отправки — без спама
            if not row["overdue_notified"] and now >= overdue_at_of(row):
                who = ""
                if row["assignee_id"] and row["assignee_id"] != owner:
                    who = f"\nИсполнитель: {html.escape(await aq.member_name(row['assignee_id']))}"
                extra = [owner] if owner in members and owner not in to else []
                await send_all(bot, to + extra,
                               f"⚠️ <b>Просрочено</b>\n\n{name}\n\nПо графику: {hhmm(row['sent_at'])}{who}")
                await aq.set_flag(date, key, "overdue_notified")

    # 4. Отчёт за день и достижения
    if now.time() >= REPORT_TIME and await aq.once("report_sent", day) and not paused:
        if await aq.day_tasks(day):
            await send_all(bot, members, await day_report(aq, today))
        for streak in await aq.award(day):
            await send_all(bot, members,
                           f"🎉 <b>Новое достижение!</b>\n\n{ACHIEVEMENTS[streak]}\n\nТак держать! 🐠")

    # 5. Воскресенье: статистика, график выполнения и советы на неделю
    if today.weekday() == SUN and now.time() >= WEEKLY_TIME and await aq.once("weekly_sent", day):
        await send_weekly(bot, app, aq, today)

    # 6. Вечером: агент предлагает график, если его нет, или задаёт вопрос дня (участникам по очереди)
    if s.aquarium_ask_hour <= now.hour < 22 and await aq.once("ask_sent", day):
        if not await offer_missing_plan(bot, app, aq):
            turn = int(await aq.get_setting("ask_turn") or 0)
            await aq.set_setting("ask_turn", str(turn + 1))
            await ask_question(bot, app, aq, members[turn % len(members)], now)


async def send_weekly(bot: Bot, app: App, aq: Aquarium, today: datetime.date) -> None:
    members = await member_ids(aq)
    text = await aq.stats_text(today)
    async with app.queue.slot():
        tips = await aq.brain.advice(app.llm, app.settings.default_model, aq, await aq.care_summary(today))
    if tips:
        text += "\n\n🐠 <b>Советы на неделю</b>\n" + html.escape(tips)
    await send_all(bot, members, text)
    points = await aq.daily_completion(today, 30)
    if len(points) >= 3 and (png := await chart(app, care_chart_code(points))):
        for uid in members:
            try:
                await bot.send_photo(uid, BufferedInputFile(png, "care.png"), caption="Уход за 30 дней")
            except TelegramAPIError as exc:
                log.warning("aquarium chart not sent: %s", exc)


async def offer_missing_plan(bot: Bot, app: App, aq: Aquarium) -> bool:
    """Если у аквариума нет графика, а агент уже кое-что о нём знает, — предлагает график хозяину."""
    for tank in await aq.tanks():
        if await aq.schedule(tank.id) or await aq.get_setting(f"plan_offered:{tank.id}"):
            continue
        known = [f for f in await aq.brain.facts(tank.id) if f.tank_id == tank.id]
        if len({f.topic for f in known}) < 2:
            continue  # пока знает слишком мало — сначала поспрашивает
        await aq.set_setting(f"plan_offered:{tank.id}", "1")
        return await send_plan(bot, app, aq, aq.home.owner_id, tank)
    return False


async def send_plan(bot: Bot, app: App, aq: Aquarium, chat_id: int, tank: Tank) -> bool:
    async with app.queue.slot():
        items = await aq.brain.propose_plan(app.llm, app.settings.default_model, aq, tank)
    if not items:
        return bool(await safe_send(
            bot, chat_id, f"⚠️ Не получилось составить график для {html.escape(tank.label)}. "
            "Расскажи мне больше об аквариуме (/tank add …) и попробуй /aqplan ещё раз."))
    return bool(await safe_send(
        bot, chat_id,
        f"🗂 <b>Предлагаю график ухода: {html.escape(tank.label)}</b>\n\n{plan_text(items)}\n\n"
        "Принять — заменит текущий график этого аквариума. Поправить потом: /aqschedule, "
        "поручить пункт кому-то: /aqassign",
        reply_markup=plan_keyboard(tank.id),
    ))


async def ask_question(bot: Bot, app: App, aq: Aquarium, user_id: int, now: datetime.datetime | None = None,
                       *, force: bool = False) -> bool:
    """Задаёт вопрос дня. False — спрашивать пока нечего."""
    brain = aq.brain
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


async def day_report(aq: Aquarium, today: datetime.date) -> str:
    rows = await aq.day_tasks(today.isoformat())
    shared = len(await aq.members()) > 1
    lines = ["📊 <b>Аквариумы за день</b>", f"📅 {today:%d.%m.%Y}", ""]
    for row in rows:
        line = format_task_row(row, pending_icon="❌")
        if shared and row["completed_by_name"]:
            line += f" ({html.escape(row['completed_by_name'])})"
        lines.append(line)
    lines += ["", f"📈 Выполнено: {sum(1 for r in rows if r['completed_at'])}/{len(rows)}"]
    current, _ = await aq.streaks(today.isoformat())
    lines.append(f"🔥 Серия без пропусков: {current} дн.")
    return "\n".join(lines)

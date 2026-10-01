"""Аквариум: меню, кнопки под задачами, команды владельца, знания и тесты воды.

Доступ: ухаживающие (AQUARIUM_CARETAKER_IDS) и владелец (AQUARIUM_OWNER_ID / первый админ).
"""

import datetime
import html
import os
import tempfile

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, Message, ReplyKeyboardRemove

from ..app import App
from ..aquarium import (
    ALL_TASKS,
    CANT_REASONS,
    DAY_SHORT,
    FASTING_DAYS,
    MAX_SNOOZES,
    REPORT_TIME,
    SNOOZE_FOR,
    TASKS,
    TEST_TASKS,
    WEEKDAYS_RU,
    WEEKLY_TIME,
    Aquarium,
    days_text,
    format_task_row,
    hhmm,
    parse_dt,
)
from ..aquarium_bot import (
    CB,
    CB_Q,
    MENU_HELP,
    MENU_HIDE,
    MENU_HISTORY,
    MENU_ITEMS,
    MENU_OVERDUE,
    MENU_STATS,
    MENU_TANK,
    MENU_TODAY,
    MENU_TOMORROW,
    ask_question,
    main_menu,
    reasons_keyboard,
    safe_send,
    send_task,
    task_keyboard,
)
from ..aquarium_brain import TOPICS, WATER_PARAMS, parse_water, water_warnings
from ..assistant import Turn
from .common import respond, with_user
from .registry import Routes

router = Routes("aquarium")

MAX_IMPORT = 20 * 1024 * 1024


def _aq(app: App) -> Aquarium:
    return app.assistant.aquarium


def is_member(app: App, user_id: int) -> bool:
    return user_id in app.settings.aquarium_members


def is_owner(app: App, user_id: int) -> bool:
    return app.settings.aquarium_enabled and user_id == app.settings.aquarium_owner


async def member_filter(event, app: App) -> bool:
    user = getattr(event, "from_user", None)
    return user is not None and is_member(app, user.id)


async def owner_filter(event, app: App) -> bool:
    user = getattr(event, "from_user", None)
    return user is not None and is_owner(app, user.id)


def display_name(turn: Turn) -> str:
    return (turn.user.name if turn.user and turn.user.name else "") or turn.author or "Неизвестный"


def _e(text) -> str:
    return html.escape(str(text))


async def _html(message: Message, text: str, **kwargs) -> None:
    await message.answer(text, parse_mode=ParseMode.HTML, **kwargs)


# ---------------------------------------------------------------- меню и просмотр


HELP = (
    "🐠 <b>Аквариум</b>\n\n"
    "Каждый день я напоминаю о задачах ухода. Под задачей кнопки:\n"
    "✅ Выполнено · ⏰ Отложить на {snooze} мин (до {snoozes} раз) · ❌ Не могу — выбрать причину\n\n"
    "Кнопки меню внизу: сегодня, завтра, просрочки, история, статистика, что я знаю об аквариуме.\n\n"
    "Можно просто спросить меня про аквариум или прислать его фото — посмотрю и подскажу.\n"
    "/water pH 7.2 NO2 0 NO3 20 — записать тест воды · /water — история\n"
    "/tank — что я знаю · /tank add в аквариуме 5 неонов — рассказать мне что-то новое\n"
    "/aq — меню"
)
OWNER_HELP = (
    "\n\n🔧 <b>Владелец</b>\n"
    "/aqschedule — расписание · /aqsettime &lt;задача&gt; &lt;ЧЧ:ММ|default&gt;\n"
    "/aqpause [дни] · /aqresume — пауза\n"
    "/aqtest · /aqoverdue_test — тестовые задачи\n"
    "/aqask — задать вопрос дня сейчас · /tank del &lt;номер&gt;\n"
    "Файл <code>aquarium.db</code> с подписью /aqimport — перенести базу старого fish_helper\n\n"
    "Тебе приходят: выполнено / не могу, просрочки, отчёт в {report}, "
    "статистика с советами по вс в {weekly}, вопрос дня."
)


def help_text(app: App, user_id: int) -> str:
    text = HELP.format(snooze=int(SNOOZE_FOR.total_seconds() // 60), snoozes=MAX_SNOOZES)
    if is_owner(app, user_id):
        text += OWNER_HELP.format(report=REPORT_TIME.strftime("%H:%M"), weekly=WEEKLY_TIME.strftime("%H:%M"))
    return text


@router.message(Command("aq", "aqhelp"), member_filter)
async def cmd_aq(message: Message, app: App, turn: Turn) -> None:
    await _html(message, help_text(app, turn.user_id), reply_markup=main_menu(is_owner(app, turn.user_id)))


async def show_today(message: Message, app: App, turn: Turn) -> None:
    aq = _aq(app)
    now = aq.now()
    rows = await aq.day_tasks(now.date().isoformat())
    lines = ["📋 <b>Аквариум сегодня</b>", f"📅 {now:%d.%m.%Y}", ""]
    if await aq.is_paused():
        lines += [f"⏸ Напоминания на паузе {_e(await aq.pause_text())}", ""]
    if aq.is_fasting(now.date()):
        lines += ["🚫 Разгрузочный день — рыбок не кормим", ""]
    if not rows:
        lines.append("Сегодня задач ещё не было.")
    else:
        for row in rows:
            lines += [format_task_row(row), ""]
        lines.append(f"📈 Выполнено: {sum(1 for r in rows if r['completed_at'])}/{len(rows)}")
    await _html(message, "\n".join(lines))


async def show_tomorrow(message: Message, app: App, turn: Turn) -> None:
    aq = _aq(app)
    day = aq.now().date() + datetime.timedelta(days=1)
    lines = ["🗓 <b>Задачи на завтра</b>", f"📅 {day:%d.%m.%Y}, {WEEKDAYS_RU[day.weekday()]}", ""]
    until = await aq.pause_until()
    if until is not None and (isinstance(until, str) or until > aq.at(day, datetime.time(0))):
        lines += [f"⏸ Напоминания на паузе {_e(await aq.pause_text())}", ""]
    if aq.is_fasting(day):
        lines.append(f"🚫 {(await aq.feed_time()):%H:%M} — Разгрузочный день, НЕ кормить")
    items = await aq.tasks_for(day)
    if not items:
        lines.append("Задач нет.")
    else:
        lines += [f"🕐 {at:%H:%M} — {_e(TASKS[task_id])}" for at, task_id in items]
        lines += ["", f"Всего: {len(items)}"]
    await _html(message, "\n".join(lines))


async def show_overdue(message: Message, app: App, turn: Turn) -> None:
    aq = _aq(app)
    now = aq.now()
    # Владельцу видны и тестовые — чтобы проверять /aqoverdue_test
    rows = await aq.overdue(now, include_test=is_owner(app, turn.user_id))
    if not rows:
        await message.answer("👍 Просроченных задач нет.")
        return
    lines = ["⚠️ <b>Просроченные задачи</b>", ""]
    for row in rows:
        sent = parse_dt(row["sent_at"])
        hours, minutes = divmod(int((now - sent).total_seconds() // 60), 60)
        lines += [f"⏳ {_e(row['task_name'])}\n   Запланировано: {sent:%H:%M} (прошло {hours} ч {minutes} мин)", ""]
    lines.append(f"Всего: {len(rows)}")
    await _html(message, "\n".join(lines))


async def show_history(message: Message, app: App, turn: Turn) -> None:
    aq = _aq(app)
    rows = await aq.since((aq.now().date() - datetime.timedelta(days=6)).isoformat())
    if not rows:
        await message.answer("📚 История пока пустая.")
        return
    lines = ["📚 <b>История аквариума</b>", "Последние 7 дней:", ""]
    current = None
    for row in rows:
        if row["date"] != current:
            current = row["date"]
            lines.append(f"📅 <b>{datetime.date.fromisoformat(current):%d.%m.%Y}</b>")
        lines += [format_task_row(row, indent="  ", pending_icon="❌"), ""]
    text = "\n".join(lines)
    if len(text) > 4000:  # лимит Telegram — 4096
        text = text[:4000].rsplit("\n", 1)[0] + "\n\n…"
    await _html(message, text)


async def show_stats(message: Message, app: App, turn: Turn) -> None:
    await _html(message, await _aq(app).stats_text(_aq(app).now().date()))


async def show_tank(message: Message, app: App, turn: Turn) -> None:
    brain = app.assistant.aquarium_brain
    facts = await brain.facts()
    lines = ["🐠 <b>Что я знаю об аквариуме</b>", ""]
    if not facts:
        lines.append("Пока почти ничего. Я буду иногда спрашивать, а можно рассказать самому: "
                     "/tank add объём 60 л, 6 гуппи и 2 сомика")
    for topic, (title, _) in TOPICS.items():
        items = [f for f in facts if f.topic == topic]
        if items:
            lines.append(f"<b>{title}</b>")
            lines += [f"<code>{f.id}</code> {_e(f.text)}" for f in items]
            lines.append("")
    missing = [TOPICS[t][0] for t in TOPICS if not any(f.topic == t for f in facts)]
    if facts and missing:
        lines.append("Ещё не знаю: " + ", ".join(missing))
    lines.append("\nДобавить: /tank add … · удалить: /tank del номер")
    await _html(message, "\n".join(lines))


async def show_help(message: Message, app: App, turn: Turn) -> None:
    await _html(message, help_text(app, turn.user_id))


async def hide_menu(message: Message, app: App, turn: Turn) -> None:
    await message.answer("Меню скрыто. Вернуть: /aq", reply_markup=ReplyKeyboardRemove())


MENU_ACTIONS = {
    MENU_TODAY: show_today, MENU_TOMORROW: show_tomorrow, MENU_OVERDUE: show_overdue,
    MENU_HISTORY: show_history, MENU_STATS: show_stats, MENU_TANK: show_tank, MENU_HELP: show_help,
    MENU_HIDE: hide_menu,
}


@router.message(F.text.in_(MENU_ITEMS), member_filter)
async def on_menu(message: Message, app: App, turn: Turn) -> None:
    await MENU_ACTIONS[message.text](message, app, turn)


# ---------------------------------------------------------------- кнопки под задачами


async def _notify_owner_cant(bot: Bot, app: App, row: dict, user_id: int) -> None:
    if user_id != app.settings.aquarium_owner:
        await safe_send(bot, app.settings.aquarium_owner,
                        f"🚫 <b>{_e(row['cant_by_name'])}</b> не может выполнить:\n\n{_e(row['task_name'])}\n\n"
                        f"Причина: {_e(row['cant_reason'])}")


@router.callback_query(F.data.startswith(CB), member_filter)
async def on_task_button(callback: CallbackQuery, bot: Bot, app: App, turn: Turn) -> None:
    parts = callback.data.removeprefix(CB).split(":")
    msg = callback.message
    if len(parts) not in (3, 4) or parts[2] not in ALL_TASKS or not isinstance(msg, Message):
        await callback.answer()
        return
    action, date, task_id = parts[:3]
    extra = parts[3] if len(parts) == 4 else None
    aq, name, task_name = _aq(app), display_name(turn), _e(ALL_TASKS[task_id])
    owner = app.settings.aquarium_owner

    if action == "done":
        await callback.answer()
        row, just_now = await aq.complete(date, task_id, turn.user_id, name)
        if row is None or not row["completed_at"]:
            await msg.edit_text(f"⚠️ Не нашёл задачу «{task_name}» в базе.")
            return
        done_at = hhmm(row["completed_at"])
        await msg.edit_text(f"✅ <b>Выполнено</b>\n\n{task_name}\n\n👤 Выполнил: {_e(row['completed_by_name'])}\n"
                            f"🕐 Время: {done_at}", parse_mode=ParseMode.HTML)
        if just_now and turn.user_id != owner:
            test = "🧪 (тест) " if task_id in TEST_TASKS else ""
            await safe_send(bot, owner, f"✅ {test}<b>{_e(row['completed_by_name'])}</b> выполнил:\n\n{task_name}\n\n"
                            f"📨 Напоминание: {hhmm(row['sent_at'])}\n🕐 Выполнено: {done_at}")
    elif action == "snooze":
        row, ok = await aq.snooze(date, task_id)
        if not ok:
            closed = row is not None and (row["completed_at"] or row["cant_at"])
            await callback.answer("Эта задача уже закрыта." if closed
                                  else f"Больше откладывать нельзя (максимум {MAX_SNOOZES} раза).", show_alert=True)
            return
        await callback.answer("Отложено")
        await msg.edit_text(f"⏰ <b>Отложено</b>\n\n{task_name}\n\nНапомню ещё раз в {hhmm(row['remind_at'])}.\n"
                            f"Можно отложить ещё: {MAX_SNOOZES - row['snooze_count']} р.", parse_mode=ParseMode.HTML)
    elif action == "cant":
        await callback.answer()
        await msg.edit_reply_markup(reply_markup=reasons_keyboard(date, task_id))
    elif action == "back":
        await callback.answer()
        row = await aq.get_task(date, task_id)
        await msg.edit_reply_markup(reply_markup=task_keyboard(date, task_id, row["snooze_count"] if row else 0))
    elif action == "why":
        await callback.answer()
        if extra == "other":
            aq_pending[turn.user_id] = (date, task_id)
            await msg.edit_text(f"✍️ <b>Напиши причину одним сообщением</b>\n\n{task_name}", parse_mode=ParseMode.HTML)
            return
        reason = CANT_REASONS.get(extra, "Без причины")
        row, ok = await aq.cant(date, task_id, name, reason)
        if not ok:
            await msg.edit_text("Эта задача уже закрыта.")
            return
        await msg.edit_text(f"🚫 <b>Не получилось</b>\n\n{task_name}\n\nПричина: {_e(reason)}\n"
                            f"Я передал {_e(app.settings.aquarium_owner_name)}.", parse_mode=ParseMode.HTML)
        await _notify_owner_cant(bot, app, row, turn.user_id)
    else:
        await callback.answer()


# Кто сейчас пишет причину «не могу»: user_id -> (дата, задача)
aq_pending: dict[int, tuple[str, str]] = {}


async def writing_reason(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in aq_pending


@router.message(F.text & ~F.text.startswith("/"), writing_reason)
async def on_reason(message: Message, bot: Bot, app: App, turn: Turn) -> None:
    date, task_id = aq_pending.pop(turn.user_id)
    reason = message.text.strip()[:300]
    row, ok = await _aq(app).cant(date, task_id, display_name(turn), reason)
    if not ok:
        await message.answer("Эта задача уже закрыта.")
        return
    await message.answer(f"🚫 Записал: «{reason}».\nЯ передал {app.settings.aquarium_owner_name}.")
    await _notify_owner_cant(bot, app, row, turn.user_id)


# ---------------------------------------------------------------- владелец


@router.message(Command("aqschedule"), owner_filter)
async def cmd_schedule(message: Message, app: App) -> None:
    aq = _aq(app)
    lines = ["🗓 <b>Расписание</b>", ""]
    for item in sorted(await aq.schedule(), key=lambda i: i.at):
        lines.append(f"🕐 {item.at:%H:%M} — {_e(TASKS[item.task_id])}\n   <code>{item.task_id}</code> · "
                     f"{days_text(item.days)}")
    lines += ["", f"🚫 Разгрузочный день: {', '.join(DAY_SHORT[d] for d in FASTING_DAYS)}",
              f"📊 Отчёт: {REPORT_TIME:%H:%M} · 📈 Статистика: вс {WEEKLY_TIME:%H:%M}",
              f"❓ Вопрос дня: после {app.settings.aquarium_ask_hour}:00"]
    if await aq.is_paused():
        lines.append(f"⏸ Пауза {_e(await aq.pause_text())}")
    lines += ["", "Поменять время: /aqsettime light_on 15:00"]
    await _html(message, "\n".join(lines))


@router.message(Command("aqsettime"), owner_filter)
async def cmd_settime(message: Message, command: CommandObject, app: App) -> None:
    usage = ("Использование:\n/aqsettime <задача> <ЧЧ:ММ>\n/aqsettime <задача> default\n\n"
             f"Задачи: {', '.join(TASKS)}\nПример: /aqsettime light_on 15:00")
    args = (command.args or "").lower().split()
    if len(args) != 2 or args[0] not in TASKS:
        await message.answer(usage)
        return
    task_id, value = args
    if value == "default":
        await _aq(app).set_override(task_id, None)
    else:
        try:
            parsed = datetime.datetime.strptime(value, "%H:%M")
        except ValueError:
            await message.answer(f"❌ Неверное время «{value}». Нужно ЧЧ:ММ, например 07:30")
            return
        await _aq(app).set_override(task_id, parsed.strftime("%H:%M"))
    new = next(i.at for i in await _aq(app).schedule() if i.task_id == task_id)
    await message.answer(f"✅ {TASKS[task_id]} — теперь в {new:%H:%M}"
                         + (" (по умолчанию)" if value == "default" else "") + "\n\nПроверить: /aqschedule")


@router.message(Command("aqpause"), owner_filter)
async def cmd_pause(message: Message, command: CommandObject, bot: Bot, app: App) -> None:
    arg = (command.args or "").strip()
    if arg and (not arg.isdigit() or int(arg) < 1):
        await message.answer("❌ Укажи число дней, например: /aqpause 3\nИли просто /aqpause — до /aqresume")
        return
    aq = _aq(app)
    await aq.pause(int(arg) if arg else None)
    text = f"⏸ Напоминания об аквариуме на паузе {await aq.pause_text()}."
    await message.answer(text + "\n\nСнять паузу: /aqresume")
    for uid in app.settings.aquarium_caretakers - {message.from_user.id}:
        await safe_send(bot, uid, html.escape(text))


@router.message(Command("aqresume"), owner_filter)
async def cmd_resume(message: Message, bot: Bot, app: App) -> None:
    aq = _aq(app)
    if await aq.pause_until() is None:
        await message.answer("▶️ Паузы и так нет.")
        return
    await aq.resume()
    text = "▶️ Пауза снята. Напоминания об аквариуме снова включены."
    await message.answer(text)
    for uid in app.settings.aquarium_caretakers - {message.from_user.id}:
        await safe_send(bot, uid, text)


@router.message(Command("aqtest"), owner_filter)
async def cmd_test(message: Message, bot: Bot, app: App) -> None:
    delivered = await send_task(bot, app, "test")
    if delivered:
        await message.answer(f"🧪 Тестовая задача отправлена: {', '.join(map(str, delivered))}")
    else:
        await message.answer("⚠️ Тестовая задача никому не дошла.\nПроверь, что брат нажал /start у бота.")


@router.message(Command("aqoverdue_test"), owner_filter)
async def cmd_overdue_test(message: Message, app: App) -> None:
    await _aq(app).insert_overdue_test(_aq(app).now())
    await message.answer("🧪 Тестовая просроченная задача создана (будто отправлена 2 часа назад).\n"
                         "В течение минуты придут напоминание и предупреждение о просрочке. "
                         f"Список — «{MENU_OVERDUE}».")


@router.message(Command("aqask"), owner_filter)
async def cmd_ask(message: Message, bot: Bot, app: App) -> None:
    if not await ask_question(bot, app, message.from_user.id, force=True):
        await message.answer("⚠️ Не получилось задать вопрос.")


@router.message(F.document & F.caption.startswith("/aqimport"), owner_filter)
async def on_import(message: Message, bot: Bot, app: App) -> None:
    doc = message.document
    if (doc.file_size or 0) > MAX_IMPORT:
        await message.answer("Файл больше 20 МБ.")
        return
    data = (await bot.download(doc)).read()
    if not data.startswith(b"SQLite format 3"):
        await message.answer("⚠️ Это не база SQLite. Пришли файл из /backup старого бота (aquarium_*.db).")
        return
    fd, path = tempfile.mkstemp(suffix=".db")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        stats = await _aq(app).import_legacy(path)
    except (ValueError, OSError) as exc:
        await message.answer(f"⚠️ Не получилось: {exc}")
        return
    except Exception as exc:  # битый файл — sqlite3.DatabaseError и подобное
        await message.answer(f"⚠️ Не получилось прочитать базу: {exc}")
        return
    finally:
        os.remove(path)
    await message.answer(
        f"📥 Перенесено задач: {stats['tasks']} из {stats['total']} (остальные уже были), "
        f"изменений расписания: {stats['schedule']}, достижений: {stats['achievements']}.\n"
        "Теперь можно выключить старый бот на Railway."
    )


# ---------------------------------------------------------------- знания и вода


@router.message(Command("tank"), member_filter)
async def cmd_tank(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    brain = app.assistant.aquarium_brain
    action, _, rest = (command.args or "").strip().partition(" ")
    if action == "add" and rest.strip():
        async with app.queue.slot():
            added = await brain.learn(app.llm, app.settings.default_model, rest, source="tank")
        if not added:
            fid = await brain.add_fact("tank", rest, "tank")
            added = [rest.strip()] if fid else []
        await message.answer("🧠 Запомнил:\n" + "\n".join(f"• {a}" for a in added) if added
                             else "Это я уже знаю 🙂")
    elif action == "del" and rest.strip().isdigit():
        if not is_owner(app, turn.user_id):
            await message.answer("Удалять может только владелец.")
            return
        ok = await brain.delete_fact(int(rest))
        await message.answer("🗑 Забыл" if ok else "Нет такого номера, см. /tank")
    else:
        await show_tank(message, app, turn)


@router.message(Command("water"), member_filter)
async def cmd_water(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    brain = app.assistant.aquarium_brain
    if command.args:
        values = parse_water(command.args)
        if not values:
            await message.answer("Не понял значения 🤔 Пример: /water pH 7.2 NO2 0 NO3 20 KH 6 GH 8 T 25")
            return
        await brain.add_water(values)
        warnings = water_warnings(values)
        lines = ["💧 Записал: " + ", ".join(f"{WATER_PARAMS[k][0]} {v:g}" for k, v in values.items())]
        if warnings:
            lines += ["", "⚠️ Обрати внимание:"] + [f"• {w}" for w in warnings]
            lines.append("\nЧто делать — спроси меня, например: «что делать, если нитриты высокие?»")
            owner = app.settings.aquarium_owner
            if turn.user_id != owner:
                await safe_send(bot, owner, f"⚠️ <b>Тест воды от {_e(display_name(turn))}</b>\n"
                                + "\n".join(f"• {_e(w)}" for w in warnings))
        else:
            lines.append("✅ Всё в норме.")
        await message.answer("\n".join(lines))
        return
    history = await brain.water_history(5)
    if not history:
        await message.answer("💧 Тестов воды ещё не было. Запиши: /water pH 7.2 NO2 0 NO3 20 KH 6 GH 8 T 25")
        return
    lines = ["💧 <b>Тесты воды</b> (новые слева)", ""]
    for key, values in history.items():
        name, unit, low, high = WATER_PARAMS[key]
        norm = f"{low:g}–{high:g}" if low is not None else f"≤ {high:g}"
        bad = " ⚠️" if water_warnings({key: values[0][1]}) else ""
        lines.append(f"{name}: " + " ← ".join(f"{v:g}" for _, v in values) + f" {unit} (норма {norm}){bad}")
    await _html(message, "\n".join(lines))


# ---------------------------------------------------------------- ответы на вопрос дня


@router.callback_query(F.data.startswith(CB_Q), member_filter)
async def on_question_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    brain = app.assistant.aquarium_brain
    action, _, raw = callback.data.removeprefix(CB_Q).partition(":")
    question = await brain.get_question(int(raw)) if raw.isdigit() else None
    if question is None or question[1] != turn.user_id or question[4] != 0:
        await callback.answer("Этот вопрос уже закрыт", show_alert=True)
        return
    await callback.answer()
    if isinstance(callback.message, Message):
        await callback.message.edit_reply_markup(reply_markup=None)
    if action == "answer":
        brain.answering[turn.user_id] = question[0]
        await callback.message.answer("✍️ Слушаю — напиши ответ одним сообщением.")
    else:
        await brain.close_question(question[0], answered=False)
        await callback.message.answer("👌 Хорошо, спрошу о чём-нибудь другом в другой раз.")


async def answering_question(message: Message, app: App) -> bool:
    user = message.from_user
    if user is None or not is_member(app, user.id):
        return False
    brain = app.assistant.aquarium_brain
    if user.id in brain.answering:
        return True
    reply = message.reply_to_message
    return bool(reply and await brain.question_by_msg(user.id, reply.message_id))


@router.message(F.text & ~F.text.startswith("/"), answering_question)
async def on_answer(message: Message, app: App, turn: Turn) -> None:
    brain = app.assistant.aquarium_brain
    qid = brain.answering.pop(turn.user_id, None)
    if qid is None and message.reply_to_message:
        qid = await brain.question_by_msg(turn.user_id, message.reply_to_message.message_id)
    question = await brain.get_question(qid) if qid else None
    if question is None:
        return
    await brain.close_question(qid)
    async with app.queue.slot():
        added = await brain.learn(app.llm, app.settings.default_model, message.text,
                                  source="question", question=question[3])
    if added:
        await message.answer("🧠 Запомнил:\n" + "\n".join(f"• {a}" for a in added))
    prompt = (f"Ты спросил меня про аквариум: «{question[3]}». Мой ответ: {message.text}\n"
              "Коротко (2–4 предложения) прокомментируй как аквариумист: всё ли в порядке, "
              "и дай один полезный совет с учётом того, что знаешь об аквариуме.")
    await respond(message, app, with_user(turn, mode="aquarium"), prompt, extract_memory=False,
                  allow_tools=False, keyboard=False)

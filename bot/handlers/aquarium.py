"""Аквариумы владельца: меню, кнопки под задачами, график ухода, знания, тесты воды.

Доступно только владельцу (AQUARIUM_OWNER_ID или первый из ADMIN_IDS).
"""

import datetime
import html

from aiogram import Bot, F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, Message, ReplyKeyboardRemove

from ..app import App
from ..aquarium import (
    CANT_REASONS,
    KINDS,
    MAX_SNOOZES,
    REPORT_TIME,
    TEST_TASKS,
    WEEKDAYS_RU,
    WEEKLY_TIME,
    Aquarium,
    Tank,
    days_text,
    format_task_row,
    hhmm,
    parse_days,
    parse_dt,
)
from ..aquarium_bot import (
    CB,
    CB_PLAN,
    CB_Q,
    MENU_HIDE,
    MENU_ITEMS,
    MENU_OVERDUE,
    MENU_PLAN,
    MENU_STATS,
    MENU_TANK,
    MENU_TODAY,
    MENU_TOMORROW,
    MENU_WATER,
    ask_question,
    chart,
    main_menu,
    reasons_keyboard,
    send_plan,
    send_test_task,
    task_keyboard,
)
from ..aquarium_brain import (
    TOPICS,
    WATER_PARAMS,
    care_chart_code,
    parse_water,
    water_chart_code,
    water_warnings,
)
from ..assistant import Turn
from .common import respond, with_user
from .registry import Routes

router = Routes("aquarium")


def _aq(app: App) -> Aquarium:
    return app.assistant.aquarium


async def owner_filter(event, app: App) -> bool:
    user = getattr(event, "from_user", None)
    return user is not None and user.id in app.settings.aquarium_members


def _e(text) -> str:
    return html.escape(str(text))


async def _html(message: Message, text: str, **kwargs) -> None:
    await message.answer(text, parse_mode=ParseMode.HTML, **kwargs)


async def split_tank(app: App, args: str | None) -> tuple[Tank | None, str, list[Tank]]:
    """«85 pH 7» -> (аквариум 85 л, «pH 7», все). Один аквариум — он по умолчанию."""
    text = (args or "").strip()
    tanks = await _aq(app).tanks()
    first, _, rest = text.partition(" ")
    if first and (tank := await _aq(app).find_tank(first.rstrip(":"))):
        return tank, rest.strip(), tanks
    return (tanks[0] if len(tanks) == 1 else None), text, tanks


def tanks_hint(tanks: list[Tank], example: str) -> str:
    names = ", ".join(f"{t.volume:g} ({_e(t.name)})" for t in tanks)
    return f"Укажи аквариум первым словом — объёмом или именем: {names}.\nПример: {example}"


# ---------------------------------------------------------------- меню и просмотр


HELP = (
    "🐠 <b>Мои аквариумы</b>\n\n"
    "Я присылаю задачи по графику ухода. Под задачей: ✅ Сделано · ⏰ +15 мин (до {snoozes} раз) · "
    "❌ Не сделаю (с причиной). Через 30 мин — напоминание, через час — «просрочено».\n"
    "Отчёт за день — в {report}, по воскресеньям в {weekly} — статистика, график и советы.\n"
    "Каждый вечер я задаю вопрос про один из аквариумов — так я узнаю их и советую точнее.\n\n"
    "<b>Аквариумы и график</b>\n"
    "/aqtank — список · /aqtank add Креветочник 20 · /aqtank del номер\n"
    "/aqplan [аквариум] — я предложу график ухода\n"
    "/aqschedule — график · /aqadd 85 19:00 ср,вс Почистить губку · /aqdel номер\n"
    "/aqsettime номер 20:00 · /aqpause [дни] · /aqresume\n\n"
    "<b>Знания и вода</b>\n"
    "/tank [аквариум] — что я знаю · /tank add 5 живёт петушок · /tank del номер\n"
    "/water 85 pH 7.2 NO2 0 NO3 20 KH 6 GH 8 T 25 — тест воды\n"
    "/water 85 — история и график · /aqchart — график ухода за 30 дней\n"
    "/aqask — вопрос сейчас · /aqtest · /aqoverdue_test — проверить напоминания\n\n"
    "Можно просто спросить про аквариум (режим 🐠 Аквариумист в /mode) или прислать фото."
)


@router.message(Command("aq", "aqhelp"), owner_filter)
async def cmd_aq(message: Message, app: App) -> None:
    await _aq(app).seed(app.settings.aquarium_tanks)
    await _html(message, HELP.format(snoozes=MAX_SNOOZES, report=f"{REPORT_TIME:%H:%M}",
                                     weekly=f"{WEEKLY_TIME:%H:%M}"), reply_markup=main_menu())


async def show_today(message: Message, app: App) -> None:
    aq = _aq(app)
    now = aq.now()
    rows = await aq.day_tasks(now.date().isoformat())
    lines = ["📋 <b>Аквариумы сегодня</b>", f"📅 {now:%d.%m.%Y}, {WEEKDAYS_RU[now.weekday()]}", ""]
    if await aq.is_paused():
        lines += [f"⏸ На паузе {_e(await aq.pause_text())}", ""]
    done_keys = {r["task_id"] for r in rows}
    lines += [format_task_row(r) for r in rows]
    upcoming = [i for i in await aq.tasks_for(now.date()) if i.key not in done_keys and aq.at(now.date(), i.at) > now]
    if upcoming:
        lines += ["", "Дальше сегодня:"] + [f"🕐 {i.at:%H:%M} {_e(await aq.task_name(i))}" for i in upcoming]
    if not rows and not upcoming:
        lines.append("Сегодня задач нет." if await aq.schedule() else "График ухода пуст — /aqplan")
    await _html(message, "\n".join(lines))


async def show_tomorrow(message: Message, app: App) -> None:
    aq = _aq(app)
    day = aq.now().date() + datetime.timedelta(days=1)
    lines = ["🗓 <b>Задачи на завтра</b>", f"📅 {day:%d.%m.%Y}, {WEEKDAYS_RU[day.weekday()]}", ""]
    until = await aq.pause_until()
    if until is not None and (isinstance(until, str) or until > aq.at(day, datetime.time(0))):
        lines += [f"⏸ На паузе {_e(await aq.pause_text())}", ""]
    items = await aq.tasks_for(day)
    lines += [f"🕐 {i.at:%H:%M} — {_e(await aq.task_name(i))}" for i in items] or ["Задач нет."]
    await _html(message, "\n".join(lines))


async def show_overdue(message: Message, app: App) -> None:
    aq = _aq(app)
    now = aq.now()
    rows = await aq.overdue(now)
    if not rows:
        await message.answer("👍 Просроченных задач нет.")
        return
    lines = ["⚠️ <b>Просроченные задачи</b>", ""]
    for row in rows:
        sent = parse_dt(row["sent_at"])
        hours, minutes = divmod(int((now - sent).total_seconds() // 60), 60)
        lines.append(f"⏳ {_e(row['task_name'])} — с {sent:%H:%M} (прошло {hours} ч {minutes} мин)")
    await _html(message, "\n".join(lines))


async def show_schedule(message: Message, app: App) -> None:
    aq = _aq(app)
    lines = ["🗂 <b>График ухода</b>"]
    for tank in await aq.tanks():
        lines += ["", f"<b>{_e(tank.label)}</b>"]
        items = await aq.schedule(tank.id)
        lines += [f"<code>{i.id}</code> {i.icon} {i.at:%H:%M} · {days_text(i.days)} · {_e(i.title)}"
                  for i in items] or ["пусто — /aqplan " + f"{tank.volume:g}"]
    if await aq.is_paused():
        lines += ["", f"⏸ Пауза {_e(await aq.pause_text())}"]
    lines += ["", "Предложить график: /aqplan 85 · добавить: /aqadd 85 19:00 ср,вс Почистить губку",
              "Удалить: /aqdel номер · время: /aqsettime номер 20:00"]
    await _html(message, "\n".join(lines))


async def show_stats(message: Message, app: App) -> None:
    await _html(message, await _aq(app).stats_text(_aq(app).now().date()))


async def show_tank(message: Message, app: App, tank: Tank | None = None) -> None:
    aq, brain = _aq(app), app.assistant.aquarium_brain
    tanks = [tank] if tank else await aq.tanks()
    lines = ["🐠 <b>Что я знаю об аквариумах</b>"]
    for t in tanks:
        facts = [f for f in await brain.facts(t.id) if f.tank_id == t.id]
        lines += ["", f"<b>{_e(t.label)}</b>"]
        for topic, (title, _) in TOPICS.items():
            items = [f for f in facts if f.topic == topic]
            if items:
                lines.append(f"{title}: " + "; ".join(f"<code>{f.id}</code> {_e(f.text)}" for f in items))
        missing = [TOPICS[k][0] for k in TOPICS if not any(f.topic == k for f in facts)]
        if missing:
            lines.append("<i>Ещё не знаю: " + ", ".join(missing) + "</i>")
    common = [f for f in await brain.facts() if f.tank_id is None]
    if common:
        lines += ["", "<b>Общее</b>"] + [f"<code>{f.id}</code> {_e(f.text)}" for f in common]
    lines.append("\nРассказать: /tank add 85 живут 10 неонов · удалить: /tank del номер")
    await _html(message, "\n".join(lines))


async def show_water_menu(message: Message, app: App) -> None:
    tanks = await _aq(app).tanks()
    lines = ["💧 <b>Тесты воды</b>", ""]
    for t in tanks:
        last = await app.assistant.aquarium_brain.water_history(t.id, 1)
        when = min((v[0][0][:10] for v in last.values()), default=None) if last else None
        lines.append(f"{_e(t.label)}: " + (f"последний тест {when} — /water {t.volume:g}" if when else "тестов нет"))
    lines += ["", "Записать: /water 85 pH 7.2 NO2 0 NO3 20 KH 6 GH 8 T 25"]
    await _html(message, "\n".join(lines))


async def hide_menu(message: Message, app: App) -> None:
    await message.answer("Меню скрыто. Вернуть: /aq", reply_markup=ReplyKeyboardRemove())


MENU_ACTIONS = {
    MENU_TODAY: show_today, MENU_TOMORROW: show_tomorrow, MENU_OVERDUE: show_overdue,
    MENU_PLAN: show_schedule, MENU_STATS: show_stats, MENU_TANK: show_tank, MENU_WATER: show_water_menu,
    MENU_HIDE: hide_menu,
}


@router.message(F.text.in_(MENU_ITEMS), owner_filter)
async def on_menu(message: Message, app: App) -> None:
    await MENU_ACTIONS[message.text](message, app)


# ---------------------------------------------------------------- кнопки под задачами


# Кто сейчас пишет причину «не сделаю»: user_id -> (дата, задача)
aq_pending: dict[int, tuple[str, str]] = {}


@router.callback_query(F.data.startswith(CB), owner_filter)
async def on_task_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    parts = callback.data.removeprefix(CB).split(":")
    msg = callback.message
    if len(parts) not in (3, 4) or not isinstance(msg, Message):
        await callback.answer()
        return
    action, date, key = parts[:3]
    extra = parts[3] if len(parts) == 4 else None
    aq = _aq(app)
    row = await aq.get_task(date, key)
    if row is None:
        await callback.answer("Эта задача уже не найдена", show_alert=True)
        return
    name = _e(row["task_name"])

    if action == "done":
        await callback.answer("👍")
        row, _ = await aq.complete(date, key, turn.user_id, turn.author)
        if row["completed_at"]:
            await msg.edit_text(f"✅ <b>Сделано</b> — {hhmm(row['completed_at'])}\n\n{name}", parse_mode=ParseMode.HTML)
        else:
            await msg.edit_text(f"🚫 {name}\n\nУже отмечено: {_e(row['cant_reason'])}", parse_mode=ParseMode.HTML)
    elif action == "snooze":
        row, ok = await aq.snooze(date, key)
        if not ok:
            closed = row["completed_at"] or row["cant_at"]
            await callback.answer("Задача уже закрыта." if closed
                                  else f"Больше откладывать нельзя (максимум {MAX_SNOOZES} раза).", show_alert=True)
            return
        await callback.answer("Отложено")
        await msg.edit_text(f"⏰ <b>Отложено</b>\n\n{name}\n\nНапомню в {hhmm(row['remind_at'])}. "
                            f"Можно отложить ещё: {MAX_SNOOZES - row['snooze_count']} р.", parse_mode=ParseMode.HTML)
    elif action == "cant":
        await callback.answer()
        await msg.edit_reply_markup(reply_markup=reasons_keyboard(date, key))
    elif action == "back":
        await callback.answer()
        await msg.edit_reply_markup(reply_markup=task_keyboard(date, key, row["snooze_count"]))
    elif action == "why":
        await callback.answer()
        if extra == "other":
            aq_pending[turn.user_id] = (date, key)
            await msg.edit_text(f"✍️ <b>Напиши причину одним сообщением</b>\n\n{name}", parse_mode=ParseMode.HTML)
            return
        reason = CANT_REASONS.get(extra, "Без причины")
        row, ok = await aq.cant(date, key, turn.author, reason)
        await msg.edit_text(f"🚫 {name}\n\nПричина: {_e(reason)}" if ok else "Задача уже закрыта.",
                            parse_mode=ParseMode.HTML)
    else:
        await callback.answer()


async def writing_reason(message: Message) -> bool:
    return message.from_user is not None and message.from_user.id in aq_pending


@router.message(F.text & ~F.text.startswith("/"), writing_reason)
async def on_reason(message: Message, app: App, turn: Turn) -> None:
    date, key = aq_pending.pop(turn.user_id)
    reason = message.text.strip()[:300]
    _, ok = await _aq(app).cant(date, key, turn.author, reason)
    await message.answer(f"🚫 Записал: «{reason}». Учту в советах." if ok else "Задача уже закрыта.")


# ---------------------------------------------------------------- аквариумы и график


@router.message(Command("aqtank"), owner_filter)
async def cmd_tank_list(message: Message, command: CommandObject, app: App) -> None:
    aq = _aq(app)
    action, _, rest = (command.args or "").strip().partition(" ")
    if action == "add":
        name, _, volume = rest.strip().rpartition(" ")
        try:
            liters = float(volume.lower().removesuffix("л").replace(",", "."))
        except ValueError:
            await message.answer("Использование: /aqtank add Креветочник 20")
            return
        tank = await aq.add_tank(name.strip() or f"{liters:g} л", liters)
        await message.answer(f"🫙 Добавил: {tank.label}. Расскажи о нём (/tank add {liters:g} …) "
                             f"или попроси график: /aqplan {liters:g}")
        return
    if action == "del" and rest.strip().isdigit():
        tank = await aq.tank(int(rest))
        if tank is None:
            await message.answer("Нет такого аквариума, см. /aqtank")
            return
        await app.db._exec("DELETE FROM aq_tanks WHERE id = ?", (tank.id,))
        await app.db._exec("DELETE FROM aq_plan WHERE tank_id = ?", (tank.id,))
        await message.answer(f"🗑 Удалил {tank.label} и его график. История и знания остались.")
        return
    tanks = await aq.tanks()
    lines = ["🫙 <b>Аквариумы</b>", ""]
    lines += [f"<code>{t.id}</code> {_e(t.label)} — задач в графике: {len(await aq.schedule(t.id))}" for t in tanks]
    lines += ["", "Добавить: /aqtank add Креветочник 20 · удалить: /aqtank del номер"]
    await _html(message, "\n".join(lines))


@router.message(Command("aqplan"), owner_filter)
async def cmd_plan(message: Message, command: CommandObject, bot: Bot, app: App, turn: Turn) -> None:
    tank, _, tanks = await split_tank(app, command.args)
    if tank is None:
        await _html(message, tanks_hint(tanks, "/aqplan 85") if tanks else "Сначала добавь аквариум: /aqtank add …")
        return
    status = await message.answer(f"🗂 Составляю график для {tank.label}…")
    await send_plan(bot, app, turn.chat_id, tank)
    await status.delete()


@router.callback_query(F.data.startswith(CB_PLAN), owner_filter)
async def on_plan_button(callback: CallbackQuery, bot: Bot, app: App, turn: Turn) -> None:
    action, _, raw = callback.data.removeprefix(CB_PLAN).partition(":")
    brain, aq = app.assistant.aquarium_brain, _aq(app)
    tank = await aq.tank(int(raw)) if raw.isdigit() else None
    msg = callback.message
    if tank is None or not isinstance(msg, Message):
        await callback.answer("Аквариум не найден", show_alert=True)
        return
    await callback.answer()
    await msg.edit_reply_markup(reply_markup=None)
    if action == "ok":
        items = brain.proposals.pop(tank.id, None)
        if not items:
            await msg.answer("Это предложение устарело — /aqplan ещё раз.")
            return
        count = await aq.replace_schedule(tank.id, items)
        await msg.answer(f"✅ График для {tank.label}: {count} задач. Посмотреть и поправить: /aqschedule")
    elif action == "again":
        await send_plan(bot, app, turn.chat_id, tank)
    else:
        brain.proposals.pop(tank.id, None)
        await msg.answer("👌 Оставил как было.")


@router.message(Command("aqschedule"), owner_filter)
async def cmd_schedule(message: Message, app: App) -> None:
    await show_schedule(message, app)


@router.message(Command("aqadd"), owner_filter)
async def cmd_add(message: Message, command: CommandObject, app: App) -> None:
    usage = ("Использование: /aqadd <аквариум> <ЧЧ:ММ> <дни> <что сделать>\n"
             "Дни: каждый, пн-пт, ср,вс\nПример: /aqadd 85 19:00 ср,вс Почистить губку фильтра")
    tank, rest, tanks = await split_tank(app, command.args)
    parts = rest.split(maxsplit=2)
    if tank is None or len(parts) < 3:
        await _html(message, _e(usage) + ("\n\n" + tanks_hint(tanks, "/aqadd 85 …") if tank is None and tanks else ""))
        return
    time_raw, days_raw, title = parts
    days = parse_days(days_raw)
    try:
        at = datetime.datetime.strptime(time_raw, "%H:%M").time()
    except ValueError:
        at = None
    if at is None or days is None:
        await message.answer(usage)
        return
    lower = title.lower()
    kind = next((k for k, words in (
        ("feed", ("корм",)), ("water", ("подмен", "вод")), ("filter", ("фильтр", "губк")),
        ("light", ("свет", "ламп")), ("air", ("воздух", "компрес")), ("glass", ("стекл",)),
        ("plants", ("растен", "стриж")), ("test", ("тест",)),
    ) if any(w in lower for w in words)), "other")
    item_id = await _aq(app).add_item(tank.id, title, at, days, kind)
    await message.answer(f"✅ {KINDS[kind]} {title} — {at:%H:%M}, {days_text(days)} ({tank.label}). "
                         f"Номер {item_id}.")


@router.message(Command("aqdel"), owner_filter)
async def cmd_del(message: Message, command: CommandObject, app: App) -> None:
    arg = (command.args or "").strip()
    ok = arg.isdigit() and await _aq(app).delete_item(int(arg))
    await message.answer("🗑 Удалил из графика" if ok else "Использование: /aqdel <номер из /aqschedule>")


@router.message(Command("aqsettime"), owner_filter)
async def cmd_settime(message: Message, command: CommandObject, app: App) -> None:
    args = (command.args or "").split()
    try:
        at = datetime.datetime.strptime(args[1], "%H:%M").time() if len(args) == 2 else None
    except ValueError:
        at = None
    if at is None or not args[0].isdigit() or not await _aq(app).set_item_time(int(args[0]), at):
        await message.answer("Использование: /aqsettime <номер из /aqschedule> <ЧЧ:ММ>")
        return
    await message.answer(f"✅ Теперь в {at:%H:%M}. Проверить: /aqschedule")


@router.message(Command("aqpause"), owner_filter)
async def cmd_pause(message: Message, command: CommandObject, app: App) -> None:
    arg = (command.args or "").strip()
    if arg and (not arg.isdigit() or int(arg) < 1):
        await message.answer("❌ Укажи число дней, например: /aqpause 3\nИли просто /aqpause — до /aqresume")
        return
    aq = _aq(app)
    await aq.pause(int(arg) if arg else None)
    await message.answer(f"⏸ Напоминания на паузе {await aq.pause_text()}. Снять: /aqresume")


@router.message(Command("aqresume"), owner_filter)
async def cmd_resume(message: Message, app: App) -> None:
    aq = _aq(app)
    if await aq.pause_until() is None:
        await message.answer("▶️ Паузы и так нет.")
        return
    await aq.resume()
    await message.answer("▶️ Пауза снята.")


@router.message(Command("aqtest"), owner_filter)
async def cmd_test(message: Message, bot: Bot, app: App) -> None:
    if not await send_test_task(bot, app):
        await message.answer("⚠️ Не получилось отправить тестовую задачу.")


@router.message(Command("aqoverdue_test"), owner_filter)
async def cmd_overdue_test(message: Message, app: App) -> None:
    await _aq(app).insert_overdue_test(_aq(app).now())
    await message.answer(f"🧪 {TEST_TASKS['overdue_test']} создана (будто 2 часа назад). В течение минуты "
                         f"придут напоминание и «просрочено». Список — «{MENU_OVERDUE}».")


@router.message(Command("aqask"), owner_filter)
async def cmd_ask(message: Message, bot: Bot, app: App) -> None:
    if not await ask_question(bot, app, message.from_user.id, force=True):
        await message.answer("⚠️ Не получилось задать вопрос.")


@router.message(Command("aqchart"), owner_filter)
async def cmd_chart(message: Message, app: App) -> None:
    points = await _aq(app).daily_completion(_aq(app).now().date(), 30)
    if not points:
        await message.answer("📊 Данных об уходе пока нет.")
        return
    png = await chart(app, care_chart_code(points))
    if png is None:
        await message.answer("⚠️ Не получилось построить график (нужна песочница: SANDBOX_URL).")
        return
    await message.answer_photo(BufferedInputFile(png, "care.png"), caption="Уход за 30 дней")


# ---------------------------------------------------------------- знания и вода


@router.message(Command("tank"), owner_filter)
async def cmd_tank(message: Message, command: CommandObject, app: App) -> None:
    brain = app.assistant.aquarium_brain
    action, _, rest = (command.args or "").strip().partition(" ")
    if action == "add" and rest.strip():
        tank, text, tanks = await split_tank(app, rest)
        async with app.queue.slot():
            added = await brain.learn(app.llm, app.settings.default_model, text, tanks, source="tank", tank=tank)
        if not added and (fid := await brain.add_fact(tank.id if tank else None, "tank", text, "tank")):
            added = [text] if fid else []
        await message.answer("🧠 Запомнил:\n" + "\n".join(f"• {a}" for a in added) if added
                             else "Это я уже знаю 🙂")
    elif action == "del" and rest.strip().isdigit():
        ok = await brain.delete_fact(int(rest))
        await message.answer("🗑 Забыл" if ok else "Нет такого номера, см. /tank")
    else:
        tank, _, _ = await split_tank(app, command.args)
        await show_tank(message, app, tank if command.args else None)


@router.message(Command("water"), owner_filter)
async def cmd_water(message: Message, command: CommandObject, app: App) -> None:
    brain = app.assistant.aquarium_brain
    tank, rest, tanks = await split_tank(app, command.args)
    if tank is None:
        if not command.args:
            await show_water_menu(message, app)
        else:
            await _html(message, tanks_hint(tanks, "/water 85 pH 7.2 NO2 0"))
        return
    if rest:
        values = parse_water(rest)
        if not values:
            await message.answer("Не понял значения 🤔 Пример: /water 85 pH 7.2 NO2 0 NO3 20 KH 6 GH 8 T 25")
            return
        await brain.add_water(tank.id, values)
        warnings = water_warnings(values)
        lines = [f"💧 {tank.label}: " + ", ".join(f"{WATER_PARAMS[k][0]} {v:g}" for k, v in values.items())]
        if warnings:
            lines += ["", "⚠️ Обрати внимание:"] + [f"• {w}" for w in warnings]
            lines.append("\nСпроси меня, что делать, — например: «нитриты 0.5 в 85 л, что делать?»")
        else:
            lines.append("✅ Всё в норме.")
        await message.answer("\n".join(lines))
        return
    history = await brain.water_history(tank.id, 6)
    if not history:
        await message.answer(f"💧 Для {tank.label} тестов ещё не было. Запиши: /water {tank.volume:g} pH 7.2 NO2 0")
        return
    lines = [f"💧 <b>Вода: {_e(tank.label)}</b> (новые слева)", ""]
    for key, values in history.items():
        name, unit, low, high = WATER_PARAMS[key]
        norm = f"{low:g}–{high:g}" if low is not None else f"≤ {high:g}"
        bad = " ⚠️" if water_warnings({key: values[0][1]}) else ""
        lines.append(f"{name}: " + " ← ".join(f"{v:g}" for _, v in values) + f" {unit} (норма {norm}){bad}")
    await _html(message, "\n".join(lines))
    rows = await brain.water_rows(tank.id)
    if len(rows) >= 2 and (png := await chart(app, water_chart_code(tank, rows))):
        await message.answer_photo(BufferedInputFile(png, "water.png"), caption=f"Вода: {tank.label}, 90 дней")


# ---------------------------------------------------------------- ответы на вопрос дня


@router.callback_query(F.data.startswith(CB_Q), owner_filter)
async def on_question_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    brain = app.assistant.aquarium_brain
    action, _, raw = callback.data.removeprefix(CB_Q).partition(":")
    question = await brain.get_question(int(raw)) if raw.isdigit() else None
    if question is None or question[1] != turn.user_id or question[5] != 0:
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
        await callback.message.answer("👌 Хорошо, спрошу о другом в другой раз.")


async def answering_question(message: Message, app: App) -> bool:
    user = message.from_user
    if user is None or user.id not in app.settings.aquarium_members:
        return False
    brain = app.assistant.aquarium_brain
    if user.id in brain.answering:
        return True
    reply = message.reply_to_message
    return bool(reply and await brain.question_by_msg(user.id, reply.message_id))


@router.message(F.text & ~F.text.startswith("/"), answering_question)
async def on_answer(message: Message, app: App, turn: Turn) -> None:
    brain, aq = app.assistant.aquarium_brain, _aq(app)
    qid = brain.answering.pop(turn.user_id, None)
    if qid is None and message.reply_to_message:
        qid = await brain.question_by_msg(turn.user_id, message.reply_to_message.message_id)
    question = await brain.get_question(qid) if qid else None
    if question is None:
        return
    await brain.close_question(qid)
    tank = await aq.tank(question[2])
    async with app.queue.slot():
        added = await brain.learn(app.llm, app.settings.default_model, message.text, await aq.tanks(),
                                  source="question", question=question[4], tank=tank)
    if added:
        await message.answer("🧠 Запомнил:\n" + "\n".join(f"• {a}" for a in added))
    where = f" про {tank.label}" if tank else ""
    prompt = (f"Ты спросил меня{where}: «{question[4]}». Мой ответ: {message.text}\n"
              "Коротко (2–4 предложения) прокомментируй как аквариумист: всё ли в порядке, и дай один "
              "полезный совет. Если это стоит учесть в графике ухода — скажи, что поменять.")
    await respond(message, app, with_user(turn, mode="aquarium"), prompt, extract_memory=False,
                  allow_tools=False, keyboard=False)

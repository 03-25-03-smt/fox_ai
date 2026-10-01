"""Кухня: /list (общий список покупок), /menu (меню на неделю), /save и /recipes (книга рецептов)."""

import html

from aiogram import F
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from ..app import App
from ..assistant import Turn
from ..formatting import md_to_html, split_markdown
from ..kitchen import (
    INGREDIENTS_PROMPT,
    MENU_PROMPT,
    Kitchen,
    ShopItem,
    parse_ingredients,
    parse_items,
    parse_menu,
    recipe_title,
)
from ..llm import LLMError
from .registry import Routes

router = Routes("kitchen")

CB = "ks:"  # ks:t:<id> отметить · ks:clean · ks:menu · ks:ing:<id рецепта> · ks:rdel:<id>
MEALS = {"1": "ужин", "2": "обед и ужин", "3": "завтрак, обед и ужин"}


def _k(app: App) -> Kitchen:
    return app.assistant.kitchen


def _e(text: str) -> str:
    return html.escape(text)


def list_view(items: list[ShopItem]) -> tuple[str, InlineKeyboardMarkup | None]:
    if not items:
        return "🛒 Список покупок пуст.\nДобавить: /list молоко, хлеб, яйца 10 шт", None
    left = [i for i in items if not i.done]
    lines = [f"🛒 <b>Список покупок</b> — осталось {len(left)} из {len(items)}", ""]
    for i in items:
        qty = f" — {_e(i.qty)}" if i.qty else ""
        lines.append(f"{'✅ <s>' if i.done else '▫️ '}{_e(i.text)}{qty}{'</s>' if i.done else ''}")
    lines.append("\nНажми на покупку, чтобы отметить. Добавить: /list сыр, бананы")
    buttons = [[InlineKeyboardButton(text=("✅ " if i.done else "▫️ ") + f"{i.text} {i.qty}".strip()[:40],
                                     callback_data=f"{CB}t:{i.id}")] for i in items[:40]]
    if any(i.done for i in items):
        buttons.append([InlineKeyboardButton(text="🧹 Убрать купленное", callback_data=f"{CB}clean")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=buttons)


@router.message(Command("list", "buy"))
async def cmd_list(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    args = (command.args or "").strip()
    kitchen = _k(app)
    if args.lower() in ("clear", "очистить"):
        count = await kitchen.clear(turn.chat_id)
        await message.answer(f"🧹 Список очищен ({count}).")
        return
    if args:
        added = await kitchen.add(turn.chat_id, parse_items(args), turn.author)
        if not added:
            await message.answer("Это уже есть в списке 🙂")
            return
    text, markup = list_view(await kitchen.items(turn.chat_id))
    await message.answer(text, parse_mode=ParseMode.HTML, reply_markup=markup)


@router.callback_query(F.data.startswith(CB))
async def on_button(callback: CallbackQuery, app: App, turn: Turn) -> None:
    kitchen, msg = _k(app), callback.message
    action, _, arg = callback.data.removeprefix(CB).partition(":")
    if not isinstance(msg, Message):
        await callback.answer()
        return
    if action in ("t", "clean"):
        if action == "t" and arg.isdigit():
            await kitchen.toggle(turn.chat_id, int(arg))
        else:
            await kitchen.clear_done(turn.chat_id)
        await callback.answer()
        text, markup = list_view(await kitchen.items(turn.chat_id))
        await msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)
    elif action == "menu":
        items = kitchen.menus.pop(turn.chat_id, None)
        if not items:
            await callback.answer("Это меню устарело — /menu ещё раз", show_alert=True)
            return
        added = await kitchen.add(turn.chat_id, items, turn.author)
        await callback.answer(f"Добавлено: {added}")
        await msg.edit_reply_markup(reply_markup=None)
        await msg.answer(f"🛒 В список покупок добавлено {added}. Открыть: /list")
    elif action == "ing" and arg.isdigit():
        recipe = await kitchen.recipe(turn.chat_id, int(arg))
        if recipe is None:
            await callback.answer("Рецепта уже нет", show_alert=True)
            return
        await callback.answer("Разбираю ингредиенты…")
        try:
            async with app.queue.slot():
                raw = await app.llm.chat(app.settings.default_model, [
                    {"role": "user", "content": INGREDIENTS_PROMPT.format(text=recipe.text[:6000])},
                ], json_mode=True, options={"temperature": 0})
        except LLMError as exc:
            await msg.answer(f"⚠️ {exc}")
            return
        items = parse_ingredients(raw)
        added = await kitchen.add(turn.chat_id, items, turn.author)
        await msg.answer(f"🛒 Из «{recipe.title}» добавлено в список: {added}. /list" if items
                         else "Не нашёл ингредиентов в рецепте 🤔")
    elif action == "rdel" and arg.isdigit():
        ok = await kitchen.delete_recipe(turn.chat_id, int(arg))
        await callback.answer("Удалено" if ok else "Уже удалён")
        await msg.edit_reply_markup(reply_markup=None)
    else:
        await callback.answer()


# ---------------------------------------------------------------- меню на неделю


@router.message(Command("menu"))
async def cmd_menu(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    """/menu [дней] [пожелания]: «/menu 5 без свинины, бюджетно, на двоих»."""
    words = (command.args or "").split(maxsplit=1)
    days = int(words[0]) if words and words[0].isdigit() and 1 <= int(words[0]) <= 7 else 7
    wishes = words[1] if words and words[0].isdigit() else (command.args or "")
    people = "компании друзей" if turn.is_group else "одного человека (студента)"
    status = await message.answer(f"🍽 Составляю меню на {days} дн.…")
    prompt = MENU_PROMPT.format(days=days, people=people, meals=MEALS["2"],
                                wishes=f". Пожелания: {wishes}" if wishes.strip() else "")
    try:
        async with app.queue.slot():
            raw = await app.llm.chat(app.settings.default_model, [{"role": "user", "content": prompt}],
                                     json_mode=True, options={"temperature": 0.7})
    except LLMError as exc:
        await status.edit_text(f"⚠️ {exc}")
        return
    plan, shopping = parse_menu(raw)
    if not plan:
        await status.edit_text("⚠️ Модель не справилась с меню, попробуй ещё раз: /menu")
        return
    lines = [f"🍽 <b>Меню на {len(plan)} дн.</b>", ""]
    for d in plan:
        lines.append(f"<b>{_e(d['day'])}</b>")
        lines += [f"  {_e(meal)}: {_e(dish)}" if meal else f"  {_e(dish)}" for meal, dish in d["meals"]]
    if shopping:
        lines += ["", f"🛒 <b>Покупки ({len(shopping)})</b>"]
        lines.append(", ".join(f"{_e(n)}{' ' + _e(q) if q else ''}" for n, q in shopping))
        _k(app).menus[turn.chat_id] = shopping
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Всё в список покупок", callback_data=f"{CB}menu")]]) if shopping else None
    await status.edit_text("\n".join(lines)[:4000], parse_mode=ParseMode.HTML, reply_markup=markup)
    await app.count_usage(turn.user_id)


# ---------------------------------------------------------------- книга рецептов


@router.message(Command("save"))
async def cmd_save(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    """/save [название] — ответом на сообщение с рецептом, или сохранит последний ответ бота.
    /save Название\\nтекст рецепта — сохранить свой."""
    args = (command.args or "").strip()
    title, _, own_text = args.partition("\n")
    reply = message.reply_to_message
    if own_text.strip():
        text = own_text.strip()
    elif reply and (reply.text or reply.caption):
        stored = await app.db.message_by_tg_id(turn.chat_id, reply.message_id)
        text = stored.content if stored else (reply.text or reply.caption)
    else:
        last = [m for m in await app.db.last_messages(turn.chat_id, 4) if m.role == "assistant"]
        if not last:
            await message.answer("📒 Ответь /save на сообщение с рецептом или спроси меня рецепт и напиши /save.")
            return
        text = last[-1].content
    recipe_id = await _k(app).save_recipe(turn.chat_id, title.strip() or recipe_title(text), text, turn.author)
    recipe = await _k(app).recipe(turn.chat_id, recipe_id)
    await message.answer(f"📒 Сохранил в книгу рецептов: <b>{_e(recipe.title)}</b> (/recipe {recipe_id})",
                         parse_mode=ParseMode.HTML)


@router.message(Command("recipes"))
async def cmd_recipes(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    query = (command.args or "").strip()
    recipes = await _k(app).recipes(turn.chat_id, query)
    if not recipes:
        await message.answer("📒 Ничего не нашёл." if query else
                             "📒 Книга рецептов пуста. Спроси меня рецепт и напиши /save.")
        return
    lines = [f"📒 <b>Книга рецептов</b> ({len(recipes)})", ""]
    lines += [f"<code>{r.id}</code> {_e(r.title)}" for r in recipes[:80]]
    lines.append("\nОткрыть: /recipe номер · найти: /recipes курица")
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("recipe"))
async def cmd_recipe(message: Message, command: CommandObject, app: App, turn: Turn) -> None:
    arg = (command.args or "").strip()
    recipe = await _k(app).recipe(turn.chat_id, int(arg)) if arg.isdigit() else None
    if recipe is None:
        await message.answer("Использование: /recipe <номер из /recipes>")
        return
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🛒 Ингредиенты в список", callback_data=f"{CB}ing:{recipe.id}"),
        InlineKeyboardButton(text="🗑", callback_data=f"{CB}rdel:{recipe.id}"),
    ]])
    chunks = split_markdown(f"📒 **{recipe.title}**\n\n{recipe.text}")
    for i, chunk in enumerate(chunks):
        await message.answer(md_to_html(chunk), parse_mode=ParseMode.HTML,
                             reply_markup=markup if i == len(chunks) - 1 else None)

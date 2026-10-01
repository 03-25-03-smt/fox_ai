"""Кухня: список покупок, меню на неделю, книга рецептов."""

import json

from aiogram.methods import EditMessageText, SendMessage

from bot.kitchen import parse_items, parse_menu, recipe_title

from .conftest import ADMIN, FRIEND, GROUP

MENU = {
    "days": [{"day": "Пн", "meals": [{"meal": "обед", "dish": "Паста с курицей"}]},
             {"day": "Вт", "meals": [{"meal": "ужин", "dish": "Гречка с грибами"}]}],
    "shopping": [{"item": "куриное филе", "qty": "600 г"}, {"item": "гречка", "qty": "500 г"}, "молоко 1 л"],
}


def test_parse_items():
    assert parse_items("молоко 2 л, хлеб; яйца x10\n сыр 200 г") == [
        ("молоко", "2 л"), ("хлеб", ""), ("яйца", "10"), ("сыр", "200 г")]
    assert parse_items("  ,, ") == []


def test_parse_menu_and_title():
    days, shopping = parse_menu(json.dumps(MENU))
    assert [d["day"] for d in days] == ["Пн", "Вт"] and days[0]["meals"] == [("обед", "Паста с курицей")]
    assert shopping == [("куриное филе", "600 г"), ("гречка", "500 г"), ("молоко", "1 л")]
    assert parse_menu("мусор") == ([], [])
    assert recipe_title("## Борщ по-домашнему\nСвёкла…") == "Борщ по-домашнему"
    assert recipe_title("**Блины**\n...") == "Блины"


async def test_shopping_list_flow(env):
    await env.send(ADMIN, "/list молоко 2 л, хлеб")
    await env.send(ADMIN, "/list хлеб, сыр")  # хлеб уже есть
    items = await env.assistant.kitchen.items(ADMIN)
    assert [(i.text, i.qty) for i in items] == [("молоко", "2 л"), ("хлеб", ""), ("сыр", "")]
    await env.click(ADMIN, f"ks:t:{items[0].id}")
    edit = env.session.of_type(EditMessageText)[-1]
    assert "осталось 2 из 3" in edit.text and "<s>молоко" in edit.text
    await env.click(ADMIN, "ks:clean")
    assert [i.text for i in await env.assistant.kitchen.items(ADMIN)] == ["хлеб", "сыр"]
    await env.send(FRIEND, "/list")  # у друга свой список
    assert "пуст" in env.last_text()


async def test_group_list_is_shared(env):
    await env.db.allow_chat(GROUP, "g", ADMIN)
    await env.send(ADMIN, "/list пиво", chat_id=GROUP)
    await env.send(FRIEND, "/list чипсы", chat_id=GROUP)
    assert [i.text for i in await env.assistant.kitchen.items(GROUP)] == ["пиво", "чипсы"]


async def test_menu_to_shopping_list(env):
    env.llm.facts_json = json.dumps(MENU)
    await env.send(ADMIN, "/menu 2 без свинины")
    prompt = env.llm.chat_calls[-1]["messages"][-1]["content"]
    assert "2 дн." in prompt and "без свинины" in prompt
    text = env.last_text()
    assert "Паста с курицей" in text and "куриное филе 600 г" in text
    await env.click(ADMIN, "ks:menu")
    assert [i.text for i in await env.assistant.kitchen.items(ADMIN)] == ["куриное филе", "гречка", "молоко"]
    await env.click(ADMIN, "ks:menu")  # второй раз — уже устарело
    assert len(await env.assistant.kitchen.items(ADMIN)) == 3


async def test_save_recipe_and_ingredients(env):
    env.llm.reply = "## Блины\n\nМука 200 г, молоко 500 мл, 2 яйца. Смешать и жарить."
    await env.send(ADMIN, "дай рецепт блинов")
    await env.send(ADMIN, "/save")
    assert "Блины" in env.last_text()
    (recipe,) = await env.assistant.kitchen.recipes(ADMIN)
    assert "Мука 200 г" in recipe.text
    await env.send(ADMIN, "/save Мой салат\nОгурцы, помидоры, сметана")
    assert [r.title for r in await env.assistant.kitchen.recipes(ADMIN, "огурц")] == ["Мой салат"]
    await env.send(ADMIN, "/recipes")
    assert "Блины" in env.last_text() and "Мой салат" in env.last_text()

    env.llm.facts_json = json.dumps({"ingredients": [{"item": "мука", "qty": "200 г"}, {"item": "яйца", "qty": "2"}]})
    await env.send(ADMIN, f"/recipe {recipe.id}")
    assert "Смешать и жарить" in env.session.of_type(SendMessage)[-1].text
    await env.click(ADMIN, f"ks:ing:{recipe.id}")
    assert [i.text for i in await env.assistant.kitchen.items(ADMIN)] == ["мука", "яйца"]
    await env.click(ADMIN, f"ks:rdel:{recipe.id}")
    assert len(await env.assistant.kitchen.recipes(ADMIN)) == 1

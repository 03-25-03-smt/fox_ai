"""Кухня: общий список покупок, меню на неделю, книга рецептов.

Всё хранится по чату: в группе с друзьями список и книга общие, в личке — свои.
"""

import json
import re
from dataclasses import dataclass
from typing import Any

from .db import Database

MAX_ITEMS = 100
MAX_RECIPE_CHARS = 8000

# «молоко 2 л, хлеб; яйца x10» -> отдельные пункты
_SPLIT_RE = re.compile(r"[,;\n]+")
_QTY_RE = re.compile(
    r"^(?P<name>.+?)\s+(?P<qty>(?:x|×)?\s*\d+(?:[.,]\d+)?\s*(?:шт|кг|г|гр|л|мл|уп|пачк\w*|бут\w*|бан\w*)?\.?)$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class ShopItem:
    id: int
    text: str
    qty: str
    done: bool


@dataclass(frozen=True)
class Recipe:
    id: int
    title: str
    text: str
    added_by: str


def parse_items(text: str) -> list[tuple[str, str]]:
    """Строка с покупками -> [(название, количество)]."""
    out = []
    for part in _SPLIT_RE.split(text):
        part = part.strip(" .-•*")
        if not part:
            continue
        m = _QTY_RE.match(part)
        name, qty = (m.group("name"), m.group("qty").replace("x", "").replace("×", "").strip()) if m else (part, "")
        out.append((name.strip()[:80], qty[:20]))
    return out


def recipe_title(text: str) -> str:
    """Название рецепта: markdown-заголовок, жирная строка или первая строка."""
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        m = re.match(r"^(?:#+\s*|\*\*)(.+?)(?:\*\*)?$", line)
        title = (m.group(1) if m else line).strip(" *#:")
        return title[:60] or "Рецепт"
    return "Рецепт"


def _json(raw: str) -> dict[str, Any]:
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def parse_menu(raw: str) -> tuple[list[dict[str, Any]], list[tuple[str, str]]]:
    """Ответ модели -> (дни [{day, meals: [(приём, блюдо)]}], покупки [(что, сколько)])."""
    data = _json(raw)
    days = []
    for d in data.get("days") or []:
        if not isinstance(d, dict):
            continue
        meals = [(str(m.get("meal") or "").strip(), str(m.get("dish") or "").strip())
                 for m in d.get("meals") or [] if isinstance(m, dict) and m.get("dish")]
        if meals:
            days.append({"day": str(d.get("day") or "").strip()[:20], "meals": meals[:5]})
    shopping = []
    for it in data.get("shopping") or []:
        if isinstance(it, dict) and str(it.get("item") or "").strip():
            shopping.append((str(it["item"]).strip()[:80], str(it.get("qty") or "").strip()[:20]))
        elif isinstance(it, str) and it.strip():
            shopping.extend(parse_items(it))
    return days[:7], shopping[:60]


def parse_ingredients(raw: str) -> list[tuple[str, str]]:
    data = _json(raw)
    out = []
    for it in data.get("ingredients") or []:
        if isinstance(it, dict) and str(it.get("item") or "").strip():
            out.append((str(it["item"]).strip()[:80], str(it.get("qty") or "").strip()[:20]))
    return out[:40]


MENU_PROMPT = (
    "Составь меню на {days} дн. для {people}{wishes}. Блюда простые, из доступных в Европе продуктов, "
    "без повторов подряд; остатки одного блюда можно использовать на следующий день. Приёмы пищи: "
    "{meals}. Затем — общий список покупок на всё меню с количеством (сложи одинаковые продукты, "
    "не включай соль, перец, воду и масло, если их нужно немного).\n"
    'Только JSON: {{"days": [{{"day": "Пн", "meals": [{{"meal": "обед", "dish": "Паста с курицей"}}]}}], '
    '"shopping": [{{"item": "куриное филе", "qty": "600 г"}}]}}'
)
INGREDIENTS_PROMPT = (
    "Выпиши ингредиенты рецепта для списка покупок (без соли, перца и воды), с количеством.\n"
    'Только JSON: {{"ingredients": [{{"item": "мука", "qty": "200 г"}}]}}\n\nРецепт:\n{text}'
)


class Kitchen:
    def __init__(self, db: Database) -> None:
        self.db = db
        self.menus: dict[int, list[tuple[str, str]]] = {}  # chat_id -> покупки из последнего меню

    # ------------------------------------------------------------ список покупок

    async def items(self, chat_id: int) -> list[ShopItem]:
        rows = await self.db._fetchall(
            "SELECT id, text, qty, done FROM shop_items WHERE chat_id = ? ORDER BY done, id", (chat_id,)
        )
        return [ShopItem(i, t, q, bool(d)) for i, t, q, d in rows]

    async def add(self, chat_id: int, items: list[tuple[str, str]], added_by: str = "") -> int:
        """Добавляет покупки; уже стоящие в списке (не купленные) не дублирует. Возвращает, сколько добавлено."""
        have = {i.text.casefold() for i in await self.items(chat_id) if not i.done}
        space = MAX_ITEMS - len(await self.items(chat_id))
        added = 0
        for name, qty in items:
            if name.casefold() in have or added >= space:
                continue
            await self.db._exec(
                "INSERT INTO shop_items (chat_id, text, qty, added_by) VALUES (?, ?, ?, ?)",
                (chat_id, name, qty, added_by),
            )
            have.add(name.casefold())
            added += 1
        return added

    async def toggle(self, chat_id: int, item_id: int) -> bool:
        cur = await self.db._exec(
            "UPDATE shop_items SET done = 1 - done WHERE chat_id = ? AND id = ?", (chat_id, item_id)
        )
        return cur.rowcount > 0

    async def clear_done(self, chat_id: int) -> int:
        cur = await self.db._exec("DELETE FROM shop_items WHERE chat_id = ? AND done = 1", (chat_id,))
        return cur.rowcount

    async def clear(self, chat_id: int) -> int:
        cur = await self.db._exec("DELETE FROM shop_items WHERE chat_id = ?", (chat_id,))
        return cur.rowcount

    # ------------------------------------------------------------ рецепты

    async def save_recipe(self, chat_id: int, title: str, text: str, added_by: str = "") -> int:
        cur = await self.db._exec(
            "INSERT INTO recipes (chat_id, title, text, added_by) VALUES (?, ?, ?, ?)",
            (chat_id, title[:60], text[:MAX_RECIPE_CHARS], added_by),
        )
        return int(cur.lastrowid)

    async def recipes(self, chat_id: int, query: str = "") -> list[Recipe]:
        sql, params = "SELECT id, title, text, added_by FROM recipes WHERE chat_id = ?", [chat_id]
        rows = await self.db._fetchall(sql + " ORDER BY title COLLATE NOCASE, id", tuple(params))
        recipes = [Recipe(*r) for r in rows]
        if query:
            q = query.casefold()  # SQLite lower() не умеет кириллицу — фильтруем в Python
            recipes = [r for r in recipes if q in r.title.casefold() or q in r.text.casefold()]
        return recipes

    async def recipe(self, chat_id: int, recipe_id: int) -> Recipe | None:
        row = await self.db._fetchone(
            "SELECT id, title, text, added_by FROM recipes WHERE chat_id = ? AND id = ?", (chat_id, recipe_id)
        )
        return Recipe(*row) if row else None

    async def delete_recipe(self, chat_id: int, recipe_id: int) -> bool:
        cur = await self.db._exec("DELETE FROM recipes WHERE chat_id = ? AND id = ?", (chat_id, recipe_id))
        return cur.rowcount > 0

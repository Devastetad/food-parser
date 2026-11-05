"""Список покупок: чего не хватает на план с учётом запасов.

Блюда проходятся в порядке плана, и запас расходуется так же, как при готовке, поэтому
один и тот же продукт не считается «имеющимся» дважды. Одинаковые ингредиенты из разных
рецептов складываются в одну строку.
"""
import math
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import timedelta

from core.inventory_manager import add_item
from core.plan_manager import get_plan
from core.recipe_manager import load_recipes
from core.stock import ALWAYS_AVAILABLE, EPSILON_GRAMS, Stock
from core.units import CONVERTIBLE, INVENTORY_UNITS, grams_per_unit, to_grams
from database.database import transaction

UNKNOWN_AMOUNT_UNIT = "упаковка"  # продукт «по вкусу» покупается упаковкой


@dataclass
class ShoppingLine:
    ingredient_id: int
    name: str
    category: str | None
    quantity: float | None
    unit: str | None
    recipes: list[str]
    purchased: bool


@dataclass
class _Need:
    name: str
    grams: float = 0.0
    units: Counter = field(default_factory=Counter)
    recipes: list[str] = field(default_factory=list)

    def add_recipe(self, title: str) -> None:
        if title not in self.recipes:
            self.recipes.append(title)


def _collect_needs(conn: sqlite3.Connection, plan_id: int) -> dict[int, _Need]:
    plan = get_plan(conn, plan_id)
    if plan is None:
        return {}
    recipes = load_recipes(conn)
    stock = Stock.load(conn)
    needs: dict[int, _Need] = {}
    for item in plan.items:
        if item.cooked_on:
            continue
        stock.drop_expired(plan.start + timedelta(days=item.day))
        recipe = recipes[item.recipe_id]
        factor = recipe.factor(item.servings)
        for line in recipe.lines:
            if line.name in ALWAYS_AVAILABLE:
                continue
            if line.quantity is None:
                if stock.available_grams(line.ingredient_id) <= EPSILON_GRAMS:
                    need = needs.setdefault(line.ingredient_id, _Need(line.name))
                    need.add_recipe(recipe.title)
                continue
            required = to_grams(line.quantity * factor, line.unit, line.name)
            taken = sum(grams for _, grams in stock.take(line.ingredient_id, required))
            lacking = required - taken
            if lacking > EPSILON_GRAMS:
                need = needs.setdefault(line.ingredient_id, _Need(line.name))
                need.grams += lacking
                need.units[line.unit] += 1
                need.add_recipe(recipe.title)
    return needs


def _amount(need: _Need) -> tuple[float | None, str | None]:
    """Если единицы строк совпадают, показываем в них, иначе в граммах. Штуки — целые."""
    if not need.units:
        return None, None
    unit = next(iter(need.units)) if len(need.units) == 1 else "г"
    quantity = need.grams / grams_per_unit(unit, need.name)
    if unit in CONVERTIBLE:
        return round(quantity, 2), unit
    return float(math.ceil(quantity - 1e-9)), unit


def build_shopping_list(conn: sqlite3.Connection, plan_id: int) -> list[ShoppingLine]:
    needs = _collect_needs(conn, plan_id)
    purchased = {
        row[0]
        for row in conn.execute(
            "SELECT ingredient_id FROM shopping_checks WHERE plan_id = ?", (plan_id,)
        )
    }
    categories = {
        row["id"]: row["category"] for row in conn.execute("SELECT id, category FROM ingredients")
    }
    lines = []
    for ingredient_id, need in needs.items():
        quantity, unit = _amount(need)
        lines.append(
            ShoppingLine(
                ingredient_id, need.name, categories.get(ingredient_id), quantity, unit,
                need.recipes, ingredient_id in purchased,
            )
        )  # fmt: skip
    return sorted(lines, key=lambda line: (line.category or "Другое", line.name))


def set_purchased(
    conn: sqlite3.Connection, plan_id: int, ingredient_id: int, purchased: bool
) -> None:
    if purchased:
        conn.execute(
            "INSERT OR IGNORE INTO shopping_checks (plan_id, ingredient_id) VALUES (?, ?)",
            (plan_id, ingredient_id),
        )
    else:
        conn.execute(
            "DELETE FROM shopping_checks WHERE plan_id = ? AND ingredient_id = ?",
            (plan_id, ingredient_id),
        )


def move_purchased_to_inventory(conn: sqlite3.Connection, plan_id: int) -> int:
    """Переносит купленное в инвентарь. После этого строки исчезают сами: запас покрыл нужду."""
    moved = 0
    lines = [line for line in build_shopping_list(conn, plan_id) if line.purchased]
    with transaction(conn):
        for line in lines:
            quantity, unit = line.quantity, line.unit
            if quantity is None:
                quantity, unit = 1, UNKNOWN_AMOUNT_UNIT
            elif unit not in INVENTORY_UNITS:
                quantity, unit = to_grams(quantity, unit, line.name), "г"
            add_item(conn, line.name, quantity, unit)
            moved += 1
        conn.execute("DELETE FROM shopping_checks WHERE plan_id = ?", (plan_id,))
    return moved

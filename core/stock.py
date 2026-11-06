"""Запасы для расчётов: хватит ли продуктов на рецепт и что списать.

Всё считается в граммах, поэтому «2 шт лука» и «300 г лука» сравнимы. Партии расходуются
в порядке срока годности: первым уходит то, что испортится раньше.
"""
import re
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from enum import Enum

from core.ingredient_matcher import GENERIC_INGREDIENTS, find_ingredient, generic_children
from core.recipe_manager import Recipe, RecipeLine
from core.units import grams_per_unit, to_grams

# Этого не надо покупать и хранить в инвентаре
ALWAYS_AVAILABLE = {"Вода"}
EPSILON_GRAMS = 0.01
SEASONING_RE = re.compile(
    r"соль|перец|перца|зелень|петрушк|укроп|кинз|базилик|лавров|специ|приправ|пряност|кориандр|"
    r"тмин|гвоздик|мускат|паприк|карри|орегано|тимьян|майоран|чабер|розмарин|корица|ванил|мята",
    re.I,
)


def is_optional(line: RecipeLine) -> bool:
    """Приправа «по вкусу»: без неё блюдо всё равно получится."""
    return line.quantity is None and bool(SEASONING_RE.search(line.name))


class LineStatus(Enum):
    ENOUGH = "enough"
    PARTIAL = "partial"
    MISSING = "missing"


@dataclass
class Batch:
    inventory_id: int
    ingredient_id: int
    name: str
    unit: str
    grams: float
    grams_per_unit: float
    expires_on: date | None

    @property
    def quantity(self) -> float:
        return self.grams / self.grams_per_unit


@dataclass
class LineCheck:
    line: RecipeLine
    status: LineStatus
    missing_quantity: float | None = None  # в единицах строки рецепта
    used_batches: list[str] = None


@dataclass
class Deduction:
    inventory_id: int
    name: str
    unit: str
    available: float
    amount: float


class Stock:
    def __init__(self, batches: list[Batch], children: dict[int, list[int]] | None = None):
        self._children = children or {}
        self._by_ingredient: dict[int, list[Batch]] = {}
        for batch in batches:
            self._by_ingredient.setdefault(batch.ingredient_id, []).append(batch)
        for items in self._by_ingredient.values():
            items.sort(key=lambda batch: (batch.expires_on is None, batch.expires_on or date.max))

    @classmethod
    def load(cls, conn: sqlite3.Connection, item_ids: set[int] | None = None) -> "Stock":
        """Запасы из базы. item_ids ограничивает партии, которые разрешено использовать."""
        rows = conn.execute(
            "SELECT inv.id, inv.ingredient_id, inv.name, inv.quantity, inv.unit, inv.expires_on, "
            "ing.name AS ingredient_name FROM inventory inv "
            "JOIN ingredients ing ON ing.id = inv.ingredient_id WHERE inv.finished_on IS NULL"
        ).fetchall()
        batches = [
            Batch(
                inventory_id=row["id"],
                ingredient_id=row["ingredient_id"],
                name=row["name"],
                unit=row["unit"],
                grams=to_grams(row["quantity"], row["unit"], row["ingredient_name"]),
                grams_per_unit=grams_per_unit(row["unit"], row["ingredient_name"]),
                expires_on=date.fromisoformat(row["expires_on"]) if row["expires_on"] else None,
            )
            for row in rows
            if item_ids is None or row["id"] in item_ids
        ]
        return cls(batches, generic_children(conn))

    def copy(self) -> "Stock":
        """Отдельная копия для примерки плана: настоящие запасы не меняются."""
        return deepcopy(self)

    def drop_expired(self, on_date: date) -> None:
        """Продукт, испортившийся к дате блюда, использовать нельзя."""
        for ingredient_id, items in self._by_ingredient.items():
            self._by_ingredient[ingredient_id] = [
                batch for batch in items if batch.expires_on is None or batch.expires_on >= on_date
            ]

    def _batches_for(self, ingredient_id: int) -> list[Batch]:
        ids = [ingredient_id, *self._children.get(ingredient_id, [])]
        batches = [batch for item in ids for batch in self._by_ingredient.get(item, [])]
        return sorted(batches, key=lambda batch: (batch.expires_on is None, batch.expires_on or date.max))

    def available_grams(self, ingredient_id: int) -> float:
        return sum(batch.grams for batch in self._batches_for(ingredient_id))

    def earliest_expiry(self, ingredient_id: int) -> date | None:
        dates = [
            batch.expires_on
            for batch in self._batches_for(ingredient_id)
            if batch.grams > EPSILON_GRAMS and batch.expires_on
        ]
        return min(dates) if dates else None

    def take(self, ingredient_id: int, grams: float) -> list[tuple[Batch, float]]:
        """Расходует запас. Возвращает партии и сколько граммов взято из каждой."""
        taken = []
        for batch in self._batches_for(ingredient_id):
            if grams <= EPSILON_GRAMS:
                break
            amount = min(batch.grams, grams)
            if amount > 0:
                batch.grams -= amount
                grams -= amount
                taken.append((batch, amount))
        return taken

    def check_line(self, line: RecipeLine, factor: float = 1) -> LineCheck:
        """Проверяет одну строку рецепта с учётом количества, а не только наличия."""
        if line.name in ALWAYS_AVAILABLE:
            return LineCheck(line, LineStatus.ENOUGH)
        available = self.available_grams(line.ingredient_id)
        if line.quantity is None:  # «по вкусу»: достаточно, чтобы продукт был
            status = LineStatus.ENOUGH if available > EPSILON_GRAMS else LineStatus.MISSING
            return LineCheck(line, status)
        needed = to_grams(line.quantity * factor, line.unit, line.name)
        if available + EPSILON_GRAMS >= needed:
            return LineCheck(line, LineStatus.ENOUGH)
        missing = (needed - available) / grams_per_unit(line.unit, line.name)
        status = LineStatus.PARTIAL if available > EPSILON_GRAMS else LineStatus.MISSING
        return LineCheck(line, status, missing)

    def check_recipe(self, recipe: Recipe, factor: float = 1) -> list[LineCheck]:
        return [self.check_line(line, factor) for line in recipe.lines]

    def plan_deduction(self, recipe: Recipe, servings: float) -> list[Deduction]:
        """Что списать за приготовленное блюдо. Строки «по вкусу» не списываются."""
        stock = self.copy()
        factor = recipe.factor(servings)
        totals: dict[int, Deduction] = {}
        for line in recipe.lines:
            if line.quantity is None or line.name in ALWAYS_AVAILABLE:
                continue
            needed = to_grams(line.quantity * factor, line.unit, line.name)
            for batch, grams in stock.take(line.ingredient_id, needed):
                amount = grams / batch.grams_per_unit
                if batch.inventory_id in totals:
                    totals[batch.inventory_id].amount += amount
                else:
                    original = next(
                        item
                        for item in self._batches_for(line.ingredient_id)
                        if item.inventory_id == batch.inventory_id
                    )
                    totals[batch.inventory_id] = Deduction(
                        batch.inventory_id, batch.name, batch.unit, original.quantity, amount
                    )
        return list(totals.values())


def find_substitutes(conn: sqlite3.Connection, stock: Stock, line: RecipeLine) -> list[str]:
    """Чем заменить продукт из имеющегося: вариант из рецепта («или сметану») либо продукт
    той же группы (сливочное масло вместо растительного)."""
    candidates = []
    if line.note and "или " in line.note:
        text = line.note.split("или ", 1)[1]
        candidates.append(text.split(";")[0].split(",")[0].strip())
    for products in GENERIC_INGREDIENTS.values():
        if line.name in products:
            candidates += [product for product in products if product != line.name]
    found = []
    for name in candidates:
        ingredient_id = find_ingredient(conn, name)
        if ingredient_id is None or ingredient_id == line.ingredient_id:
            continue
        if stock.available_grams(ingredient_id) > EPSILON_GRAMS and name not in found:
            found.append(name)
    return found

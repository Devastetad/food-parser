"""Перенос сырых рецептов в основную базу."""
import json
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from pathlib import Path

import config
from core.ingredient_matcher import IngredientMatcher
from database.database import connect, transaction
from parser import normalizer
from parser.duplicate_checker import DuplicateIndex


UNTIMED_STEP_MINUTES = 5
SOURCE_NAMES = {
    "wikibooks_ru": "Викиучебник",
    "wikibooks_en": "Викиучебник на английском, машинный перевод",
    "wikibooks_fr": "Викиучебник на французском, машинный перевод",
    "wikibooks_de": "Викиучебник на немецком, машинный перевод",
    "wikibooks_it": "Викиучебник на итальянском, машинный перевод",
    "wikibooks_es": "Викиучебник на испанском, машинный перевод",
}


@dataclass
class ImportStats:
    imported: int = 0
    duplicates: int = 0
    without_ingredients: int = 0


def import_raw_recipes(db_path: Path = config.DB_PATH) -> ImportStats:
    """Нормализует все сырые рецепты одной транзакцией.

    Запись находится по source_url, её id не меняется при повторном запуске, поэтому
    история приготовления остаётся привязанной к тем же рецептам.
    """
    stats = ImportStats()
    with closing(connect(db_path)) as conn, transaction(conn):
        matcher = IngredientMatcher(conn)
        duplicates = DuplicateIndex()
        equipment_ids = {
            row["name"]: row["id"] for row in conn.execute("SELECT id, name FROM kitchen_equipment")
        }
        rows = conn.execute(
            "SELECT r.license, r.payload, i.source FROM raw_recipes r "
            "JOIN raw_items i ON i.id = r.raw_item_id ORDER BY r.id"
        ).fetchall()
        for row in rows:
            _import_one(conn, json.loads(row["payload"]), row["license"], matcher, duplicates,
                        equipment_ids, stats, SOURCE_NAMES.get(row["source"], row["source"]))  # fmt: skip
        matcher.finish()
        matcher.remove_unused()
    return stats


def _import_one(conn, raw, license_name, matcher, duplicates, equipment_ids, stats, source) -> None:
    ingredients = []
    for line in raw["ingredients"]:
        for parsed in normalizer.parse_ingredient(line["text"]):
            ingredient_id = matcher.resolve(parsed.name, parsed.unit)
            ingredients.append((ingredient_id, parsed, line))
    if not ingredients:
        stats.without_ingredients += 1
        return

    composition = [(item[0], item[1].quantity, item[1].unit) for item in ingredients]
    if not duplicates.add_if_new(raw["title"], composition, raw["steps"]):
        stats.duplicates += 1
        return

    step_minutes = [normalizer.parse_step_minutes(step) for step in raw["steps"]]
    notes = raw["ingredient_notes"]
    # Время иногда записано отдельным пунктом среди продуктов
    lines = [line["text"] for line in raw["ingredients"]]
    minutes = normalizer.parse_minutes(raw["time_text"]) or normalizer.time_from_notes(notes + lines)
    is_estimated = 0
    if minutes is None and any(step_minutes):
        # Шаг без указанного времени тоже занимает время: иначе лепка хинкали стоит 0 минут
        minutes = sum(value or UNTIMED_STEP_MINUTES for value in step_minutes)
        is_estimated = 1

    recipe_id = conn.execute(
        "INSERT INTO recipes (title, description, category, cuisine, cook_time_min, "
        "time_is_estimated, servings, difficulty, image_url, source, source_url, license, "
        "calories) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT (source_url) DO UPDATE SET title = excluded.title, "
        "description = excluded.description, category = excluded.category, "
        "cuisine = excluded.cuisine, cook_time_min = excluded.cook_time_min, "
        "time_is_estimated = excluded.time_is_estimated, servings = excluded.servings, "
        "difficulty = excluded.difficulty, image_url = excluded.image_url, "
        "source = excluded.source, license = excluded.license, calories = excluded.calories "
        "RETURNING id",
        (
            raw["title"],
            raw["description"],
            normalizer.normalize_category(
                raw["category"], raw["title"], [item[1].name for item in ingredients]
            ),
            normalizer.normalize_cuisine(raw["cuisine"]),
            minutes,
            is_estimated,
            normalizer.parse_servings(raw["servings_text"], notes),
            normalizer.parse_difficulty(raw["difficulty_text"]),
            raw["image_url"],
            source,
            raw["source_url"],
            license_name,
            normalizer.parse_calories(raw["calories_text"]),
        ),
    ).fetchone()[0]

    _replace_details(conn, recipe_id, raw, ingredients, step_minutes, equipment_ids)
    _store_nutrition(conn, recipe_id, ingredients, normalizer.parse_servings(raw["servings_text"], notes))
    stats.imported += 1


def _store_nutrition(conn, recipe_id, ingredients, servings) -> None:
    lines = [
        RecipeLine(ingredient_id, parsed.name, parsed.quantity, parsed.unit)
        for ingredient_id, parsed, _ in ingredients
    ]
    from core import nutrition  # nutrition использует RecipeLine из этого модуля

    estimated = nutrition.estimate(lines, servings or config.DEFAULT_SERVINGS)
    if estimated:
        conn.execute(
            "UPDATE recipes SET calories = coalesce(calories, ?), protein_g = ?, fat_g = ?, "
            "carbs_g = ?, nutrition_is_estimated = 1 WHERE id = ?",
            (estimated.calories, estimated.protein_g, estimated.fat_g, estimated.carbs_g, recipe_id),
        )


def _replace_details(
    conn: sqlite3.Connection, recipe_id, raw, ingredients, step_minutes, equipment_ids
) -> None:
    for table in ("recipe_ingredients", "cooking_instructions", "recipe_equipment"):
        conn.execute(f"DELETE FROM {table} WHERE recipe_id = ?", (recipe_id,))
    conn.executemany(
        "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit, raw_text, "
        "note, group_name) VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            (recipe_id, ingredient_id, parsed.quantity, parsed.unit, line["text"], parsed.note,
             line["group"])
            for ingredient_id, parsed, line in ingredients
        ],
    )  # fmt: skip
    conn.executemany(
        "INSERT INTO cooking_instructions (recipe_id, step_number, description, duration_min) "
        "VALUES (?, ?, ?, ?)",
        [
            (recipe_id, number, step, minutes)
            for number, (step, minutes) in enumerate(zip(raw["steps"], step_minutes), start=1)
        ],
    )
    conn.executemany(
        "INSERT INTO recipe_equipment (recipe_id, equipment_id) VALUES (?, ?)",
        [
            (recipe_id, equipment_ids[name])
            for name in normalizer.detect_equipment(raw["steps"])
        ],
    )


def count_recipes(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM recipes").fetchone()[0]


def list_ingredient_names(conn: sqlite3.Connection) -> list[str]:
    """Названия для подсказок: сначала продукты, которые чаще встречаются в рецептах."""
    return [
        row["name"]
        for row in conn.execute(
            "SELECT i.name FROM ingredients i "
            "LEFT JOIN recipe_ingredients r ON r.ingredient_id = i.id "
            "GROUP BY i.id ORDER BY count(r.id) DESC, i.name"
        )
    ]


@dataclass
class RecipeLine:
    ingredient_id: int
    name: str
    quantity: float | None
    unit: str | None
    note: str | None = None
    group: str | None = None


@dataclass
class Step:
    number: int
    text: str
    minutes: int | None = None


@dataclass
class Recipe:
    id: int
    title: str
    category: str | None
    minutes: int | None
    time_is_estimated: bool
    servings: int | None
    difficulty: str | None = None
    description: str | None = None
    cuisine: str | None = None
    image_url: str | None = None
    source: str | None = None
    source_url: str | None = None
    license: str | None = None
    calories: float | None = None
    protein_g: float | None = None
    fat_g: float | None = None
    carbs_g: float | None = None
    lines: list[RecipeLine] = field(default_factory=list)
    steps: list[Step] = field(default_factory=list)
    equipment: list[str] = field(default_factory=list)

    @property
    def base_servings(self) -> int:
        return self.servings or config.DEFAULT_SERVINGS

    def factor(self, servings: float) -> float:
        """Во сколько раз умножить количества, чтобы получить нужное число порций."""
        return servings / self.base_servings


def _recipe_from_row(row: sqlite3.Row) -> Recipe:
    return Recipe(
        id=row["id"],
        title=row["title"],
        category=row["category"],
        minutes=row["cook_time_min"],
        time_is_estimated=bool(row["time_is_estimated"]),
        servings=row["servings"],
        difficulty=row["difficulty"],
        description=row["description"],
        cuisine=row["cuisine"],
        image_url=row["image_url"],
        source=row["source"],
        source_url=row["source_url"],
        license=row["license"],
        calories=row["calories"],
        protein_g=row["protein_g"],
        fat_g=row["fat_g"],
        carbs_g=row["carbs_g"],
    )


def load_recipes(conn: sqlite3.Connection, with_steps: bool = False) -> dict[int, Recipe]:
    """Все рецепты с составом и техникой. Их немного, поэтому фильтры работают в Python:
    заодно поиск не зависит от регистра кириллицы, которого не умеет SQLite."""
    recipes = {
        row["id"]: _recipe_from_row(row)
        for row in conn.execute("SELECT * FROM recipes ORDER BY title")
    }
    for row in conn.execute(
        "SELECT r.*, i.name FROM recipe_ingredients r "
        "JOIN ingredients i ON i.id = r.ingredient_id ORDER BY r.id"
    ):
        if row["recipe_id"] in recipes:
            recipes[row["recipe_id"]].lines.append(_line_from_row(row))
    for row in conn.execute(
        "SELECT r.recipe_id, e.name FROM recipe_equipment r "
        "JOIN kitchen_equipment e ON e.id = r.equipment_id ORDER BY e.id"
    ):
        recipes[row["recipe_id"]].equipment.append(row["name"])
    if with_steps:
        for row in conn.execute(
            "SELECT * FROM cooking_instructions ORDER BY recipe_id, step_number"
        ):
            recipes[row["recipe_id"]].steps.append(_step_from_row(row))
    return recipes


def _line_from_row(row: sqlite3.Row) -> RecipeLine:
    return RecipeLine(
        row["ingredient_id"], row["name"], row["quantity"], row["unit"], row["note"],
        row["group_name"],
    )  # fmt: skip


def _step_from_row(row: sqlite3.Row) -> Step:
    return Step(row["step_number"], row["description"], row["duration_min"])


def get_recipe(conn: sqlite3.Connection, recipe_id: int) -> Recipe | None:
    row = conn.execute("SELECT * FROM recipes WHERE id = ?", (recipe_id,)).fetchone()
    if row is None:
        return None
    recipe = _recipe_from_row(row)
    recipe.lines = [
        _line_from_row(line)
        for line in conn.execute(
            "SELECT r.*, i.name FROM recipe_ingredients r "
            "JOIN ingredients i ON i.id = r.ingredient_id WHERE r.recipe_id = ? ORDER BY r.id",
            (recipe_id,),
        )
    ]
    recipe.steps = [
        _step_from_row(step)
        for step in conn.execute(
            "SELECT * FROM cooking_instructions WHERE recipe_id = ? ORDER BY step_number",
            (recipe_id,),
        )
    ]
    recipe.equipment = [
        item["name"]
        for item in conn.execute(
            "SELECT e.name FROM recipe_equipment r JOIN kitchen_equipment e "
            "ON e.id = r.equipment_id WHERE r.recipe_id = ? ORDER BY e.id",
            (recipe_id,),
        )
    ]
    return recipe


def search_recipes(
    recipes: list[Recipe],
    text: str = "",
    category: str | None = None,
    max_minutes: int | None = None,
    equipment: set[str] | None = None,
) -> list[Recipe]:
    """Фильтрует рецепты. text ищется в названии и в составе. equipment: что есть дома."""
    needle = text.strip().casefold().replace("ё", "е")
    found = []
    for recipe in recipes:
        if category and recipe.category != category:
            continue
        if max_minutes is not None and (recipe.minutes is None or recipe.minutes > max_minutes):
            continue
        if equipment is not None and not set(recipe.equipment) <= equipment:
            continue
        if needle:
            words = [recipe.title, *(line.name for line in recipe.lines)]
            if not any(needle in word.casefold().replace("ё", "е") for word in words):
                continue
        found.append(recipe)
    return found


def list_categories(conn: sqlite3.Connection) -> list[str]:
    return [
        row[0]
        for row in conn.execute(
            "SELECT category FROM recipes WHERE category IS NOT NULL "
            "GROUP BY category ORDER BY count(*) DESC"
        )
    ]

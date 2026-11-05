"""Предпочтения пользователя: хранятся в базе и подставляются в генератор."""
import json
import re
import sqlite3
from dataclasses import asdict, dataclass, field

from core.ingredient_matcher import ingredient_key
from core.recipe_manager import Recipe

DIETS = {
    "vegetarian": "Без мяса и рыбы",
    "no_pork": "Без свинины",
    "no_fish": "Без рыбы и морепродуктов",
    "no_dairy": "Без молочного",
    "no_gluten": "Без муки и хлеба",
    "no_nuts": "Без орехов",
}
MEAT = r"мяс|говяд|свин|баран|телят|кур[иа]|цыпл|индейк|утк|гус[ья]|кролик|колбас|сосиск|ветчин|фарш|бекон|сало|смалец|печен[ьк]|бульон"
FISH = r"рыб|кревет|кальмар|лосос|сёмг|семг|сельд|треск|анчоус|икр|мид[ий]|краб|омар|судак|щук|карп"
DIET_RULES = {
    "vegetarian": f"{MEAT}|{FISH}",
    "no_pork": r"свин|бекон|ветчин|сало|смалец|шпик",
    "no_fish": FISH,
    "no_dairy": r"молок|сливк|сливоч|сметан|творог|сыр|кефир|йогурт|брынз",
    "no_gluten": r"мук|хлеб|макарон|лапш|сухар|манн|тесто|батон",
    "no_nuts": r"орех|миндал|фундук|арахис|кешью|фисташ",
}
DIET_PATTERNS = {name: re.compile(rule, re.I) for name, rule in DIET_RULES.items()}


@dataclass
class Preferences:
    people: int = 2
    meals_per_day: int = 3
    max_minutes: int | None = 60
    diets: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    target_kcal: int | None = None


def load_preferences(conn: sqlite3.Connection) -> Preferences:
    row = conn.execute("SELECT value FROM user_preferences WHERE key = 'main'").fetchone()
    if row is None:
        return Preferences()
    known = Preferences.__dataclass_fields__
    return Preferences(**{key: value for key, value in json.loads(row[0]).items() if key in known})


def save_preferences(conn: sqlite3.Connection, preferences: Preferences) -> None:
    conn.execute(
        "INSERT INTO user_preferences (key, value) VALUES ('main', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        (json.dumps(asdict(preferences), ensure_ascii=False),),
    )


def violates_diet(recipe: Recipe, diets: list[str]) -> bool:
    return any(
        DIET_PATTERNS[diet].search(line.name) for diet in diets for line in recipe.lines
    )


def has_excluded(recipe: Recipe, excluded: list[str]) -> bool:
    """«грибы» исключает и «грибы белые»: сравниваем слова, а не строки целиком."""
    keys = [set(ingredient_key(name).split()) for name in excluded if name.strip()]
    lines = [set(ingredient_key(line.name).split()) for line in recipe.lines]
    return any(key <= line for key in keys for line in lines)

"""Генератор рациона.

Слоты заполняются по порядку. Для каждого берётся рецепт с лучшей оценкой, а его продукты
сразу списываются с копии запасов, поэтому килограмм курицы распределится между блюдами
и не будет посчитан дважды. Жёсткие условия (техника, время, диета, режим «только новые»)
отсекают рецепт полностью, мягкие (запасы, сроки, разнообразие, оценки) меняют только баллы.
"""
import random
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta

from core.history_manager import CookedSummary, cooked_recipe_ids, cooked_summary
from core.preferences import has_excluded, violates_diet
from core.recipe_manager import Recipe, load_recipes
from core.stock import ALWAYS_AVAILABLE, LineStatus, Stock, is_optional
from core.units import to_grams

MIXED, NEW_ONLY = "mixed", "new"

MEALS = {"breakfast": "Завтрак", "lunch": "Обед", "dinner": "Ужин", "snack": "Перекус"}
SLOTS_BY_COUNT = {
    1: ["dinner"],
    2: ["lunch", "dinner"],
    3: ["breakfast", "lunch", "dinner"],
    4: ["breakfast", "lunch", "snack", "dinner"],
    5: ["breakfast", "snack", "lunch", "snack", "dinner"],
}
MEAL_CATEGORIES = {
    "breakfast": {"Каши"},
    "lunch": {"Супы", "Вторые блюда"},
    "dinner": {"Вторые блюда", "Салаты"},
    "snack": {"Закуски", "Салаты", "Десерты", "Выпечка"},
}
MEAL_SHARE = {"breakfast": 0.25, "lunch": 0.35, "dinner": 0.3, "snack": 0.1}
BREAKFAST_RE = re.compile(
    r"омлет|яичниц|яйц|яич|блин|олад|сырник|творог|творож|запеканк|каш[аи]|бутерброд|тост|"
    r"гренк|кекс|маффин|вафл|овсян|мюсли|пудинг|булоч",
    re.I,
)

# Заготовки и ингредиенты, а не блюда: их нельзя подать как приём пищи
NOT_A_MEAL_RE = re.compile(
    r"\b(?:масло|бульон|маринад|тесто|закваск|заправк|соус|приправ|смесь|глазур|крем|начинк|"
    r"фарш|основ[аы]|сироп|пюре томат|дюксель|фритюр|обертк)",
    re.I,
)
FAMILY_SUFFIX_RE = re.compile(r"(?:\s+\d+)?(?:\s*\(вариант \d+\))?$")
NOT_FOR_BREAKFAST_RE = re.compile(r"морожен|желе|торт|пирожн", re.I)

# Примерные цены, руб. за кг: нужны только для ограничения бюджета, в интерфейсе — «около»
PRICE_PER_KG = {
    "Мясо и птица": 550, "Рыба": 650, "Овощи": 90, "Зелень и пряности": 500,
    "Молочное и яйца": 300, "Бакалея": 120,
}  # fmt: skip
DEFAULT_PRICE_PER_KG = 300

MIN_LINES = 2
REPEAT_GAP_DAYS = 14
STOCK_ONLY_REPEAT_GAP_DAYS = 3  # из малого набора продуктов выходит мало блюд
NOT_MEAL_CATEGORIES = {"Соусы", "Напитки"}
RECENT_DAYS = 14
TIRED_DAYS = 60

COVERAGE_WEIGHT = 40
MISSING_LINE_PENALTY = 6
EXPIRY_WEIGHT = 15
EXPIRY_HORIZON_DAYS = 7
MAX_EXPIRY_BONUS = 45
SAME_MAIN_PENALTY = 15
SAME_FAMILY_PENALTY = 25
FAMILY_WINDOW_DAYS = 7
SAME_CATEGORY_PENALTY = 5
REPEAT_PENALTY = 30
LIKED_BONUS = 8
TIRED_PENALTY = 25
RECENT_PENALTY = 15
CALORIE_WEIGHT = 12
UNKNOWN_CALORIES_PENALTY = 3
RANDOM_SPREAD = 3


@dataclass
class PlanRequest:
    start: date
    days: int = 1
    meals_per_day: int = 3
    people: int = 2
    max_minutes: int | None = None
    include_unknown_time: bool = False
    budget: float | None = None
    diets: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    equipment: set[str] | None = None
    mode: str = MIXED
    allow_old: bool = True  # режим A: может ли алгоритм сам брать приготовленные блюда
    pinned: list[int] = field(default_factory=list)  # режим A: выбранные из истории
    target_kcal: int | None = None
    only_from_stock: bool = False  # только то, что есть дома; приправы по вкусу не считаются
    seed: int | None = None


@dataclass
class PlanItem:
    day: int
    meal: str
    recipe_id: int
    title: str
    servings: int
    missing_lines: int
    cost: float
    calories: float | None
    is_old: bool

    def on_date(self, start: date) -> date:
        return start + timedelta(days=self.day)


@dataclass
class Plan:
    request: PlanRequest
    items: list[PlanItem]
    slots_total: int
    warnings: list[str]

    @property
    def cost(self) -> float:
        return sum(item.cost for item in self.items)


class PlanRuleViolation(Exception):
    """В план попало блюдо, запрещённое условиями. Такой план сохранять нельзя."""


def is_eligible(recipe: Recipe, meal: str, any_slot: bool = False) -> bool:
    """any_slot: блюд мало, поэтому подходит любое блюдо, а не только типичное для приёма."""
    if NOT_A_MEAL_RE.search(recipe.title):
        return False
    if any_slot:
        return recipe.category not in NOT_MEAL_CATEGORIES
    if recipe.category in MEAL_CATEGORIES[meal]:
        return True
    if meal != "breakfast" or NOT_FOR_BREAKFAST_RE.search(recipe.title):
        return False
    return bool(BREAKFAST_RE.search(recipe.title)) and recipe.category not in {
        "Соусы", "Напитки", "Супы", "Салаты",
    }  # fmt: skip


def passes_rules(
    recipe: Recipe, request: PlanRequest, cooked: set[int], blocked: set[int]
) -> bool:
    """Жёсткие условия. Нарушивший рецепт не попадёт в план ни при каких баллах."""
    if len(recipe.lines) < MIN_LINES or recipe.id in blocked:
        return False
    if request.equipment is not None and not set(recipe.equipment) <= request.equipment:
        return False
    if request.max_minutes is not None:
        if recipe.minutes is None:
            if not request.include_unknown_time:
                return False
        elif recipe.minutes > request.max_minutes:
            return False
    if violates_diet(recipe, request.diets) or has_excluded(recipe, request.excluded):
        return False
    if recipe.id in cooked:
        if request.mode == NEW_ONLY:
            return False
        if not request.allow_old and recipe.id not in request.pinned:
            return False
    return True


def main_ingredient(recipe: Recipe) -> int | None:
    """Самый весомый продукт блюда: по нему оценивается разнообразие."""
    weighted = [
        (to_grams(line.quantity, line.unit, line.name), line.ingredient_id)
        for line in recipe.lines
        if line.quantity is not None and line.name not in ALWAYS_AVAILABLE
    ]
    return max(weighted)[1] if weighted else None


def dish_family(title: str) -> str:
    """«Мастава 3 (вариант 5)» и «Мастава 1» — одно блюдо для глаза едока."""
    return FAMILY_SUFFIX_RE.sub("", title).strip().casefold()


def price_of(grams: float, category: str | None) -> float:
    return grams / 1000 * PRICE_PER_KG.get(category, DEFAULT_PRICE_PER_KG)


@dataclass
class _Fit:
    """Как рецепт ложится на текущие запасы."""

    coverage: float
    missing_lines: int
    cost: float
    expiry_bonus: float
    required_missing: int  # недостающее без учёта приправ по вкусу


def evaluate_fit(
    recipe: Recipe,
    factor: float,
    stock: Stock,
    on_date: date,
    categories: dict[int, str | None],
) -> _Fit:
    total = covered = cost = bonus = 0.0
    missing = required_missing = 0
    for check in stock.check_recipe(recipe, factor):
        line = check.line
        if check.status is not LineStatus.ENOUGH:
            missing += 1
            required_missing += not is_optional(line)
        if line.quantity is None or line.name in ALWAYS_AVAILABLE:
            continue
        needed = to_grams(line.quantity * factor, line.unit, line.name)
        lacking = 0.0
        if check.missing_quantity is not None:
            lacking = to_grams(check.missing_quantity, line.unit, line.name)
        total += needed
        covered += needed - lacking
        cost += price_of(lacking, categories.get(line.ingredient_id))
        if lacking < needed:
            expires = stock.earliest_expiry(line.ingredient_id)
            if expires is not None:
                days = (expires - on_date).days
                bonus += EXPIRY_WEIGHT * max(0.0, 1 - days / EXPIRY_HORIZON_DAYS)
    coverage = covered / total if total else 0.0
    return _Fit(coverage, missing, cost, min(bonus, MAX_EXPIRY_BONUS), required_missing)


class _Planner:
    def __init__(self, request, recipes, stock, cooked, summary, categories):
        self.request = request
        self.recipes = recipes
        self.stock = stock.copy()
        self.cooked = cooked
        self.summary = summary
        self.categories = categories
        self.random = random.Random(request.seed)
        self.items: list[PlanItem] = []
        self.spent = 0.0
        self.last_used: dict[int, int] = {}
        self.blocked = {
            recipe_id
            for recipe_id, known in summary.items()
            if known.last_rating == "disliked" and recipe_id not in request.pinned
        }
        self.allowed = [
            recipe
            for recipe in recipes.values()
            if passes_rules(recipe, request, cooked, self.blocked)
        ]

    def score(self, recipe: Recipe, day: int, meal: str, fit: _Fit, on_date: date) -> float:
        score = COVERAGE_WEIGHT * fit.coverage - MISSING_LINE_PENALTY * fit.missing_lines
        score += fit.expiry_bonus

        main = main_ingredient(recipe)
        recent = [item for item in self.items if abs(day - item.day) <= 2]
        recent_mains = {main_ingredient(self.recipes[item.recipe_id]) for item in recent}
        if main is not None and main in recent_mains:
            score -= SAME_MAIN_PENALTY
        if self.items and self.recipes[self.items[-1].recipe_id].category == recipe.category:
            score -= SAME_CATEGORY_PENALTY
        if recipe.id in self.last_used:
            score -= REPEAT_PENALTY
        family = dish_family(recipe.title)
        if any(
            abs(day - item.day) < FAMILY_WINDOW_DAYS and dish_family(item.title) == family
            for item in self.items
        ):
            score -= SAME_FAMILY_PENALTY

        known: CookedSummary | None = self.summary.get(recipe.id)
        if known:
            age = (on_date - known.last_cooked).days
            if known.last_rating == "liked":
                score += LIKED_BONUS
            if known.last_rating == "tired" and age < TIRED_DAYS:
                score -= TIRED_PENALTY
            if age < RECENT_DAYS:
                score -= RECENT_PENALTY

        if self.request.target_kcal:
            if recipe.calories is None:
                score -= UNKNOWN_CALORIES_PENALTY
            else:
                goal = self.request.target_kcal * MEAL_SHARE[meal]
                score -= CALORIE_WEIGHT * min(abs(recipe.calories - goal) / goal, 2)
        return score + self.random.uniform(0, RANDOM_SPREAD)

    def can_use(self, recipe: Recipe, day: int) -> bool:
        used = self.last_used.get(recipe.id)
        gap = STOCK_ONLY_REPEAT_GAP_DAYS if self.request.only_from_stock else REPEAT_GAP_DAYS
        return used is None or abs(day - used) >= gap

    def fits(self, recipe: Recipe, meal: str, day: int) -> bool:
        return is_eligible(recipe, meal, self.request.only_from_stock) and self.can_use(recipe, day)

    def affordable(self, fit: _Fit) -> bool:
        if self.request.only_from_stock and fit.required_missing:
            return False
        budget = self.request.budget
        return budget is None or self.spent + fit.cost <= budget

    def add(self, recipe: Recipe, day: int, meal: str, fit: _Fit, consume: bool = True) -> None:
        servings = self.request.people
        factor = recipe.factor(servings)
        for line in recipe.lines:
            if consume and line.quantity is not None and line.name not in ALWAYS_AVAILABLE:
                self.stock.take(
                    line.ingredient_id, to_grams(line.quantity * factor, line.unit, line.name)
                )
        self.spent += fit.cost
        self.last_used[recipe.id] = day
        self.items.append(
            PlanItem(
                day=day,
                meal=meal,
                recipe_id=recipe.id,
                title=recipe.title,
                servings=servings,
                missing_lines=fit.missing_lines,
                cost=round(fit.cost, 2),
                calories=recipe.calories,
                is_old=recipe.id in self.cooked,
            )
        )

    def fill(self, day: int, meal: str, on_date: date, pinned_recipe: Recipe | None) -> bool:
        factor_people = self.request.people
        if pinned_recipe is not None:
            fit = evaluate_fit(
                pinned_recipe, pinned_recipe.factor(factor_people), self.stock, on_date,
                self.categories,
            )  # fmt: skip
            self.add(pinned_recipe, day, meal, fit)
            return True

        best = None
        best_score = float("-inf")
        for recipe in self.allowed:
            if not self.fits(recipe, meal, day):
                continue
            fit = evaluate_fit(
                recipe, recipe.factor(factor_people), self.stock, on_date, self.categories
            )
            if not self.affordable(fit):
                continue
            score = self.score(recipe, day, meal, fit, on_date)
            if score > best_score:
                best, best_score, best_fit = recipe, score, fit
        if best is None:
            return False
        self.add(best, day, meal, best_fit)
        return True


def generate_plan(
    request: PlanRequest,
    recipes: dict[int, Recipe],
    stock: Stock,
    cooked: set[int],
    summary: dict[int, CookedSummary],
    categories: dict[int, str | None],
) -> Plan:
    """Составляет план. Если блюд не хватает, слоты остаются пустыми, а не заполняются
    запрещёнными рецептами: об этом сообщает warnings."""
    planner = _Planner(request, recipes, stock, cooked, summary, categories)
    slots = SLOTS_BY_COUNT[request.meals_per_day]
    pinned_queue = [] if request.mode == NEW_ONLY else [
        recipes[recipe_id] for recipe_id in request.pinned if recipe_id in recipes
    ]  # fmt: skip
    total = request.days * len(slots)
    step = max(1, total // max(len(pinned_queue), 1))

    position = 0
    empty = 0
    for day in range(request.days):
        on_date = request.start + timedelta(days=day)
        planner.stock.drop_expired(on_date)
        for meal in slots:
            pinned = None
            if pinned_queue and position % step == 0:
                index = next(
                    (i for i, recipe in enumerate(pinned_queue) if is_eligible(recipe, meal)), None
                )
                if index is not None:
                    pinned = pinned_queue.pop(index)
            if not planner.fill(day, meal, on_date, pinned):
                empty += 1
            position += 1

    warnings = []
    if empty:
        reason = "новых блюд" if request.mode == NEW_ONLY else "подходящих блюд"
        hint = "Ослабьте условия: время, технику, диету или бюджет."
        if request.only_from_stock:
            reason = "блюд из ваших продуктов"
            hint = "Добавьте продукты в инвентарь или разрешите докупку."
        warnings.append(f"Не хватает {reason}: подобрано {total - empty} из {total}. {hint}")
    if pinned_queue:
        titles = ", ".join(recipe.title for recipe in pinned_queue)
        warnings.append(f"Не нашлось места для выбранных блюд: {titles}")
    plan = Plan(request, planner.items, total, warnings)
    verify_plan(plan, cooked)
    return plan


def verify_plan(plan: Plan, cooked: set[int]) -> None:
    """Отдельная проверка результата по базе: в режиме B старых блюд быть не может."""
    if plan.request.mode == NEW_ONLY:
        old = [item.title for item in plan.items if item.recipe_id in cooked]
        if old:
            raise PlanRuleViolation(f"В плане уже приготовленные блюда: {', '.join(old)}")


def build_plan(conn: sqlite3.Connection, request: PlanRequest, item_ids: set[int] | None = None) -> Plan:
    """Собирает данные из базы и составляет план. item_ids: какие запасы можно использовать."""
    categories = {
        row["id"]: row["category"] for row in conn.execute("SELECT id, category FROM ingredients")
    }
    return generate_plan(
        request,
        load_recipes(conn),
        Stock.load(conn, item_ids),
        cooked_recipe_ids(conn),
        cooked_summary(conn),
        categories,
    )


@dataclass
class Replacement:
    recipe_id: int
    title: str
    minutes: int | None
    missing_lines: int
    cost: float


def find_replacements(
    conn: sqlite3.Connection,
    request: PlanRequest,
    others: list[tuple[int, str, int, bool]],
    day: int,
    meal: str,
    current_recipe_id: int,
    limit: int = 5,
) -> list[Replacement]:
    """Лучшие замены блюда. others: остальные блюда плана (день, приём, рецепт, приготовлено).
    Они уже заняли свои продукты, поэтому замена не отнимет запасы у соседних блюд."""
    categories = {
        row["id"]: row["category"] for row in conn.execute("SELECT id, category FROM ingredients")
    }
    recipes = load_recipes(conn)
    planner = _Planner(
        request, recipes, Stock.load(conn), cooked_recipe_ids(conn), cooked_summary(conn), categories
    )
    on_date = request.start + timedelta(days=day)
    planner.stock.drop_expired(on_date)
    for other_day, other_meal, recipe_id, is_cooked in others:
        recipe = recipes.get(recipe_id)
        if recipe is not None:
            fit = evaluate_fit(
                recipe, recipe.factor(request.people), planner.stock, on_date, categories
            )
            # Приготовленное уже списано из инвентаря: второй раз запас не расходуем
            planner.add(recipe, other_day, other_meal, fit, consume=not is_cooked)

    scored = []
    for recipe in planner.allowed:
        if recipe.id == current_recipe_id:
            continue
        if not planner.fits(recipe, meal, day):
            continue
        fit = evaluate_fit(
            recipe, recipe.factor(request.people), planner.stock, on_date, categories
        )
        if not planner.affordable(fit):
            continue
        scored.append((planner.score(recipe, day, meal, fit, on_date), recipe, fit))
    scored.sort(key=lambda entry: entry[0], reverse=True)
    return [
        Replacement(recipe.id, recipe.title, recipe.minutes, fit.missing_lines, round(fit.cost, 2))
        for _, recipe, fit in scored[:limit]
    ]


def usage_report(plan: Plan) -> Counter:
    """Сколько блюд в плане по приёмам пищи."""
    return Counter(item.meal for item in plan.items)

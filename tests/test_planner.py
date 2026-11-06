from contextlib import closing
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

import config
from core import nutrition
from core.history_manager import cooked_recipe_ids, record_cooked
from core.ingredient_matcher import IngredientMatcher, find_ingredient
from core.inventory_manager import add_item, get_item, list_items
from core.meal_planner import (
    MIXED,
    NEW_ONLY,
    Plan,
    PlanItem,
    PlanRequest,
    PlanRuleViolation,
    build_plan,
    dish_family,
    is_eligible,
    main_ingredient,
    verify_plan,
)
from core.plan_manager import (
    current_plan,
    delete_plan,
    get_plan,
    list_plans,
    period_name,
    replace_item,
    replacements_for,
    save_plan,
)
from core.preferences import (
    Preferences,
    has_excluded,
    load_preferences,
    save_preferences,
    violates_diet,
)
from core.recipe_manager import Recipe, RecipeLine, get_recipe
from core.shopping_list import build_shopping_list, move_purchased_to_inventory, set_purchased
from core.stock import Stock, find_substitutes
from database.database import connect

TODAY = date(2026, 11, 5)
ALL = {"Плита", "Духовка"}


@pytest.fixture
def db(conn):
    IngredientMatcher(conn)
    return conn


def add_recipe(conn, title, lines=(("Морковь", 200, "г"), ("Соль", None, None)), category="Вторые блюда",
               servings=2, minutes=30, equipment=("Плита",), calories=None):  # fmt: skip
    recipe_id = conn.execute(
        "INSERT INTO recipes (title, servings, cook_time_min, category, calories) "
        "VALUES (?, ?, ?, ?, ?)",
        (title, servings, minutes, category, calories),
    ).lastrowid
    for name, quantity, unit in lines:
        ingredient_id = find_ingredient(conn, name) or IngredientMatcher(conn).resolve(name)
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) "
            "VALUES (?, ?, ?, ?)",
            (recipe_id, ingredient_id, quantity, unit),
        )
    for name in equipment:
        conn.execute(
            "INSERT INTO recipe_equipment (recipe_id, equipment_id) "
            "SELECT ?, id FROM kitchen_equipment WHERE name = ?",
            (recipe_id, name),
        )
    return recipe_id


def fill_mains(conn, count=10):
    return [
        add_recipe(conn, f"Блюдо {number}", [(f"Продукт{number}", 100, "г"), ("Соль", None, None)])
        for number in range(count)
    ]


def request(**fields):
    fields.setdefault("start", TODAY)
    fields.setdefault("meals_per_day", 1)
    fields.setdefault("equipment", ALL)
    fields.setdefault("seed", 1)
    return PlanRequest(**fields)


def plan_ids(conn, **fields):
    return [item.recipe_id for item in build_plan(conn, request(**fields)).items]


# --- Этап 7: дневной рацион ---


def test_day_plan_has_requested_meals(db):
    add_recipe(db, "Каша овсяная", category="Каши")
    add_recipe(db, "Суп", category="Супы")
    add_recipe(db, "Гуляш")
    add_recipe(db, "Рагу", [("Лук", 100, "г"), ("Соль", None, None)])
    plan = build_plan(db, request(meals_per_day=3))
    assert [item.meal for item in plan.items] == ["breakfast", "lunch", "dinner"]
    assert plan.warnings == []


def test_meals_fit_their_slot(db):
    soup = add_recipe(db, "Суп", category="Супы")
    add_recipe(db, "Торт", category="Десерты")
    assert plan_ids(db, meals_per_day=2)[0] == soup


@pytest.mark.parametrize(
    "title, category, meal, expected",
    [
        ("Овсянка", "Каши", "breakfast", True),
        ("Омлет с сыром", "Вторые блюда", "breakfast", True),
        ("Блины", "Выпечка", "breakfast", True),
        ("Творожное мороженое", "Десерты", "breakfast", False),
        ("Соус Томат", "Соусы", "dinner", False),
        ("Соус Томат", "Соусы", "breakfast", False),
        ("Раковое масло", "Вторые блюда", "dinner", False),
        ("Бульон мясной", "Супы", "lunch", False),
        ("Плов", "Вторые блюда", "dinner", True),
        ("Суп из фасоли", "Супы", "lunch", True),
        ("Суп из фасоли", "Супы", "dinner", False),
    ],
)
def test_is_eligible(title, category, meal, expected):
    assert is_eligible(Recipe(1, title, category, 30, False, 2), meal) is expected


def test_prefers_recipes_covered_by_inventory(db):
    add_recipe(db, "Чужое", [("Тамаринд", 300, "г"), ("Кумкват", 100, "г")])
    home = add_recipe(db, "Из запасов", [("Морковь", 300, "г"), ("Лук", 100, "г")])
    add_item(db, "Морковь", 1, "кг")
    add_item(db, "Лук", 1, "кг")
    assert plan_ids(db) == [home]


def test_quantity_matters_not_just_presence(db):
    enough = add_recipe(db, "Хватает", [("Морковь", 100, "г"), ("Лук", 100, "г")])
    add_recipe(db, "Не хватает", [("Морковь", 5000, "г"), ("Лук", 5000, "г")])
    add_item(db, "Морковь", 200, "г")
    add_item(db, "Лук", 200, "г")
    assert plan_ids(db) == [enough]


def test_prefers_products_with_nearest_expiry(db):
    add_recipe(db, "Из риса", [("Рис", 200, "г"), ("Соль", None, None)])
    urgent = add_recipe(db, "Из сметаны", [("Сметана", 200, "г"), ("Соль", None, None)])
    add_item(db, "Соль", 100, "г")
    add_item(db, "Рис", 1, "кг", TODAY + timedelta(days=300))
    add_item(db, "Сметана", 1, "кг", TODAY + timedelta(days=1))
    assert plan_ids(db) == [urgent]


def test_expired_product_is_not_used(db):
    add_recipe(db, "Из сметаны", [("Сметана", 200, "г"), ("Соль", None, None)])
    add_item(db, "Сметана", 1, "кг", TODAY - timedelta(days=1))
    plan = build_plan(db, request())
    assert plan.items[0].missing_lines == 2


def test_equipment_is_required(db):
    add_recipe(db, "В духовке", equipment=("Духовка",))
    stove = add_recipe(db, "На плите", equipment=("Плита",))
    assert plan_ids(db, equipment={"Плита"}) == [stove]


def test_no_equipment_selected_keeps_only_recipes_without_it(db):
    add_recipe(db, "На плите", equipment=("Плита",))
    cold = add_recipe(db, "Холодное", equipment=())
    assert plan_ids(db, equipment=set()) == [cold]


def test_time_limit_is_hard(db):
    add_recipe(db, "Долго", minutes=120)
    quick = add_recipe(db, "Быстро", minutes=20)
    assert plan_ids(db, max_minutes=30) == [quick]


def test_unknown_time_is_excluded_unless_allowed(db):
    unknown = add_recipe(db, "Без времени", minutes=None)
    plan = build_plan(db, request(max_minutes=30))
    assert plan.items == [] and plan.warnings
    assert plan_ids(db, max_minutes=30, include_unknown_time=True) == [unknown]


def test_empty_slot_gets_warning(db):
    plan = build_plan(db, request(days=2))
    assert plan.items == []
    assert "подобрано 0 из 2" in plan.warnings[0]


def test_diet_and_excluded_ingredients(db):
    add_recipe(db, "С мясом", [("Говядина", 200, "г"), ("Соль", None, None)])
    add_recipe(db, "С грибами", [("Грибы белые", 200, "г"), ("Соль", None, None)])
    salad = add_recipe(db, "Овощное", [("Морковь", 200, "г"), ("Соль", None, None)])
    assert plan_ids(db, diets=["vegetarian"], excluded=["грибы"]) == [salad]


@pytest.mark.parametrize(
    "line, diet, expected",
    [
        ("Говядина", "vegetarian", True),
        ("Морковь", "vegetarian", False),
        ("Свинина", "no_pork", True),
        ("Говядина", "no_pork", False),
        ("Лосось", "no_fish", True),
        ("Сметана", "no_dairy", True),
        ("Мука пшеничная", "no_gluten", True),
        ("Миндаль", "no_nuts", True),
    ],
)
def test_violates_diet(line, diet, expected):
    recipe = Recipe(1, "Х", None, None, False, 2, lines=[RecipeLine(1, line, 1, "г")])
    assert violates_diet(recipe, [diet]) is expected


def test_excluded_matches_by_words_and_case():
    recipe = Recipe(1, "Х", None, None, False, 2, lines=[RecipeLine(1, "Грибы белые", 1, "г")])
    assert has_excluded(recipe, ["ГРИБЫ"])
    assert has_excluded(recipe, ["гриб"])
    assert not has_excluded(recipe, ["белок"])
    assert not has_excluded(recipe, [" "])


def test_budget_limits_purchases(db):
    add_recipe(db, "Дорогое", [("Говядина", 2000, "г"), ("Свинина", 2000, "г")])
    cheap = add_recipe(db, "Дешёвое", [("Морковь", 200, "г"), ("Лук", 100, "г")])
    assert plan_ids(db, budget=100) == [cheap]


def test_budget_is_shared_by_the_whole_plan(db):
    fill_mains(db, 5)
    plan = build_plan(db, request(days=5, budget=70))
    assert plan.cost <= 70
    assert len(plan.items) < 5
    assert plan.warnings


def test_calorie_target_prefers_close_dishes(db):
    add_recipe(db, "Лёгкое", calories=100, lines=[("Морковь", 200, "г"), ("Соль", None, None)])
    close = add_recipe(db, "Сытное", calories=600, lines=[("Лук", 200, "г"), ("Соль", None, None)])
    assert plan_ids(db, target_kcal=2000) == [close]


def test_disliked_dish_is_not_offered(db):
    disliked = add_recipe(db, "Невкусное")
    other = add_recipe(db, "Другое блюдо", [("Лук", 100, "г"), ("Соль", None, None)])
    record_cooked(db, disliked, TODAY, 2, "disliked", today=TODAY)
    assert plan_ids(db) == [other]


def test_liked_dish_beats_neutral_and_tired_loses(db):
    liked = add_recipe(db, "Любимое", [("Лук", 100, "г"), ("Соль", None, None)])
    tired = add_recipe(db, "Надоевшее", [("Морковь", 100, "г"), ("Соль", None, None)])
    old = TODAY - timedelta(days=30)
    record_cooked(db, liked, old, 2, "liked", today=TODAY)
    record_cooked(db, tired, old, 2, "tired", today=TODAY)
    assert plan_ids(db) == [liked]


def test_recently_cooked_loses_to_new(db):
    recent = add_recipe(db, "Вчерашнее", [("Лук", 100, "г"), ("Соль", None, None)])
    fresh = add_recipe(db, "Свежее", [("Морковь", 100, "г"), ("Соль", None, None)])
    record_cooked(db, recent, TODAY - timedelta(days=1), 2, today=TODAY)
    assert plan_ids(db) == [fresh]


def test_same_seed_gives_same_plan(db):
    fill_mains(db)
    assert plan_ids(db, days=5, seed=7) == plan_ids(db, days=5, seed=7)


def test_generation_does_not_change_real_inventory(db):
    add_recipe(db, "Блюдо", [("Морковь", 200, "г"), ("Соль", None, None)])
    item_id = add_item(db, "Морковь", 1, "кг")
    build_plan(db, request())
    assert get_item(db, item_id).quantity == 1


def test_only_selected_stock_is_used(db):
    add_recipe(db, "Блюдо", [("Морковь", 200, "г"), ("Лук", 100, "г")])
    add_item(db, "Морковь", 1, "кг")
    onion = add_item(db, "Лук", 1, "кг")
    plan = build_plan(db, request(), {onion})
    assert plan.items[0].missing_lines == 1


def test_main_ingredient_is_heaviest():
    recipe = Recipe(1, "Х", None, None, False, 2, lines=[
        RecipeLine(1, "Соль", 5, "г"), RecipeLine(2, "Курица", 1, "кг"), RecipeLine(3, "Вода", 5, "л"),
    ])  # fmt: skip
    assert main_ingredient(recipe) == 2


# --- Этап 8: неделя и месяц ---


def test_week_has_no_repeats(db):
    fill_mains(db, 10)
    ids = plan_ids(db, days=7)
    assert len(ids) == 7 and len(set(ids)) == 7


def test_month_repeats_only_after_gap(db):
    fill_mains(db, 15)
    plan = build_plan(db, request(days=30))
    assert len(plan.items) == 30
    last = {}
    for item in plan.items:
        if item.recipe_id in last:
            assert item.day - last[item.recipe_id] >= 14
        last[item.recipe_id] = item.day


def test_short_pool_leaves_gaps_instead_of_repeating_too_soon(db):
    fill_mains(db, 2)
    plan = build_plan(db, request(days=7))
    assert len(plan.items) == 2
    assert "подобрано 2 из 7" in plan.warnings[0]


def test_spec_example_one_kg_of_chicken_is_shared_between_dishes(db):
    """Пример из ТЗ: 1 кг курицы распределяется между рецептами без лишних покупок."""
    for number in range(3):
        add_recipe(db, f"Курица {number}", [("Курица", 400, "г"), ("Соль", None, None)])
    add_item(db, "Курица", 1, "кг")
    add_item(db, "Соль", 1, "кг")

    plan = build_plan(db, request(days=3))

    assert [item.missing_lines for item in plan.items] == [0, 0, 1]
    assert plan.items[0].cost == 0 and plan.items[1].cost == 0
    assert plan.items[2].cost > 0  # на третье блюдо осталось 200 г из 400


def test_week_of_diverse_main_ingredients(db):
    for number in range(4):
        add_recipe(db, f"Морковное {number}", [("Морковь", 300, "г"), ("Соль", None, None)])
    for number in range(4):
        add_recipe(db, f"Луковое {number}", [("Лук", 300, "г"), ("Соль", None, None)])
    plan = build_plan(db, request(days=2))
    mains = [main_ingredient(get_recipe(db, item.recipe_id)) for item in plan.items]
    assert len(set(mains)) == 2


def test_batches_expiring_during_plan_are_dropped_on_that_day(db):
    add_recipe(db, "Первое", [("Сметана", 100, "г"), ("Соль", None, None)])
    add_recipe(db, "Второе", [("Сметана", 100, "г"), ("Соль", None, None)])
    add_item(db, "Сметана", 1, "кг", TODAY)
    add_item(db, "Соль", 1, "кг")
    plan = build_plan(db, request(days=2))
    assert [item.missing_lines for item in plan.items] == [0, 1]


# --- Этап 9: режимы ---


def test_mode_b_excludes_all_cooked(db):
    cooked = fill_mains(db, 10)[:5]
    for recipe_id in cooked:
        record_cooked(db, recipe_id, TODAY, 2, "liked", today=TODAY)
    ids = plan_ids(db, days=5, mode=NEW_ONLY)
    assert len(ids) == 5
    assert not set(ids) & set(cooked)


def test_mode_b_reports_shortage_and_never_uses_old(db):
    recipes = fill_mains(db, 4)
    for recipe_id in recipes[:3]:
        record_cooked(db, recipe_id, TODAY, 2, "liked", today=TODAY)

    plan = build_plan(db, request(days=3, mode=NEW_ONLY))

    assert [item.recipe_id for item in plan.items] == [recipes[3]]
    assert "новых блюд" in plan.warnings[0]
    assert "подобрано 1 из 3" in plan.warnings[0]


def test_mode_b_ignores_pinned_old_dish(db):
    old = add_recipe(db, "Старое")
    new = add_recipe(db, "Новое", [("Лук", 100, "г"), ("Соль", None, None)])
    record_cooked(db, old, TODAY, 2, today=TODAY)
    assert plan_ids(db, mode=NEW_ONLY, pinned=[old]) == [new]


def test_verify_plan_catches_violation_directly():
    item = PlanItem(0, "dinner", 5, "Старое", 2, 0, 0, None, True)
    plan = Plan(request(mode=NEW_ONLY), [item], 1, [])
    with pytest.raises(PlanRuleViolation, match="Старое"):
        verify_plan(plan, {5})
    verify_plan(Plan(request(mode=MIXED), [item], 1, []), {5})


def test_save_rechecks_history_at_save_time(db):
    recipe_id = add_recipe(db, "Блюдо")
    plan = build_plan(db, request(mode=NEW_ONLY))
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)  # приготовлено уже после составления
    with pytest.raises(PlanRuleViolation, match="Блюдо"):
        save_plan(db, plan)
    assert list_plans(db) == []


def test_mode_a_allows_old_dishes(db):
    old = add_recipe(db, "Старое")
    record_cooked(db, old, TODAY - timedelta(days=40), 2, "liked", today=TODAY)
    assert plan_ids(db, mode=MIXED) == [old]


def test_mode_a_without_auto_old_uses_only_new(db):
    old = add_recipe(db, "Старое")
    record_cooked(db, old, TODAY - timedelta(days=40), 2, "liked", today=TODAY)
    new = add_recipe(db, "Новое", [("Лук", 100, "г"), ("Соль", None, None)])
    assert plan_ids(db, mode=MIXED, allow_old=False) == [new]


def test_mode_a_pinned_dish_is_included(db):
    pinned = add_recipe(db, "Выбранное", [("Тамаринд", 900, "г"), ("Кумкват", 900, "г")])
    record_cooked(db, pinned, TODAY, 2, "ok", today=TODAY)
    fill_mains(db, 5)
    plan = build_plan(db, request(days=3, mode=MIXED, allow_old=False, pinned=[pinned]))
    assert pinned in [item.recipe_id for item in plan.items]
    assert [item.is_old for item in plan.items].count(True) == 1


def test_pinned_disliked_dish_is_respected(db):
    pinned = add_recipe(db, "Спорное")
    record_cooked(db, pinned, TODAY, 2, "disliked", today=TODAY)
    assert plan_ids(db, pinned=[pinned]) == [pinned]


def test_pinned_without_suitable_meal_is_reported(db):
    pinned = add_recipe(db, "Соус", category="Соусы")
    record_cooked(db, pinned, TODAY, 2, today=TODAY)
    plan = build_plan(db, request(pinned=[pinned]))
    assert "Соус" in plan.warnings[-1]


# --- Хранение планов ---


def test_save_and_read_plan(db):
    fill_mains(db, 3)
    plan = build_plan(db, request(days=3, people=4))
    plan_id = save_plan(db, plan)
    saved = get_plan(db, plan_id)
    assert saved.name == "3 дн. с 05.11.2026"
    assert (saved.start, saved.days, saved.people) == (TODAY, 3, 4)
    assert [item.recipe_id for item in saved.items] == [item.recipe_id for item in plan.items]
    assert saved.items_on(TODAY + timedelta(days=1))[0].day == 1


@pytest.mark.parametrize("days, name", [(1, "День"), (7, "Неделя"), (30, "Месяц"), (3, "3 дн.")])
def test_period_name(days, name):
    assert period_name(days) == name


def test_current_plan_covers_today(db):
    fill_mains(db, 3)
    save_plan(db, build_plan(db, request(days=3)))
    assert current_plan(db, TODAY + timedelta(days=2)) is not None
    assert current_plan(db, TODAY + timedelta(days=3)) is None


def test_delete_plan_removes_items(db):
    fill_mains(db, 1)
    plan_id = save_plan(db, build_plan(db, request()))
    delete_plan(db, plan_id)
    assert list_plans(db) == []
    assert db.execute("SELECT count(*) FROM meal_plan_items").fetchone()[0] == 0


def test_cooking_marks_plan_item(db):
    [recipe_id] = fill_mains(db, 1)
    plan_id = save_plan(db, build_plan(db, request()))
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    assert get_plan(db, plan_id).items[0].cooked_on == TODAY
    assert cooked_recipe_ids(db) == {recipe_id}


# --- Предпочтения ---


def test_preferences_defaults_and_round_trip(db):
    assert load_preferences(db) == Preferences()
    saved = Preferences(4, 2, None, ["no_pork"], ["грибы"], 1800)
    save_preferences(db, saved)
    save_preferences(db, saved)
    assert load_preferences(db) == saved


# --- КБЖУ ---


def test_nutrition_estimate_per_serving():
    lines = [RecipeLine(1, "Куриная грудка", 400, "г"), RecipeLine(2, "Рис", 200, "г")]
    result = nutrition.estimate(lines, 2)
    assert result.calories == pytest.approx((113 * 4 + 344 * 2) / 2)
    assert result.protein_g == pytest.approx((23.6 * 4 + 6.7 * 2) / 2)


def test_nutrition_counts_pieces():
    assert nutrition.estimate([RecipeLine(1, "Яйцо", 2, "шт")], 1).calories == pytest.approx(172.7)


def test_nutrition_needs_most_of_the_recipe_known():
    lines = [RecipeLine(1, "Неизвестный продукт", 900, "г"), RecipeLine(2, "Рис", 100, "г")]
    assert nutrition.estimate(lines, 2) is None


def test_nutrition_rejects_implausible_serving():
    assert nutrition.estimate([RecipeLine(1, "Масло сливочное", 5, "кг")], 1) is None


def test_nutrition_without_quantities():
    assert nutrition.estimate([RecipeLine(1, "Соль", None, None)], 1) is None


# --- Этап 10: список покупок ---


def shopping(conn, **fields):
    plan_id = save_plan(conn, build_plan(conn, request(**fields)))
    return plan_id, build_shopping_list(conn, plan_id)


def by_name(lines):
    return {line.name: line for line in lines}


def test_list_merges_same_ingredient_and_names_recipes(db):
    add_recipe(db, "Первое", [("Морковь", 200, "г"), ("Соль", None, None)])
    add_recipe(db, "Второе", [("Морковь", 300, "г"), ("Лук", 100, "г")])
    _, lines = shopping(db, days=2)
    carrot = by_name(lines)["Морковь"]
    assert (carrot.quantity, carrot.unit) == (500, "г")
    assert sorted(carrot.recipes) == ["Второе", "Первое"]
    assert len([line for line in lines if line.name == "Морковь"]) == 1


def test_list_subtracts_stock(db):
    add_recipe(db, "Блюдо", [("Морковь", 500, "г"), ("Лук", 100, "г")])
    add_item(db, "Морковь", 200, "г")
    add_item(db, "Лук", 1, "кг")
    _, lines = shopping(db)
    assert [(line.name, line.quantity) for line in lines] == [("Морковь", 300)]


def test_stock_is_not_counted_twice_across_dishes(db):
    add_recipe(db, "Первое", [("Морковь", 300, "г"), ("Лук", 100, "г")])
    add_recipe(db, "Второе", [("Морковь", 300, "г"), ("Лук", 100, "г")])
    add_item(db, "Морковь", 400, "г")
    add_item(db, "Лук", 1, "кг")
    _, lines = shopping(db, days=2)
    assert by_name(lines)["Морковь"].quantity == 200


def test_people_scale_the_list(db):
    add_recipe(db, "Блюдо", [("Морковь", 200, "г"), ("Лук", 100, "г")], servings=2)
    _, lines = shopping(db, people=4)
    assert by_name(lines)["Морковь"].quantity == 400


def test_mixed_units_are_shown_in_grams(db):
    add_recipe(db, "Первое", [("Морковь", 1, "шт"), ("Соль", None, None)])
    add_recipe(db, "Второе", [("Морковь", 50, "г"), ("Соль", None, None)])
    _, lines = shopping(db, days=2)
    assert (by_name(lines)["Морковь"].quantity, by_name(lines)["Морковь"].unit) == (150, "г")


def test_pieces_are_rounded_up(db):
    add_recipe(db, "Блюдо", [("Яйцо", 1.5, "шт"), ("Соль", None, None)])
    _, lines = shopping(db)
    assert by_name(lines)["Яйцо"].quantity == 2


def test_water_is_not_bought_and_to_taste_has_no_amount(db):
    add_recipe(db, "Блюдо", [("Вода", 2, "л"), ("Соль", None, None)])
    _, lines = shopping(db)
    assert list(by_name(lines)) == ["Соль"]
    assert by_name(lines)["Соль"].quantity is None


def test_to_taste_with_stock_is_not_listed(db):
    add_recipe(db, "Блюдо", [("Вода", 2, "л"), ("Соль", None, None)])
    add_item(db, "Соль", 100, "г")
    assert shopping(db)[1] == []


def test_cooked_dish_is_not_in_list(db):
    recipe_id = add_recipe(db, "Блюдо", [("Морковь", 500, "г"), ("Лук", 100, "г")])
    plan_id, lines = shopping(db)
    assert lines
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    assert build_shopping_list(db, plan_id) == []


def test_purchased_flag_round_trip(db):
    add_recipe(db, "Блюдо", [("Морковь", 500, "г"), ("Лук", 100, "г")])
    plan_id, lines = shopping(db)
    carrot = by_name(lines)["Морковь"]
    set_purchased(db, plan_id, carrot.ingredient_id, True)
    set_purchased(db, plan_id, carrot.ingredient_id, True)
    assert by_name(build_shopping_list(db, plan_id))["Морковь"].purchased
    set_purchased(db, plan_id, carrot.ingredient_id, False)
    assert not by_name(build_shopping_list(db, plan_id))["Морковь"].purchased


def test_moving_purchases_updates_inventory_and_list(db):
    add_recipe(db, "Блюдо", [("Морковь", 500, "г"), ("Соль", None, None)])
    plan_id, lines = shopping(db)
    for line in lines:
        set_purchased(db, plan_id, line.ingredient_id, True)

    assert move_purchased_to_inventory(db, plan_id) == 2

    stock = {item.name: (item.quantity, item.unit) for item in list_items(db)}
    assert stock == {"Морковь": (500, "г"), "Соль": (1, "упаковка")}
    assert build_shopping_list(db, plan_id) == []


def test_unknown_plan_gives_empty_list(db):
    assert build_shopping_list(db, 999) == []


def test_lines_are_grouped_by_category(db):
    add_recipe(db, "Блюдо", [("Морковь", 500, "г"), ("Говядина", 100, "г")])
    _, lines = shopping(db)
    categories = [line.category or "Другое" for line in lines]
    assert categories == sorted(categories)


# --- Замены ---


def test_substitute_from_recipe_note(db):
    add_item(db, "Сметана", 100, "г")
    line = RecipeLine(find_ingredient(db, "Сливки"), "Сливки", 100, "г", note="или сметана")
    assert find_substitutes(db, Stock.load(db), line) == ["сметана"]


def test_substitute_from_same_group(db):
    add_item(db, "Масло сливочное", 100, "г")
    line = RecipeLine(find_ingredient(db, "Масло растительное"), "Масло растительное", 50, "г")
    assert find_substitutes(db, Stock.load(db), line) == ["Масло сливочное"]


def test_no_substitute_when_nothing_in_stock(db):
    line = RecipeLine(find_ingredient(db, "Сливки"), "Сливки", 100, "г", note="или сметана")
    assert find_substitutes(db, Stock.load(db), line) == []


# --- Страницы ---


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    import streamlit as st

    from database.database import init_db

    db_path, backup_dir = tmp_path / "app.db", tmp_path / "backups"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    init_db(db_path, backup_dir)
    with closing(connect(db_path)) as conn:
        IngredientMatcher(conn)
    st.cache_resource.clear()
    return db_path


def with_db(db_path, action):
    with closing(connect(db_path)) as conn:
        return action(conn)


def app_page(name):
    app = AppTest.from_file(str(config.BASE_DIR / "interface" / "pages" / name), default_timeout=30)
    app.run()
    assert not app.exception
    return app


def full_app():
    app = AppTest.from_file(str(config.BASE_DIR / "app.py"), default_timeout=30).run()
    assert not app.exception
    return app


def text_of(app):
    groups = (app.markdown, app.caption, app.subheader, app.title, app.warning)
    return " ".join(item.value for group in groups for item in group)


def button(app, label):
    return next(item for item in app.button if item.label == label)


def test_generator_builds_preview(app_db):
    with_db(app_db, lambda conn: fill_mains(conn, 5))
    with_db(app_db, lambda conn: conn.execute("UPDATE kitchen_equipment SET is_available = 1"))
    app = app_page("generator.py")
    next(box for box in app.checkbox if box.label.startswith("Только из продуктов")).uncheck()
    app.selectbox[0].set_value("День")
    app.number_input[0].set_value(1)
    button(app, "Составить план").click().run()
    assert not app.exception
    assert "Предварительный план" in text_of(app)
    assert "Ужин: Блюдо" in text_of(app)


def test_generator_warns_when_recipes_run_out(app_db):
    app = app_page("generator.py")
    button(app, "Составить план").click().run()
    assert not app.exception
    assert "Не хватает" in text_of(app)


def test_plans_page_empty(app_db):
    assert "Планов пока нет" in text_of(full_app_page("plans.py"))


def full_app_page(name):
    app = AppTest.from_file(str(config.BASE_DIR / "app.py"), default_timeout=30).run()
    app.switch_page(f"interface/pages/{name}").run()
    assert not app.exception
    return app


def test_plans_page_shows_saved_plan(app_db):
    with_db(app_db, lambda conn: fill_mains(conn, 2))
    start = date.today()
    with_db(app_db, lambda conn: save_plan(conn, build_plan(conn, request(start=start, days=2))))
    app = app_page("plans.py")
    assert any(item.label.startswith("Блюдо") for item in app.button)

    button(app, "Удалить этот план").click().run()
    assert with_db(app_db, list_plans) == []


def test_home_shows_today_plan(app_db):
    with_db(app_db, lambda conn: fill_mains(conn, 1))
    with_db(app_db, lambda conn: save_plan(conn, build_plan(conn, request(start=date.today()))))
    assert "Сегодня по плану" in text_of(full_app())


def test_shopping_page_flow(app_db):
    with_db(app_db, lambda conn: add_recipe(conn, "Блюдо", [("Морковь", 500, "г"), ("Говядина", 1500, "г")]))
    with_db(app_db, lambda conn: save_plan(conn, build_plan(conn, request(start=date.today()))))

    app = full_app_page("shopping.py")
    labels = [item.label for item in app.checkbox]
    assert "Морковь — 500 г" in labels
    assert "Говядина — 1,5 кг" in labels
    assert "Для: Блюдо" in text_of(app)

    app.checkbox[0].check().run()
    button(app, "Перенести купленное в инвентарь").click().run()

    assert not app.exception
    assert len(with_db(app_db, list_items)) == 1


def test_shopping_page_without_plans(app_db):
    assert "появится" in text_of(full_app_page("shopping.py"))


def test_settings_saves_preferences(app_db):
    app = app_page("settings.py")
    next(item for item in app.text_input if item.label == "Исключить продукты").set_value("грибы, кинза")
    button(app, "Сохранить предпочтения").click().run()
    assert not app.exception
    assert with_db(app_db, load_preferences).excluded == ["грибы", "кинза"]


def test_final_check_catches_broken_filters(db, monkeypatch):
    """Вторая линия защиты: даже при ошибке в фильтрах план со старым блюдом не выйдет."""
    recipe_id = add_recipe(db, "Старое")
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    monkeypatch.setattr("core.meal_planner.passes_rules", lambda *args: True)
    with pytest.raises(PlanRuleViolation, match="Старое"):
        build_plan(db, request(mode=NEW_ONLY))


@pytest.mark.parametrize(
    "title, family",
    [("Мастава 3 (вариант 5)", "мастава"), ("Мастава 1", "мастава"), ("Плов", "плов"), ("Соус 5 звёзд", "соус 5 звёзд")],
)
def test_dish_family(title, family):
    assert dish_family(title) == family


def test_variants_of_one_dish_are_spread_out(db):
    add_recipe(db, "Мастава 1", [("Морковь", 100, "г"), ("Соль", None, None)])
    add_recipe(db, "Мастава 2", [("Морковь", 100, "г"), ("Соль", None, None)])
    add_recipe(db, "Рагу", [("Лук", 100, "г"), ("Соль", None, None)])
    titles = [item.title for item in build_plan(db, request(days=2)).items]
    assert len({dish_family(title) for title in titles}) == 2


# --- Замена блюда в плане ---


def saved(conn, **fields):
    return get_plan(conn, save_plan(conn, build_plan(conn, request(**fields))))


def test_replacements_exclude_current_and_other_plan_dishes(db):
    fill_mains(db, 6)
    plan = saved(db, days=3)
    used = {item.recipe_id for item in plan.items}
    options = replacements_for(db, plan, plan.items[0])
    assert options
    assert not {option.recipe_id for option in options} & used


def test_replacement_respects_saved_conditions(db):
    fill_mains(db, 2)
    slow = add_recipe(db, "Долгое", [("Лук", 100, "г"), ("Соль", None, None)], minutes=300)
    oven = add_recipe(db, "В духовке", [("Лук", 100, "г"), ("Соль", None, None)], equipment=("Духовка",))
    meat = add_recipe(db, "Мясное", [("Говядина", 100, "г"), ("Соль", None, None)])
    good = add_recipe(db, "Подходит", [("Рис", 100, "г"), ("Соль", None, None)])
    plan = saved(db, max_minutes=60, equipment={"Плита"}, diets=["vegetarian"])
    ids = {option.recipe_id for option in replacements_for(db, plan, plan.items[0], limit=20)}
    assert good in ids
    assert not ids & {slow, oven, meat}


def test_replacement_in_new_only_plan_never_offers_cooked(db):
    recipes = fill_mains(db, 4)
    record_cooked(db, recipes[0], TODAY, 2, today=TODAY)
    plan = saved(db, mode=NEW_ONLY)
    ids = {option.recipe_id for option in replacements_for(db, plan, plan.items[0], limit=20)}
    assert recipes[0] not in ids


def test_replace_rechecks_new_only_rule(db):
    recipes = fill_mains(db, 3)
    plan = saved(db, mode=NEW_ONLY)
    other = next(recipe_id for recipe_id in recipes if recipe_id != plan.items[0].recipe_id)
    record_cooked(db, other, TODAY, 2, today=TODAY)
    with pytest.raises(PlanRuleViolation):
        replace_item(db, plan, plan.items[0], other)


def test_replace_changes_dish_and_shopping_list(db):
    add_recipe(db, "Морковное", [("Морковь", 500, "г"), ("Соль", None, None)])
    plan = saved(db)
    other = add_recipe(db, "Луковое", [("Лук", 300, "г"), ("Соль", None, None)])
    replace_item(db, plan, plan.items[0], other)
    fresh = get_plan(db, plan.id)
    assert fresh.items[0].recipe_id == other
    names = {line.name for line in build_shopping_list(db, plan.id)}
    assert "Лук репчатый" in names and "Морковь" not in names


def test_cooked_dish_cannot_be_replaced(db):
    [recipe_id] = fill_mains(db, 1)
    plan = saved(db)
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    other = add_recipe(db, "Другое", [("Лук", 100, "г"), ("Соль", None, None)])
    replace_item(db, get_plan(db, plan.id), plan.items[0], other)
    assert get_plan(db, plan.id).items[0].recipe_id == recipe_id


def test_replacement_does_not_take_stock_of_neighbour_dishes(db):
    add_recipe(db, "Первое", [("Курица", 800, "г"), ("Соль", None, None)])
    add_recipe(db, "Второе", [("Курица", 800, "г"), ("Соль", None, None)])
    add_recipe(db, "Третье", [("Курица", 800, "г"), ("Соль", None, None)])
    add_item(db, "Курица", 1, "кг")
    add_item(db, "Соль", 1, "кг")
    plan = saved(db, days=2)
    [option] = [
        option for option in replacements_for(db, plan, plan.items[1], limit=5)
    ]
    assert option.cost > 0  # из килограмма 800 г уже заняты первым блюдом


def test_old_plan_without_params_still_offers_replacements(db):
    fill_mains(db, 3)
    plan = saved(db)
    db.execute("UPDATE meal_plans SET params = '{}'")
    assert replacements_for(db, get_plan(db, plan.id), plan.items[0])


def test_plans_page_swap_flow(app_db):
    with_db(app_db, lambda conn: fill_mains(conn, 3))
    with_db(app_db, lambda conn: save_plan(conn, build_plan(conn, request(start=date.today()))))
    before = with_db(app_db, list_plans)[0].items[0].recipe_id

    app = app_page("plans.py")
    button(app, "Заменить").click().run()
    assert not app.exception
    option = next(item for item in app.button if "докупить" in item.label)
    option.click().run()

    assert not app.exception
    assert with_db(app_db, list_plans)[0].items[0].recipe_id != before


def test_plans_page_swap_can_be_cancelled(app_db):
    with_db(app_db, lambda conn: fill_mains(conn, 3))
    with_db(app_db, lambda conn: save_plan(conn, build_plan(conn, request(start=date.today()))))
    app = app_page("plans.py")
    button(app, "Заменить").click().run()
    button(app, "Отмена").click().run()
    assert not any("докупить" in item.label for item in app.button)


# --- Только из того, что есть дома ---


def test_user_report_plan_uses_only_what_is_at_home(db):
    """Случай пользователя: дома мука, яйца, вода, соль — никаких других продуктов в плане."""
    noodles = add_recipe(db, "Лапша", [("Мука", 300, "г"), ("Яйцо", 2, "шт"), ("Вода", 100, "мл"), ("Соль", None, None)])
    add_recipe(db, "Пирог с мясом", [("Мука", 300, "г"), ("Говядина", 300, "г")])
    add_recipe(db, "Блины", [("Мука", 200, "г"), ("Молоко", 500, "мл")])
    for name, quantity, unit in [("Мука", 2, "кг"), ("Яйцо", 10, "шт"), ("Соль", 1, "кг")]:
        add_item(db, name, quantity, unit)

    plan = build_plan(db, request(only_from_stock=True))

    assert [item.recipe_id for item in plan.items] == [noodles]
    assert plan.items[0].cost == 0


def test_not_enough_quantity_is_refused(db):
    add_recipe(db, "Много муки", [("Мука", 5000, "г"), ("Соль", None, None)])
    add_item(db, "Мука", 1, "кг")
    plan = build_plan(db, request(only_from_stock=True))
    assert plan.items == []
    assert "из ваших продуктов" in plan.warnings[0]


def test_empty_home_gives_no_dishes(db):
    add_recipe(db, "Блюдо")
    plan = build_plan(db, request(only_from_stock=True))
    assert plan.items == []
    assert "Добавьте продукты" in plan.warnings[0]


def test_seasoning_to_taste_does_not_block_but_real_product_does(db):
    ok = add_recipe(db, "С перцем", [("Морковь", 100, "г"), ("Лук", 100, "г"), ("Перец чёрный", None, None)])
    add_recipe(db, "Со сметаной", [("Морковь", 100, "г"), ("Лук", 100, "г"), ("Сметана", None, None)])
    add_item(db, "Морковь", 1, "кг")
    add_item(db, "Лук", 1, "кг")
    assert plan_ids(db, only_from_stock=True) == [ok]


def test_seasoning_with_exact_amount_is_required(db):
    add_recipe(db, "Перечное", [("Морковь", 100, "г"), ("Перец чёрный", 20, "г")])
    add_item(db, "Морковь", 1, "кг")
    assert plan_ids(db, only_from_stock=True) == []


def test_stock_runs_out_during_plan(db):
    for number in range(3):
        add_recipe(db, f"Блюдо {number}", [("Мука", 400, "г"), ("Соль", None, None)])
    add_item(db, "Мука", 1, "кг")
    plan = build_plan(db, request(days=3, only_from_stock=True))
    assert len(plan.items) == 2
    assert "подобрано 2 из 3" in plan.warnings[0]


def test_any_cookable_dish_fills_any_meal(db):
    bread = add_recipe(db, "Лепёшка", [("Мука", 100, "г"), ("Соль", None, None)], category="Другое")
    add_item(db, "Мука", 1, "кг")
    plan = build_plan(db, request(only_from_stock=True, meals_per_day=3))
    assert plan.items[0].recipe_id == bread


def test_sauce_is_still_not_a_meal(db):
    add_recipe(db, "Заправка", [("Мука", 100, "г"), ("Соль", None, None)], category="Соусы")
    add_item(db, "Мука", 1, "кг")
    assert plan_ids(db, only_from_stock=True) == []


def test_few_dishes_may_repeat_after_short_gap(db):
    add_recipe(db, "Первое", [("Мука", 100, "г"), ("Соль", None, None)], category="Другое")
    add_recipe(db, "Второе", [("Рис", 100, "г"), ("Соль", None, None)], category="Другое")
    add_item(db, "Мука", 5, "кг")
    add_item(db, "Рис", 5, "кг")
    plan = build_plan(db, request(days=6, only_from_stock=True))
    last = {}
    for item in plan.items:
        if item.recipe_id in last:
            assert item.day - last[item.recipe_id] >= 3
        last[item.recipe_id] = item.day
    assert len(plan.items) == 4


def test_strict_plan_needs_no_shopping(db):
    add_recipe(db, "Блюдо", [("Морковь", 100, "г"), ("Перец чёрный", None, None)])
    add_item(db, "Морковь", 1, "кг")
    assert shopping(db, only_from_stock=True)[1] == []


def test_replacement_in_strict_plan_needs_no_purchase(db):
    add_recipe(db, "Первое", [("Морковь", 100, "г"), ("Соль", None, None)])
    add_recipe(db, "Второе", [("Лук", 100, "г"), ("Соль", None, None)])
    add_recipe(db, "Покупное", [("Говядина", 100, "г"), ("Соль", None, None)])
    add_item(db, "Морковь", 1, "кг")
    add_item(db, "Лук", 1, "кг")
    plan = saved(db, only_from_stock=True)
    titles = {option.title for option in replacements_for(db, plan, plan.items[0], limit=10)}
    assert "Покупное" not in titles
    assert titles


def test_generator_uses_strict_mode_by_default(app_db):
    with_db(app_db, lambda conn: fill_mains(conn, 5))
    with_db(app_db, lambda conn: conn.execute("UPDATE kitchen_equipment SET is_available = 1"))
    app = app_page("generator.py")
    button(app, "Составить план").click().run()
    assert not app.exception
    assert "из ваших продуктов" in text_of(app)


def test_white_flour_at_home_covers_recipe_with_wheat_flour(tmp_path):
    from contextlib import closing

    from core.ingredient_matcher import IngredientMatcher, find_ingredient, ingredient_key
    from core.inventory_manager import add_item
    from core.stock import Stock
    from database.database import connect, init_db

    path = tmp_path / "flour.db"
    init_db(path, tmp_path / "backups")
    with closing(connect(path)) as conn:
        IngredientMatcher(conn)  # заводит «Мука пшеничная»
        conn.execute(
            "INSERT OR IGNORE INTO ingredients (name, normalized_name) VALUES (?, ?)",
            ("Мука белая", ingredient_key("Мука белая")),
        )
        add_item(conn, "Мука белая", 1000, "г")
        wheat = find_ingredient(conn, "Мука пшеничная")
        assert Stock.load(conn).available_grams(wheat) == 1000


@pytest.mark.parametrize(
    "title, is_preparation",
    [
        ("Соус бешамель", True),
        ("Простое тесто для хлеба", True),
        ("Сухая смесь специй", True),
        ("Курица в сливочном соусе", False),
        ("Фаршированный перец", False),
        ("Яичница, политая маслом", False),
        ("Грибной крем-суп", False),
    ],
)
def test_only_preparations_are_not_meals(title, is_preparation):
    from core.meal_planner import NOT_A_MEAL_RE

    assert bool(NOT_A_MEAL_RE.search(title)) is is_preparation

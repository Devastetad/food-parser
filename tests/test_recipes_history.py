from contextlib import closing
from datetime import date, timedelta

import pytest
from streamlit.testing.v1 import AppTest

import config
from core.history_manager import (
    HistoryError,
    cooked_recipe_ids,
    cooked_summary,
    delete_entry,
    list_history,
    record_cooked,
    update_entry,
)
from core.ingredient_matcher import IngredientMatcher, find_ingredient
from core.inventory_manager import add_item, get_item, list_finished, list_items
from core.recipe_manager import Recipe, RecipeLine, get_recipe, load_recipes, search_recipes
from core.stock import Batch, LineStatus, Stock
from core.units import grams_per_unit, to_grams
from database.database import connect

TODAY = date(2026, 11, 5)


@pytest.fixture
def db(conn):
    IngredientMatcher(conn)
    return conn


def make_recipe(conn, title="Суп", lines=(("Морковь", 200, "г"),), servings=2, minutes=30,
                category="Супы", equipment=()):  # fmt: skip
    recipe_id = conn.execute(
        "INSERT INTO recipes (title, servings, cook_time_min, category) VALUES (?, ?, ?, ?)",
        (title, servings, minutes, category),
    ).lastrowid
    for name, quantity, unit in lines:
        ingredient_id = find_ingredient(conn, name)
        if ingredient_id is None:
            ingredient_id = IngredientMatcher(conn).resolve(name)
        conn.execute(
            "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) "
            "VALUES (?, ?, ?, ?)",
            (recipe_id, ingredient_id, quantity, unit),
        )
    conn.execute(
        "INSERT INTO cooking_instructions (recipe_id, step_number, description) "
        "VALUES (?, 1, 'Варить')",
        (recipe_id,),
    )
    for name in equipment:
        conn.execute(
            "INSERT INTO recipe_equipment (recipe_id, equipment_id) "
            "SELECT ?, id FROM kitchen_equipment WHERE name = ?",
            (recipe_id, name),
        )
    return recipe_id


def statuses(conn, recipe_id, factor=1):
    recipe = get_recipe(conn, recipe_id)
    return [check.status for check in Stock.load(conn).check_recipe(recipe, factor)]


# --- Единицы для расчётов ---


@pytest.mark.parametrize(
    "quantity, unit, name, grams",
    [
        (1, "кг", "", 1000),
        (2, "шт", "Лук репчатый", 200),
        (3, "шт", "Яйцо", 165),
        (2, "шт", "Неизвестно", 200),
        (2, "ст. л.", "Масло", 30),
        (1, "стакан", "Вода", 200),
        (4, "зубчик", "Чеснок", 20),
        (1, "л", "Молоко", 1000),
    ],
)
def test_to_grams(quantity, unit, name, grams):
    assert to_grams(quantity, unit, name) == grams


def test_unknown_unit_has_default_weight():
    assert grams_per_unit(None) == 100


# --- Хватит ли продуктов ---


def test_spec_example_500g_needed_200g_at_home(db):
    """Пример из ТЗ: рецепту нужно 500 г курицы, дома 200 г."""
    recipe_id = make_recipe(db, lines=[("Курица", 500, "г")])
    add_item(db, "Курица", 200, "г")

    [check] = Stock.load(db).check_recipe(get_recipe(db, recipe_id))

    assert check.status is LineStatus.PARTIAL
    assert check.missing_quantity == 300


def test_enough_when_quantity_covers(db):
    recipe_id = make_recipe(db, lines=[("Морковь", 200, "г")])
    add_item(db, "Морковь", 2, "шт")
    assert statuses(db, recipe_id) == [LineStatus.ENOUGH]


def test_nothing_at_home_is_missing(db):
    assert statuses(db, make_recipe(db)) == [LineStatus.MISSING]


def test_finished_batch_is_not_counted(db):
    from core.inventory_manager import mark_finished

    recipe_id = make_recipe(db)
    mark_finished(db, add_item(db, "Морковь", 1, "кг"))
    assert statuses(db, recipe_id) == [LineStatus.MISSING]


def test_portions_scale_the_requirement(db):
    recipe_id = make_recipe(db, lines=[("Морковь", 200, "г")])
    add_item(db, "Морковь", 300, "г")
    assert statuses(db, recipe_id, factor=1) == [LineStatus.ENOUGH]
    assert statuses(db, recipe_id, factor=2) == [LineStatus.PARTIAL]


def test_to_taste_needs_only_presence(db):
    recipe_id = make_recipe(db, lines=[("Соль", None, None)])
    assert statuses(db, recipe_id) == [LineStatus.MISSING]
    add_item(db, "Соль", 1, "г")
    assert statuses(db, recipe_id) == [LineStatus.ENOUGH]


def test_water_is_always_available(db):
    assert statuses(db, make_recipe(db, lines=[("Вода", 2, "л")])) == [LineStatus.ENOUGH]


def test_generic_recipe_line_is_covered_by_specific_product(db):
    recipe_id = make_recipe(db, lines=[("Масло", 50, "г")])
    add_item(db, "Масло сливочное", 100, "г")
    assert statuses(db, recipe_id) == [LineStatus.ENOUGH]


def test_specific_line_is_not_covered_by_other_specific(db):
    recipe_id = make_recipe(db, lines=[("Масло сливочное", 50, "г")])
    add_item(db, "Масло растительное", 100, "г")
    assert statuses(db, recipe_id) == [LineStatus.MISSING]


def test_batches_are_summed(db):
    recipe_id = make_recipe(db, lines=[("Морковь", 300, "г")])
    add_item(db, "Морковь", 100, "г", TODAY)
    add_item(db, "Морковь", 200, "г")
    assert statuses(db, recipe_id) == [LineStatus.ENOUGH]


def test_take_uses_earliest_expiry_first():
    batches = [
        Batch(1, 7, "Без срока", "г", 100, 1, None),
        Batch(2, 7, "Позже", "г", 100, 1, TODAY + timedelta(days=9)),
        Batch(3, 7, "Раньше", "г", 100, 1, TODAY),
    ]
    stock = Stock(batches)
    taken = stock.take(7, 150)
    assert [(batch.name, grams) for batch, grams in taken] == [("Раньше", 100), ("Позже", 50)]
    assert stock.available_grams(7) == 150


def test_copy_does_not_touch_original():
    stock = Stock([Batch(1, 7, "Рис", "г", 100, 1, None)])
    stock.copy().take(7, 100)
    assert stock.available_grams(7) == 100


# --- Порции и поиск ---


def test_factor_uses_recipe_servings():
    recipe = Recipe(1, "Х", None, None, False, 2)
    assert recipe.factor(6) == 3


def test_unknown_servings_use_default():
    recipe = Recipe(1, "Х", None, None, False, None)
    assert recipe.base_servings == config.DEFAULT_SERVINGS
    assert recipe.factor(config.DEFAULT_SERVINGS * 2) == 2


def recipe_with(title, line="Соль", minutes=30, equipment=(), category="Супы"):
    return Recipe(1, title, category, minutes, False, 2, lines=[RecipeLine(1, line, 1, "г")],
                  equipment=list(equipment))  # fmt: skip


@pytest.mark.parametrize(
    "text, found",
    [("борщ", True), ("БОРЩ", True), ("свекла", True), ("свёкла", True), ("плов", False), ("", True)],
)
def test_search_by_title_and_ingredient_ignores_case_and_yo(text, found):
    assert bool(search_recipes([recipe_with("Борщ", "Свёкла")], text)) is found


def test_search_filters():
    quick = recipe_with("Быстрый", minutes=10, equipment=["Плита"])
    slow = recipe_with("Долгий", minutes=90, category="Выпечка", equipment=["Духовка"])
    unknown = recipe_with("Без времени", minutes=None)
    recipes = [quick, slow, unknown]
    assert search_recipes(recipes, max_minutes=30) == [quick]
    assert search_recipes(recipes, category="Выпечка") == [slow]
    assert search_recipes(recipes, equipment={"Плита"}) == [quick, unknown]


def test_load_recipes_includes_lines_and_equipment(db):
    recipe_id = make_recipe(db, equipment=["Плита"])
    recipe = load_recipes(db, with_steps=True)[recipe_id]
    assert [line.name for line in recipe.lines] == ["Морковь"]
    assert recipe.equipment == ["Плита"]
    assert [step.text for step in recipe.steps] == ["Варить"]


def test_get_missing_recipe(db):
    assert get_recipe(db, 999) is None


# --- Списание ---


def test_deduction_plan_scales_with_portions(db):
    recipe_id = make_recipe(db, lines=[("Морковь", 200, "г")], servings=2)
    item_id = add_item(db, "Морковь", 1, "кг")
    [deduction] = Stock.load(db).plan_deduction(get_recipe(db, recipe_id), 4)
    assert deduction.inventory_id == item_id
    assert deduction.amount == pytest.approx(0.4)
    assert deduction.unit == "кг"
    assert deduction.available == 1


def test_deduction_takes_earliest_batch_and_merges_same_product(db):
    recipe_id = make_recipe(db, lines=[("Морковь", 100, "г"), ("Морковь", 50, "г")])
    soon = add_item(db, "Морковь", 200, "г", TODAY)
    add_item(db, "Морковь", 200, "г", TODAY + timedelta(days=5))
    deductions = Stock.load(db).plan_deduction(get_recipe(db, recipe_id), 2)
    assert [(item.inventory_id, item.amount) for item in deductions] == [(soon, 150)]


def test_deduction_skips_to_taste_and_water(db):
    recipe_id = make_recipe(db, lines=[("Соль", None, None), ("Вода", 1, "л")])
    add_item(db, "Соль", 100, "г")
    assert Stock.load(db).plan_deduction(get_recipe(db, recipe_id), 2) == []


def test_deduction_is_limited_by_stock(db):
    recipe_id = make_recipe(db, lines=[("Морковь", 500, "г")])
    add_item(db, "Морковь", 200, "г")
    [deduction] = Stock.load(db).plan_deduction(get_recipe(db, recipe_id), 2)
    assert deduction.amount == 200


# --- История ---


def test_recipe_in_database_is_not_cooked_until_recorded(db):
    recipe_id = make_recipe(db)
    assert cooked_recipe_ids(db) == set()
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    assert cooked_recipe_ids(db) == {recipe_id}


def test_record_saves_all_fields(db):
    recipe_id = make_recipe(db, title="Борщ")
    record_cooked(db, recipe_id, TODAY, 3, "liked", "  вкусно ", today=TODAY)
    [entry] = list_history(db)
    assert (entry.title, entry.cooked_on, entry.servings) == ("Борщ", TODAY, 3)
    assert (entry.rating, entry.comment) == ("liked", "вкусно")


def test_record_deducts_confirmed_amount(db):
    recipe_id = make_recipe(db)
    item_id = add_item(db, "Морковь", 500, "г")
    record_cooked(db, recipe_id, TODAY, 2, deductions=[(item_id, 120)], today=TODAY)
    assert get_item(db, item_id).quantity == 380


def test_full_deduction_marks_product_finished(db):
    recipe_id = make_recipe(db)
    item_id = add_item(db, "Морковь", 500, "г")
    record_cooked(db, recipe_id, TODAY, 2, deductions=[(item_id, 500)], today=TODAY)
    assert list_items(db) == []
    assert [item.id for item in list_finished(db)] == [item_id]


def test_zero_deduction_changes_nothing(db):
    recipe_id = make_recipe(db)
    item_id = add_item(db, "Морковь", 500, "г")
    record_cooked(db, recipe_id, TODAY, 2, deductions=[(item_id, 0)], today=TODAY)
    assert get_item(db, item_id).quantity == 500


def test_too_large_deduction_rolls_everything_back(db):
    recipe_id = make_recipe(db)
    first = add_item(db, "Морковь", 500, "г")
    second = add_item(db, "Лук", 100, "г")
    with pytest.raises(HistoryError, match="меньше"):
        record_cooked(db, recipe_id, TODAY, 2, deductions=[(first, 100), (second, 200)], today=TODAY)
    assert list_history(db) == []
    assert get_item(db, first).quantity == 500


@pytest.mark.parametrize(
    "cooked_on, servings, rating, message",
    [
        (TODAY + timedelta(days=1), 2, None, "не наступила"),
        (TODAY, 0, None, "порций"),
        (TODAY, 2, "супер", "оценка"),
    ],
)
def test_invalid_record_is_rejected(db, cooked_on, servings, rating, message):
    with pytest.raises(HistoryError, match=message):
        record_cooked(db, make_recipe(db), cooked_on, servings, rating, today=TODAY)
    assert list_history(db) == []


def test_missing_recipe_is_rejected(db):
    with pytest.raises(HistoryError, match="не найден"):
        record_cooked(db, 999, TODAY, 2, today=TODAY)


def test_long_comment_is_rejected(db):
    with pytest.raises(HistoryError, match="длиннее"):
        record_cooked(db, make_recipe(db), TODAY, 2, comment="х" * 1001, today=TODAY)


def test_rating_can_be_changed_and_cleared(db):
    entry_id = record_cooked(db, make_recipe(db), TODAY, 2, "ok", today=TODAY)
    update_entry(db, entry_id, "tired", "надоело")
    assert list_history(db)[0].rating == "tired"
    update_entry(db, entry_id, None, None)
    assert list_history(db)[0].rating is None


def test_delete_entry_makes_recipe_new_again(db):
    recipe_id = make_recipe(db)
    entry_id = record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    delete_entry(db, entry_id)
    assert cooked_recipe_ids(db) == set()


def test_summary_counts_and_keeps_last_known_rating(db):
    recipe_id = make_recipe(db)
    record_cooked(db, recipe_id, TODAY - timedelta(days=9), 2, "liked", today=TODAY)
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    summary = cooked_summary(db)[recipe_id]
    assert (summary.times, summary.last_cooked, summary.last_rating) == (2, TODAY, "liked")


def test_history_is_newest_first(db):
    recipe_id = make_recipe(db)
    record_cooked(db, recipe_id, TODAY - timedelta(days=5), 2, today=TODAY)
    record_cooked(db, recipe_id, TODAY, 2, today=TODAY)
    assert [entry.cooked_on for entry in list_history(db)] == [TODAY, TODAY - timedelta(days=5)]


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


def page(name):
    return str(config.BASE_DIR / "interface" / "pages" / name)


def run(name, state=None):
    app = AppTest.from_file(page(name), default_timeout=30)
    for key, value in (state or {}).items():
        app.session_state[key] = value
    app.run()
    assert not app.exception
    return app


def all_text(app):
    groups = (app.markdown, app.caption, app.subheader, app.title)
    return " ".join(item.value for group in groups for item in group)


def with_db(db_path, action):
    with closing(connect(db_path)) as conn:
        return action(conn)


def test_recipe_list_and_search(app_db):
    with_db(app_db, lambda conn: make_recipe(conn, title="Борщ"))
    with_db(app_db, lambda conn: make_recipe(conn, title="Плов"))
    app = run("recipes.py")
    assert "Найдено 2 рецепта" in all_text(app)

    app.text_input[0].set_value("борщ").run()

    assert "Найдено 1 рецепт" in all_text(app)


def test_recipe_list_empty_result(app_db):
    app = run("recipes.py")
    app.text_input[0].set_value("нет такого").run()
    assert "Ничего не нашлось" in all_text(app)


def test_card_shows_stock_status_and_scales(app_db):
    recipe_id = with_db(app_db, lambda conn: make_recipe(conn, lines=[("Морковь", 200, "г")], servings=2))
    with_db(app_db, lambda conn: add_item(conn, "Морковь", 300, "г"))

    app = run("recipes.py", {"open_recipe": recipe_id})
    assert "Морковь — 200 г" in all_text(app)
    assert "есть" in all_text(app)

    app.number_input[0].set_value(4).run()

    assert "Морковь — 400 г" in all_text(app)
    assert "не хватает 100 г" in all_text(app)


def test_card_of_missing_recipe(app_db):
    assert "не найден" in run("recipes.py", {"open_recipe": 999}).error[0].value


def test_card_says_when_servings_are_unknown(app_db):
    recipe_id = with_db(app_db, lambda conn: make_recipe(conn, servings=None))
    assert "не указано" in all_text(run("recipes.py", {"open_recipe": recipe_id}))


def test_cooked_flow_records_and_deducts(app_db):
    recipe_id = with_db(app_db, lambda conn: make_recipe(conn, lines=[("Морковь", 200, "г")], servings=2))
    item_id = with_db(app_db, lambda conn: add_item(conn, "Морковь", 500, "г"))

    app = run("recipes.py", {"open_recipe": recipe_id})
    next(button for button in app.button if button.label == "Сохранить в историю").click().run()

    assert not app.exception
    assert [entry.recipe_id for entry in with_db(app_db, list_history)] == [recipe_id]
    assert with_db(app_db, lambda conn: get_item(conn, item_id)).quantity == 300


def test_cooked_flow_uses_corrected_amount(app_db):
    recipe_id = with_db(app_db, lambda conn: make_recipe(conn, lines=[("Морковь", 200, "г")], servings=2))
    item_id = with_db(app_db, lambda conn: add_item(conn, "Морковь", 500, "г"))

    app = run("recipes.py", {"open_recipe": recipe_id})
    app.number_input[1].set_value(50)
    next(button for button in app.button if button.label == "Сохранить в историю").click().run()

    assert with_db(app_db, lambda conn: get_item(conn, item_id)).quantity == 450


def test_history_page_changes_rating_and_deletes(app_db):
    recipe_id = with_db(app_db, lambda conn: make_recipe(conn, title="Борщ"))
    with_db(app_db, lambda conn: record_cooked(conn, recipe_id, date.today(), 2, "ok", "соли мало"))

    app = run("history.py")
    assert "Борщ" in all_text(app) and "соли мало" in all_text(app)

    app.selectbox[0].set_value("liked").run()
    assert with_db(app_db, list_history)[0].rating == "liked"

    next(button for button in app.button if button.label == "Удалить").click().run()
    assert with_db(app_db, list_history) == []

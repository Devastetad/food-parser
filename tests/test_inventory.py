from datetime import date, timedelta

import pytest

from core.equipment_manager import (
    EquipmentError,
    add_equipment,
    delete_equipment,
    list_equipment,
    set_available,
)
from core.ingredient_matcher import (
    IngredientMatcher,
    find_ingredient,
    generic_ingredient_ids,
    suggest_ingredients,
)
from core.inventory_manager import (
    InventoryError,
    add_item,
    add_quick_items,
    count_recipes_with,
    delete_item,
    get_item,
    link_item,
    list_expiring,
    list_finished,
    list_items,
    list_unmatched,
    mark_finished,
    parse_quantity,
    parse_quick_list,
    restore_item,
    update_item,
)
from core.units import convert
from interface.components.shared import expiry_label, format_quantity, plural

TODAY = date(2026, 11, 5)


@pytest.fixture
def db(conn):
    """База с известными продуктами."""
    IngredientMatcher(conn)
    return conn


def add_recipe_with(conn, ingredient_id):
    recipe_id = conn.execute("INSERT INTO recipes (title) VALUES ('Блюдо')").lastrowid
    conn.execute(
        "INSERT INTO recipe_ingredients (recipe_id, ingredient_id) VALUES (?, ?)",
        (recipe_id, ingredient_id),
    )


# --- Единицы ---


@pytest.mark.parametrize(
    "quantity, source, target, expected",
    [
        (1, "кг", "г", 1000),
        (500, "г", "кг", 0.5),
        (1.5, "л", "мл", 1500),
        (2, "ст. л.", "мл", 30),
        (3, "ч. л.", "ст. л.", 1),
        (1, "стакан", "мл", 200),
        (7, "шт", "шт", 7),
        (1, "кг", "шт", None),
        (1, "кг", "л", None),
        (1, "шт", "упаковка", None),
    ],
)
def test_convert(quantity, source, target, expected):
    assert convert(quantity, source, target) == expected


# --- Добавление ---


def test_added_product_is_listed_and_linked_to_ingredient(db):
    item_id = add_item(db, "Лук", 3, "шт", TODAY)
    item = get_item(db, item_id)
    assert (item.name, item.quantity, item.unit, item.expires_on) == ("Лук", 3, "шт", TODAY)
    assert item.ingredient_name == "Лук репчатый"
    assert item.added_on == date.today()
    assert [row.id for row in list_items(db)] == [item_id]


@pytest.mark.parametrize(
    "name, quantity, unit, message",
    [
        ("", 1, "шт", "название"),
        ("   ", 1, "шт", "название"),
        (None, 1, "шт", "название"),
        ("х" * 101, 1, "шт", "длиннее"),
        ("Рис", 0, "г", "больше нуля"),
        ("Рис", -1, "г", "больше нуля"),
        ("Рис", float("nan"), "г", "числом"),
        ("Рис", float("inf"), "г", "числом"),
        ("Рис", None, "г", "числом"),
        ("Рис", 1, "ведро", "единица"),
    ],
)
def test_invalid_product_is_rejected(db, name, quantity, unit, message):
    with pytest.raises(InventoryError, match=message):
        add_item(db, name, quantity, unit)
    assert list_items(db) == []


def test_same_batch_is_topped_up(db):
    first = add_item(db, "Морковь", 2, "шт", TODAY)
    second = add_item(db, "морковки", 3, "шт", TODAY)
    assert first == second
    assert get_item(db, first).quantity == 5


def test_other_expiry_is_a_separate_batch(db):
    add_item(db, "Молоко", 1, "л", TODAY)
    add_item(db, "Молоко", 1, "л", TODAY + timedelta(days=7))
    add_item(db, "Молоко", 1, "л")
    assert len(list_items(db)) == 3


def test_other_unit_is_a_separate_batch(db):
    add_item(db, "Мука", 1, "кг")
    add_item(db, "Мука", 500, "г")
    assert len(list_items(db)) == 2


def test_unknown_products_with_one_name_are_topped_up(db):
    first = add_item(db, "Тамаринд", 1, "шт")
    assert add_item(db, "Тамаринд", 1, "шт") == first
    assert add_item(db, "Кумкват", 1, "шт") != first


def test_finished_batch_is_not_topped_up(db):
    first = add_item(db, "Рис", 1, "кг")
    mark_finished(db, first)
    assert add_item(db, "Рис", 1, "кг") != first


# --- Изменение ---


def test_quantity_is_changed(db):
    item_id = add_item(db, "Рис", 1, "кг")
    update_item(db, item_id, quantity=0.5)
    assert get_item(db, item_id).quantity == 0.5


def test_changing_unit_converts_quantity(db):
    item_id = add_item(db, "Рис", 1.5, "кг")
    update_item(db, item_id, unit="г")
    item = get_item(db, item_id)
    assert (item.quantity, item.unit) == (1500, "г")


def test_incompatible_unit_keeps_number(db):
    item_id = add_item(db, "Рис", 2, "кг")
    update_item(db, item_id, unit="упаковка")
    item = get_item(db, item_id)
    assert (item.quantity, item.unit) == (2, "упаковка")


def test_quantity_and_unit_together_are_taken_literally(db):
    item_id = add_item(db, "Рис", 1, "кг")
    update_item(db, item_id, quantity=300, unit="г")
    item = get_item(db, item_id)
    assert (item.quantity, item.unit) == (300, "г")


def test_expiry_is_set_and_cleared(db):
    item_id = add_item(db, "Рис", 1, "кг")
    update_item(db, item_id, expires_on=TODAY)
    assert get_item(db, item_id).expires_on == TODAY
    update_item(db, item_id, expires_on=None)
    assert get_item(db, item_id).expires_on is None


def test_unrelated_update_keeps_expiry(db):
    item_id = add_item(db, "Рис", 1, "кг", TODAY)
    update_item(db, item_id, quantity=2)
    assert get_item(db, item_id).expires_on == TODAY


def test_negative_update_is_rejected(db):
    item_id = add_item(db, "Рис", 1, "кг")
    with pytest.raises(InventoryError):
        update_item(db, item_id, quantity=-1)
    with pytest.raises(InventoryError):
        update_item(db, item_id, unit="ведро")
    assert get_item(db, item_id).quantity == 1


def test_zero_quantity_means_finished_and_keeps_old_amount(db):
    item_id = add_item(db, "Рис", 2, "кг")
    update_item(db, item_id, quantity=0)
    assert list_items(db) == []
    assert get_item(db, item_id).quantity == 2


def test_update_of_deleted_item_reports_it(db):
    item_id = add_item(db, "Рис", 1, "кг")
    delete_item(db, item_id)
    with pytest.raises(InventoryError, match="удалён"):
        update_item(db, item_id, quantity=2)


@pytest.mark.parametrize(
    "text, value", [("0.5", 0.5), ("0,5", 0.5), (" 2 ", 2), ("1000", 1000), ("1,25", 1.25)]
)
def test_quantity_accepts_comma_and_dot(text, value):
    assert parse_quantity(text) == value


@pytest.mark.parametrize("text", ["", "много", "1 кг", "1,2,3"])
def test_garbage_quantity_is_reported(text):
    with pytest.raises(InventoryError, match="не похоже"):
        parse_quantity(text)


# --- Закончившиеся и удаление ---


def test_finish_and_restore(db):
    item_id = add_item(db, "Рис", 2, "кг")

    mark_finished(db, item_id, TODAY)
    assert list_items(db) == []
    assert [item.finished_on for item in list_finished(db)] == [TODAY]

    restore_item(db, item_id)
    assert [item.quantity for item in list_items(db)] == [2]
    assert list_finished(db) == []


def test_delete_removes_item(db):
    item_id = add_item(db, "Рис", 1, "кг")
    delete_item(db, item_id)
    assert list_items(db) == [] and list_finished(db) == []


# --- Порядок и сроки ---


def test_list_is_sorted_by_expiry_with_no_expiry_last(db):
    add_item(db, "Соль", 1, "кг")
    add_item(db, "Молоко", 1, "л", TODAY + timedelta(days=5))
    add_item(db, "Сметана", 1, "шт", TODAY + timedelta(days=1))
    assert [item.name for item in list_items(db)] == ["Сметана", "Молоко", "Соль"]


def test_expiring_includes_overdue_and_soon_only(db):
    add_item(db, "Просрочено", 1, "шт", TODAY - timedelta(days=1))
    add_item(db, "Сегодня", 1, "шт", TODAY)
    add_item(db, "Граница", 1, "шт", TODAY + timedelta(days=3))
    add_item(db, "Потом", 1, "шт", TODAY + timedelta(days=4))
    add_item(db, "Без срока", 1, "шт")
    finished = add_item(db, "Закончилось", 1, "шт", TODAY)
    mark_finished(db, finished)
    names = [item.name for item in list_expiring(db, today=TODAY)]
    assert names == ["Просрочено", "Сегодня", "Граница"]


@pytest.mark.parametrize("offset, days", [(-2, -2), (0, 0), (3, 3)])
def test_days_left(db, offset, days):
    item = get_item(db, add_item(db, "Рис", 1, "кг", TODAY + timedelta(days=offset)))
    assert item.days_left(TODAY) == days


def test_days_left_without_expiry(db):
    assert get_item(db, add_item(db, "Рис", 1, "кг")).days_left(TODAY) is None


# --- Распознавание и обучение ---


def test_unknown_product_waits_for_link(db):
    item_id = add_item(db, "Гхи", 200, "г")
    assert [item.id for item in list_unmatched(db)] == [item_id]


def test_link_teaches_the_system(db):
    butter = find_ingredient(db, "масло сливочное")
    item_id = add_item(db, "Гхи", 200, "г")

    link_item(db, item_id, butter)

    assert list_unmatched(db) == []
    assert get_item(db, item_id).ingredient_name == "Масло сливочное"
    assert get_item(db, add_item(db, "гхи", 100, "г")).quantity == 300


def test_find_ingredient_creates_nothing(db):
    before = db.execute("SELECT count(*) FROM ingredients").fetchone()[0]
    assert find_ingredient(db, "неведомый продукт") is None
    assert db.execute("SELECT count(*) FROM ingredients").fetchone()[0] == before


def test_suggestions_by_shared_word_and_typo(db):
    assert "Масло сливочное" in [name for _, name in suggest_ingredients(db, "масло домашнее")]
    assert suggest_ingredients(db, "малоко")[0][1] == "Молоко"
    assert suggest_ingredients(db, "йцукенг") == []


def test_suggestions_are_limited(db):
    assert len(suggest_ingredients(db, "масло домашнее", limit=2)) == 2


# --- Общие названия ---


def test_butter_covers_generic_oil_but_not_other_way(db):
    matcher = IngredientMatcher(db)
    oil = matcher.resolve("масло")
    butter = find_ingredient(db, "масло сливочное")
    assert oil in generic_ingredient_ids(db, butter)
    assert generic_ingredient_ids(db, oil) == []


def test_recipe_count_includes_generic_names(db):
    matcher = IngredientMatcher(db)
    oil = matcher.resolve("масло")
    butter = find_ingredient(db, "масло сливочное")
    sunflower = find_ingredient(db, "масло растительное")
    add_recipe_with(db, oil)
    add_recipe_with(db, butter)
    add_recipe_with(db, sunflower)

    assert count_recipes_with(db, butter) == 2
    assert count_recipes_with(db, oil) == 1
    assert count_recipes_with(db, None) == 0


# --- Техника ---


def test_equipment_toggle(conn):
    stove = list_equipment(conn)[0]
    assert stove.is_available is False
    set_available(conn, stove.id, True)
    assert list_equipment(conn)[0].is_available is True


def test_added_equipment_is_available(conn):
    equipment_id = add_equipment(conn, "  Су-вид ")
    item = next(item for item in list_equipment(conn) if item.id == equipment_id)
    assert (item.name, item.is_available) == ("Су-вид", True)


@pytest.mark.parametrize("name", ["", "   ", None, "х" * 61, "плита", "ПЛИТА"])
def test_invalid_equipment_is_rejected(conn, name):
    with pytest.raises(EquipmentError):
        add_equipment(conn, name)


def test_unused_equipment_can_be_removed(conn):
    equipment_id = add_equipment(conn, "Су-вид")
    delete_equipment(conn, equipment_id)
    assert equipment_id not in [item.id for item in list_equipment(conn)]


def test_built_in_equipment_cannot_be_removed(conn):
    stove = list_equipment(conn)[0]
    assert stove.is_custom is False
    with pytest.raises(EquipmentError, match="Стандартный"):
        delete_equipment(conn, stove.id)


def test_custom_equipment_used_by_recipes_cannot_be_removed(conn):
    equipment_id = add_equipment(conn, "Су-вид")
    recipe_id = conn.execute("INSERT INTO recipes (title) VALUES ('Суп')").lastrowid
    conn.execute(
        "INSERT INTO recipe_equipment (recipe_id, equipment_id) VALUES (?, ?)",
        (recipe_id, equipment_id),
    )
    with pytest.raises(EquipmentError, match="нужен рецептам"):
        delete_equipment(conn, equipment_id)


# --- Форматирование ---


@pytest.mark.parametrize(
    "value, text", [(1.0, "1"), (1.5, "1,5"), (0.25, "0,25"), (1000.0, "1000"), (0.333, "0,33")]
)
def test_format_quantity(value, text):
    assert format_quantity(value) == text


@pytest.mark.parametrize(
    "number, word",
    [(1, "день"), (2, "дня"), (4, "дня"), (5, "дней"), (11, "дней"), (12, "дней"), (21, "день"), (22, "дня"), (111, "дней")],
)
def test_plural(number, word):
    assert plural(number, "день", "дня", "дней") == word


@pytest.mark.parametrize(
    "offset, text",
    [
        (-3, "Просрочено на 3 дня"),
        (-1, "Просрочено на 1 день"),
        (0, "Сегодня последний день"),
        (1, "Остался 1 день"),
        (2, "Осталось 2 дня"),
        (5, "Осталось 5 дней"),
        (21, "Остался 21 день"),
    ],
)
def test_expiry_label(offset, text):
    assert expiry_label(TODAY + timedelta(days=offset), TODAY) == text


def test_no_expiry_label():
    assert expiry_label(None) == ""


# --- Быстрое добавление списком ---


def quick(text):
    return [(item.name, item.quantity, item.unit) for item in parse_quick_list(text)]


def test_quick_list_example():
    assert quick("молоко 1 л, яйца 10, лук 1 кг") == [
        ("Молоко", 1, "л"), ("Яйцо", 10, "шт"), ("Лук", 1, "кг"),
    ]  # fmt: skip


@pytest.mark.parametrize(
    "text, expected",
    [
        ("мука 1,5 кг", [("Мука", 1.5, "кг")]),
        ("мука 1.5 кг", [("Мука", 1.5, "кг")]),
        ("сметана 200 г", [("Сметана", 200, "г")]),
        ("вода 2 л", [("Вода", 2, "л")]),
        ("яйца 12", [("Яйцо", 12, "шт")]),
        ("10 яиц", [("Яйца", 10, "шт")]),
        ("200 г муки", [("Мука", 200, "г")]),
        ("соль", [("Соль", 1, "шт")]),
        ("чеснок 3 зубчика", [("Чеснок", 3, "зубчик")]),
        ("корица: щепотка", [("Корица", 0.5, "г")]),
        ("масло сливочное 200 г", [("Масло сливочное", 200, "г")]),
    ],
)
def test_quick_line_formats(text, expected):
    assert quick(text) == expected


@pytest.mark.parametrize("separator", [", ", "; ", "\n", ",\n"])
def test_quick_list_separators(separator):
    assert len(quick(separator.join(["рис 1 кг", "лук 2 шт"]))) == 2


@pytest.mark.parametrize("text", ["", "   ", None, ",, ;", "\n\n"])
def test_quick_list_empty(text):
    assert quick(text) == []


def test_quick_items_are_added_and_recognized(db):
    assert add_quick_items(db, parse_quick_list("молоко 1 л, лук 1 кг")) == 2
    items = {item.name: item for item in list_items(db)}
    assert items["Лук"].ingredient_name == "Лук репчатый"
    assert items["Молоко"].quantity == 1


def test_quick_items_merge_with_existing_batch(db):
    add_item(db, "Рис", 1, "кг")
    add_quick_items(db, parse_quick_list("рис 2 кг"))
    assert [item.quantity for item in list_items(db)] == [3]


def test_quick_items_are_added_all_or_nothing(db):
    from core.inventory_manager import QuickItem

    with pytest.raises(InventoryError):
        add_quick_items(db, [QuickItem("Рис", 1, "кг"), QuickItem("Лук", 1, "ведро")])
    assert list_items(db) == []

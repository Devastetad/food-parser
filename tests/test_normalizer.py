import json
import sqlite3
from contextlib import closing
from dataclasses import asdict

import pytest

from core.ingredient_matcher import (
    IngredientMatcher,
    add_alias,
    ingredient_key,
    is_nominative,
    to_nominative,
)
from core.recipe_manager import import_raw_recipes
from database.database import connect, transaction
from parser import normalizer
from parser.base_parser import IngredientLine, RawRecipe
from parser.duplicate_checker import DuplicateIndex
from parser.normalizer import parse_ingredient, parse_minutes, parse_number, parse_step_minutes


def one(text):
    [parsed] = parse_ingredient(text)
    return parsed.name, parsed.quantity, parsed.unit


# --- Количество и единицы (строки взяты из реальных рецептов) ---


@pytest.mark.parametrize(
    "text, expected",
    [
        ("мука: 150 г", ("мука", 150, "г")),
        ("Мука пшеничная - 60 г", ("Мука пшеничная", 60, "г")),
        ("Баранина — 108 г", ("Баранина", 108, "г")),
        ("Говядина 160 грамм", ("Говядина", 160, "г")),
        ("шампиньоны - 100г", ("шампиньоны", 100, "г")),
        ("соль: 0,500 кг", ("соль", 500, "г")),
        ("фасоль стручковая зеленая 0.5 кг", ("фасоль стручковая зеленая", 500, "г")),
        ("молоко: 0,25 л", ("молоко", 250, "мл")),
        ("Сливки жирные - 100 мл", ("Сливки жирные", 100, "мл")),
        ("200 гр муки", ("муки", 200, "г")),
        ("50 г дрожжей", ("дрожжей", 50, "г")),
        ("2 ст. л. каперсов", ("каперсов", 2, "ст. л.")),
        ("1-2 ст.л. сливочного масла", ("сливочного масла", 1.5, "ст. л.")),
        ("паста карри: 2 ст. л", ("паста карри", 2, "ст. л.")),
        ("Какао-порошок - 1 ст. ложка", ("Какао-порошок", 1, "ст. л.")),
        ("соль 1 ч. л", ("соль", 1, "ч. л.")),
        ("3 столовые ложки сахара", ("сахара", 3, "ст. л.")),
        ("1 кофейная ложка соли", ("соли", 0.5, "ч. л.")),
        ("рис 0.5 стакана", ("рис", 0.5, "стакан")),
        ("1 стакан мелкопесочного сахара", ("мелкопесочного сахара", 1, "стакан")),
        ("2 чашки молока", ("молока", 2, "стакан")),
        ("Мандарин 4 шт", ("Мандарин", 4, "шт")),
        ("Яйцо куриное 3 штуки", ("Яйцо куриное", 3, "шт")),
        ("лук репчатый: 2-3 шт", ("лук репчатый", 2.5, "шт")),
        ("3–4 яйца", ("яйца", 3.5, "шт")),
        ("1 красный перец", ("красный перец", 1, "шт")),
        ("2 средних луковицы", ("луковицы", 2, "шт")),
        ("4 крупных зубка чеснока", ("чеснока", 4, "зубчик")),
        ("Перец черный молотый - 1-2 щепотки", ("Перец черный молотый", 1.5, "щепотка")),
        ("Соль - щепотка", ("Соль", 1, "щепотка")),
        ("мускатный орех: большая щепотка", ("мускатный орех", 1, "щепотка")),
        ("маргарин: 1 фунт", ("маргарин", 453.6, "г")),
        ("петрушка: 4 унции", ("петрушка", 113.4, "г")),
        ("соус эспаньоль: 0,75 галлона", ("соус эспаньоль", 3409.5, "мл")),
        ("уксус: 1 кварта", ("уксус", 1136, "мл")),
        ("2/3 банки сгущёнки", ("сгущёнки", 0.667, "банка")),
        ("½ стакана сахара", ("сахара", 0.5, "стакан")),
        ("1½ стакана воды", ("воды", 1.5, "стакан")),
        ("1 1/2 стакана воды", ("воды", 1.5, "стакан")),
        ("полтора стакана воды", ("воды", 1.5, "стакан")),
        ("пол стакана молока", ("молока", 0.5, "стакан")),
        ("один лимон", ("лимон", 1, "шт")),
        ("чеснок 5 г 4 дольки", ("чеснок", 5, "г")),
        ("миндаль жареный 5 г 20 г", ("миндаль жареный", 5, "г")),
        ("кервель: 20 г + 1 ст. л", ("кервель", 20, "г")),
        ("Мука пшеничная 76 76", ("Мука пшеничная", 76, "г")),
        ("говядина: 54", ("говядина", 54, "г")),
        ("лук: 2", ("лук", 2, "шт")),
        ("1 Лук жареный 2 ст. л.", ("Лук жареный", 2, "ст. л.")),
        ("1\\4 ч.л. корицы", ("корицы", 0.25, "ч. л.")),
    ],
)
def test_quantity_and_unit(text, expected):
    name, quantity, unit = one(text)
    assert (name, unit) == (expected[0], expected[2])
    assert quantity == pytest.approx(expected[1], abs=0.001)


@pytest.mark.parametrize(
    "text, name",
    [
        ("соль по вкусу", "соль"),
        ("Соль — по вкусу", "Соль"),
        ("Перец чёрный - по вкусу", "Перец чёрный"),
        ("перец", "перец"),
        ("Масло оливковое", "Масло оливковое"),
        ("жир для жарки", "жир"),
        ("Соль — по желанию", "Соль"),
    ],
)
def test_no_quantity(text, name):
    assert one(text) == (name, None, None)


def test_metric_in_parentheses_goes_to_note_and_imperial_is_converted():
    [parsed] = parse_ingredient("маргарин: 1 фунт (0,453 кг)")
    assert parsed.quantity == 453.6
    assert parsed.note == "0,453 кг"


def test_preparation_note_is_kept():
    [parsed] = parse_ingredient("морковь: 1 шт (мелко нарезанная)")
    assert (parsed.name, parsed.quantity, parsed.note) == ("морковь", 1, "мелко нарезанная")


def test_alternative_goes_to_note():
    [parsed] = parse_ingredient("солодовый уксус или экстракт тамаринда: 1 ст. л")
    assert parsed.name == "солодовый уксус"
    assert parsed.note == "или экстракт тамаринда"


def test_purpose_goes_to_note():
    [parsed] = parse_ingredient("вода для варки: 30 л")
    assert (parsed.name, parsed.quantity, parsed.unit) == ("вода", 30000, "мл")
    assert parsed.note == "для варки"


def test_description_after_comma_goes_to_note():
    [parsed] = parse_ingredient("1 ч.л. соды, гашеной уксусом")
    assert (parsed.name, parsed.note) == ("соды", "гашеной уксусом")


def test_range_at_start_of_line_is_quantity():
    assert one("0.5 - 1 кг мидий свежих") == ("мидий свежих", 750, "г")


@pytest.mark.parametrize(
    "text, names",
    [
        ("Перец чёрный молотый и соль - по вкусу", ["Перец чёрный молотый", "соль"]),
        ("соль, перец", ["соль", "перец"]),
        ("Соль, перец по вкусу", ["Соль", "перец"]),
        ("соль и перец", ["соль", "перец"]),
        ("соль, перец чёрный молотый, оливковое масло", ["соль", "перец чёрный молотый", "оливковое масло"]),
    ],
)
def test_several_products_in_one_line(text, names):
    assert [item.name for item in parse_ingredient(text)] == names


@pytest.mark.parametrize(
    "text",
    ["2465 1387 985", "5325 850 230", "На 10 порций взять", ")", "—", "время приготовления 3 часа"],
)
def test_table_noise_gives_no_ingredient(text):
    assert parse_ingredient(text) == []


def test_size_word_with_comma_is_not_a_product():
    [parsed] = parse_ingredient("1 маленький, мелко нарезанный зубчик чеснока")
    assert parsed.quantity == 1
    assert ingredient_key(parsed.name) == ingredient_key("зубчик чеснока")


def test_zero_quantity_is_dropped():
    assert one("соль: 0 г") == ("соль", None, None)


def test_broken_fraction_does_not_crash():
    assert one("1/0 стакана воды") == ("воды", None, None)


@pytest.mark.parametrize("text, value", [("1,5", 1.5), ("1/2", 0.5), ("2 1/2", 2.5), ("¾", 0.75), ("два", 2)])
def test_parse_number(text, value):
    assert parse_number(text) == value


# --- Время ---


@pytest.mark.parametrize(
    "text, minutes",
    [
        ("1 час", 60),
        ("40 мин", 40),
        ("3 мин.", 3),
        ("1.5 часа", 90),
        ("2,5 часа", 150),
        ("2 часа 20 минут", 140),
        ("30-40 минут", 40),
        ("10—20 минут", 20),
        ("1 - 1.5 часа", 90),
        ("40 минут - 1 час", 60),
        ("40 минут -1 час", 60),
        ("подготовка: 45 минут выпечка: 3-4 часа", 285),
        ("подготовка 10 мин маринование 7 дней", 10090),
        ("быстро", None),
        (None, None),
        ("", None),
    ],
)
def test_parse_minutes(text, minutes):
    assert parse_minutes(text) == minutes


@pytest.mark.parametrize(
    "step, minutes",
    [
        ("Тушить 10—15 минут", 15),
        ("Варить 1 час, затем ещё 20 минут", 80),
        ("Посолить", None),
        ("Помешивать каждые 5 минут", None),
        ("Разрезать на 2 части", None),
        ("Поставить на 2 минуты", 2),
        ("Выложить на дно 3 слоя", None),
    ],
)
def test_step_minutes(step, minutes):
    assert parse_step_minutes(step) == minutes


# --- Порции, сложность, калории ---


@pytest.mark.parametrize(
    "text, notes, servings",
    [
        ("4", [], 4),
        ("2-3", [], 2),
        ("4 порции", [], 4),
        ("2 (по 200 г)", [], 2),
        ("12-15 шт", [], None),
        ("5 банок по 1 л", [], None),
        ("(150 г)", [], None),
        (None, ["(на 100 человек; в рецепте указаны британские меры)"], 100),
        (None, ["(на 4 порции)"], 4),
        (None, ["(на одну порцию)"], 1),
        (None, ["Количество продуктов приведено из расчёта на одну порцию"], 1),
        (None, ["(на 5 стаканов соуса)"], None),
        (None, ["Выход 200 г"], None),
        (None, [], None),
    ],
)
def test_servings(text, notes, servings):
    assert normalizer.parse_servings(text, notes) == servings


def test_time_from_notes():
    assert normalizer.time_from_notes(["(на 100 человек, время приготовления 2 часа)"]) == 120
    assert normalizer.time_from_notes(["(на 4 порции)"]) is None


@pytest.mark.parametrize(
    "text, level",
    [("1", "easy"), ("2", "easy"), ("3", "medium"), ("4", "hard"), ("5", "hard"), ("9", None), (None, None)],
)
def test_difficulty(text, level):
    assert normalizer.parse_difficulty(text) == level


@pytest.mark.parametrize(
    "text, calories",
    [
        ("140 ккал", 140),
        ("530", 530),
        ("200-260 ккал", 230),
        ("80 — 100 ккал", 90),
        ("441 ккал/100 г", None),
        ("1 чашка - 50 Ккал", None),
        (None, None),
    ],
)
def test_calories_only_per_serving(text, calories):
    assert normalizer.parse_calories(text) == calories


# --- Категории и техника ---


@pytest.mark.parametrize(
    "category, title, expected",
    [
        ("Соусы", "Бешамель", "Соусы"),
        ("выпечка", "Пирог", "Выпечка"),
        ("Борщи", "Украинский", "Супы"),
        ("второе блюдо", "Х", "Вторые блюда"),
        ("Рагу", "Х", "Вторые блюда"),
        ("Торты, Вегетарианская кухня", "Х", "Десерты"),
        (None, "Соус Томат 3", "Соусы"),
        (None, "Мясо в соусе", "Вторые блюда"),
        (None, "Суп из фасоли", "Супы"),
        (None, "Салат луковый", "Салаты"),
        ("Французская кухня", "Луковый суп 2", "Супы"),
        (None, "Бланманже", "Другое"),
    ],
)
def test_category(category, title, expected):
    assert normalizer.normalize_category(category, title) == expected


def test_dish_with_meat_and_unknown_name_is_a_main_course():
    assert normalizer.normalize_category(None, "Бёф бургиньон", ["говядина", "вино"]) == "Вторые блюда"


def test_cuisine_uses_first_and_capitalizes():
    assert normalizer.normalize_cuisine("татарская кухня, башкирская кухня") == "Татарская кухня"
    assert normalizer.normalize_cuisine(None) is None


@pytest.mark.parametrize(
    "steps, expected",
    [
        (["Обжарить лук на сковороде"], {"Плита"}),
        (["Варить до готовности"], {"Плита"}),
        (["Запекать при 180 градусах"], {"Духовка"}),
        (["Разогреть духовку", "Сварить рис"], {"Духовка", "Плита"}),
        (["Взбить блендером"], {"Блендер"}),
        (["Взбить миксером"], {"Миксер"}),
        (["Жарить на гриле"], {"Гриль", "Плита"}),
        (["Приготовить в аэрогриле"], {"Аэрогриль"}),
        (["Готовить в мультиварке"], {"Мультиварка"}),
        (["Греть в микроволновке"], {"Микроволновка"}),
        (["Смешать всё в миске"], set()),
    ],
)
def test_equipment_detection(steps, expected):
    assert normalizer.detect_equipment(steps) == expected


# --- Сопоставление ингредиентов ---


def test_spec_example_chicken_breast(conn):
    """Пример из ТЗ: три написания — один продукт."""
    matcher = IngredientMatcher(conn)
    ids = {matcher.resolve(name) for name in ["куриная грудка", "филе курицы", "chicken breast"]}
    assert len(ids) == 1


@pytest.mark.parametrize(
    "names",
    [
        ["лук репчатый", "репчатый лук", "лука репчатого", "луковицы", "лук", "onion"],
        ["мука", "муки", "пшеничная мука", "мука пшеничная"],
        ["яйцо", "яйца", "яиц", "яйцо куриное", "egg"],
        ["масло сливочное", "сливочного масла", "сливочное масло", "butter"],
        ["сахар", "сахара", "сахар-песок"],
        ["свёкла", "свекла", "свёклы"],
        ["перец чёрный", "перец черный", "чёрный перец", "перец чёрный молотый"],
        ["помидоры", "помидор", "томаты"],
        ["морковь свежая", "морковь", "моркови", "морковка"],
    ],
)
def test_spellings_of_one_product_match(conn, names):
    matcher = IngredientMatcher(conn)
    assert len({matcher.resolve(name) for name in names}) == 1


@pytest.mark.parametrize(
    "first, second",
    [
        ("молоко цельное", "молоко"),
        ("мука пшеничная в/с", "мука"),
        ("картофель отварной", "картофель"),
        ("яйцо варёное", "яйцо"),
        ("очищенная свекла", "свёкла"),
    ],
)
def test_state_words_do_not_split_product(conn, first, second):
    matcher = IngredientMatcher(conn)
    assert matcher.resolve(first) == matcher.resolve(second)


@pytest.mark.parametrize(
    "first, second",
    [
        ("огурцы солёные", "огурец"),
        ("молоко сухое", "молоко"),
        ("масло сливочное", "масло растительное"),
        ("перец чёрный", "перец болгарский"),
        ("лук репчатый", "лук-порей"),
        ("куриная грудка", "курица"),
        ("мука пшеничная", "мука кукурузная"),
        ("яйцо", "яичный желток"),
    ],
)
def test_different_products_stay_different(conn, first, second):
    matcher = IngredientMatcher(conn)
    assert matcher.resolve(first) != matcher.resolve(second)


def test_key_ignores_case_order_and_descriptors():
    assert ingredient_key("Лука РЕПЧАТОГО") == ingredient_key("репчатый лук")
    assert ingredient_key("свежая мелко нарезанная кинза") == ingredient_key("кинза")


def test_mint_is_not_an_adjective(conn):
    matcher = IngredientMatcher(conn)
    mint = matcher.resolve("мята")
    assert mint == matcher.resolve("мяты")
    matcher.finish()
    assert conn.execute("SELECT name FROM ingredients WHERE id = ?", (mint,)).fetchone()[0] == "Мята"


def test_size_word_is_removed_from_shown_name(conn):
    matcher = IngredientMatcher(conn)
    ingredient_id = matcher.resolve("небольшие кумкваты")
    matcher.finish()
    assert conn.execute(
        "SELECT name FROM ingredients WHERE id = ?", (ingredient_id,)
    ).fetchone()[0] == "Кумкваты"


def test_key_of_only_descriptors_is_not_empty():
    assert ingredient_key("свежий") != ""


def test_new_product_gets_created_once(conn):
    matcher = IngredientMatcher(conn)
    assert matcher.resolve("тамаринд") == matcher.resolve("тамаринда")
    assert conn.execute(
        "SELECT count(*) FROM ingredients WHERE normalized_name = 'тамаринд'"
    ).fetchone()[0] == 1


def test_learned_alias_is_remembered_by_next_matcher(conn):
    matcher = IngredientMatcher(conn)
    butter = matcher.find("масло сливочное")
    assert matcher.find("гхи") is None

    add_alias(conn, "гхи", butter)

    assert IngredientMatcher(conn).find("гхи") == butter


def test_alias_can_be_corrected(conn):
    matcher = IngredientMatcher(conn)
    first, second = matcher.resolve("тамаринд"), matcher.resolve("кетчуп")
    add_alias(conn, "пряный соус", first)
    add_alias(conn, "пряный соус", second)
    assert IngredientMatcher(conn).find("пряный соус") == second


@pytest.mark.parametrize(
    "spelling, expected",
    [
        ("жидких свежих дрожжей", "жидкие свежие дрожжи"),
        ("мяты", "мята"),
        ("неосветлённой пшеничной муки высшего сорта", "неосветлённая пшеничная мука высшего сорта"),
        ("каперсов", "каперсы"),
        ("гвоздики", "гвоздика"),
    ],
)
def test_display_name_in_nominative(spelling, expected):
    assert to_nominative(spelling) == expected


def test_shown_name_prefers_nominative_spelling(conn):
    matcher = IngredientMatcher(conn)
    ingredient_id = matcher.resolve("каперсов")
    matcher.resolve("каперсов")
    matcher.resolve("каперсы")
    matcher.finish()
    assert conn.execute(
        "SELECT name FROM ingredients WHERE id = ?", (ingredient_id,)
    ).fetchone()[0] == "Каперсы"
    assert is_nominative("каперсы") and not is_nominative("муки")


def test_known_product_keeps_its_name(conn):
    matcher = IngredientMatcher(conn)
    ingredient_id = matcher.resolve("лука")
    matcher.finish()
    row = conn.execute("SELECT name, category FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    assert tuple(row) == ("Лук репчатый", "Овощи")


def test_default_unit_is_most_frequent(conn):
    matcher = IngredientMatcher(conn)
    for unit in ("г", "г", "шт"):
        ingredient_id = matcher.resolve("тамаринд", unit)
    matcher.finish()
    assert conn.execute(
        "SELECT default_unit FROM ingredients WHERE id = ?", (ingredient_id,)
    ).fetchone()[0] == "г"


# --- Дубли ---


def test_same_composition_and_title_is_duplicate():
    index = DuplicateIndex()
    composition = [(1, 100, "г"), (2, 1, "шт")]
    assert index.add_if_new("Суп", composition, ["Варить"]) is True
    assert index.add_if_new("суп", list(reversed(composition)), ["Сварить иначе"]) is False


def test_same_composition_and_similar_steps_is_duplicate():
    index = DuplicateIndex()
    composition = [(1, 100, "г")]
    index.add_if_new("Суп", composition, ["Варить на медленном огне двадцать минут до готовности"])
    assert index.add_if_new("Похлёбка", composition, ["Варить на медленном огне двадцать минут до готовности."]) is False


def test_same_composition_with_other_title_and_steps_is_not_duplicate():
    index = DuplicateIndex()
    composition = [(1, 100, "г")]
    index.add_if_new("Омлет", composition, ["Взбить яйца и пожарить"])
    assert index.add_if_new("Яйцо вкрутую", composition, ["Варить в кипятке десять минут"]) is True


def test_variants_with_other_quantities_are_not_duplicates():
    index = DuplicateIndex()
    assert index.add_if_new("Соус 1", [(1, 100, "г")], ["Смешать"]) is True
    assert index.add_if_new("Соус 1", [(1, 200, "г")], ["Смешать"]) is True


# --- Перенос в базу ---


def add_raw(conn, url="https://example.org/1", title="Суп", lines=("морковь: 2 шт", "соль"),
            steps=("Варить 20 минут",), **fields):  # fmt: skip
    recipe = RawRecipe(
        title=title,
        source_url=url,
        ingredients=[IngredientLine(text) for text in lines],
        steps=list(steps),
        **fields,
    )
    item_id = conn.execute(
        "INSERT INTO raw_items (source, item_id, revision, title, status) "
        "VALUES ('test', ?, '1', ?, 'parsed') "
        "ON CONFLICT (source, item_id) DO UPDATE SET title = excluded.title RETURNING id",
        (url, title),
    ).fetchone()[0]
    conn.execute("DELETE FROM raw_recipes WHERE raw_item_id = ?", (item_id,))
    conn.execute(
        "INSERT INTO raw_recipes (raw_item_id, variant, title, source_url, license, payload) "
        "VALUES (?, 1, ?, ?, 'CC BY-SA 4.0', ?)",
        (item_id, title, url, json.dumps(asdict(recipe), ensure_ascii=False)),
    )


def test_import_fills_all_tables(db_path, conn):
    add_raw(
        conn,
        lines=("морковь: 2 шт", "соль по вкусу"),
        steps=("Сварить морковь в кастрюле 20 минут", "Запечь в духовке 30 минут"),
        time_text="1 час",
        servings_text="4",
        difficulty_text="2",
        category="Супы",
        cuisine="русская кухня",
        calories_text="140 ккал",
        description="Простой суп",
        image_url="https://example.org/1.jpg",
    )

    stats = import_raw_recipes(db_path)

    assert stats.imported == 1
    recipe = conn.execute("SELECT * FROM recipes").fetchone()
    assert recipe["title"] == "Суп"
    assert recipe["category"] == "Супы"
    assert recipe["cuisine"] == "Русская кухня"
    assert recipe["cook_time_min"] == 60
    assert recipe["time_is_estimated"] == 0
    assert recipe["servings"] == 4
    assert recipe["difficulty"] == "easy"
    assert recipe["calories"] == 140
    assert recipe["license"] == "CC BY-SA 4.0"
    assert recipe["image_url"] == "https://example.org/1.jpg"

    lines = conn.execute(
        "SELECT i.name, r.quantity, r.unit, r.raw_text FROM recipe_ingredients r "
        "JOIN ingredients i ON i.id = r.ingredient_id ORDER BY r.id"
    ).fetchall()
    assert [tuple(line) for line in lines] == [
        ("Морковь", 2, "шт", "морковь: 2 шт"),
        ("Соль", None, None, "соль по вкусу"),
    ]
    steps = conn.execute(
        "SELECT step_number, duration_min FROM cooking_instructions ORDER BY step_number"
    ).fetchall()
    assert [tuple(step) for step in steps] == [(1, 20), (2, 30)]
    equipment = conn.execute(
        "SELECT e.name FROM recipe_equipment r JOIN kitchen_equipment e ON e.id = r.equipment_id"
    ).fetchall()
    assert {row[0] for row in equipment} == {"Плита", "Духовка"}


def test_time_from_steps_is_marked_estimated(db_path, conn):
    add_raw(conn, steps=("Варить 20 минут", "Запекать 1 час"))
    import_raw_recipes(db_path)
    recipe = conn.execute("SELECT cook_time_min, time_is_estimated FROM recipes").fetchone()
    assert tuple(recipe) == (80, 1)


def test_steps_without_time_add_to_estimate(db_path, conn):
    add_raw(conn, steps=("Слепить хинкали", "Варить 15 минут", "Подать"))
    import_raw_recipes(db_path)
    assert conn.execute("SELECT cook_time_min FROM recipes").fetchone()[0] == 25


def test_unknown_time_stays_empty(db_path, conn):
    add_raw(conn, steps=("Смешать",))
    import_raw_recipes(db_path)
    assert tuple(conn.execute("SELECT cook_time_min, time_is_estimated FROM recipes").fetchone()) == (None, 0)


def test_time_written_as_ingredient_line(db_path, conn):
    add_raw(conn, lines=("морковь: 2 шт", "время приготовления 3 часа"), steps=("Смешать",))
    import_raw_recipes(db_path)
    assert conn.execute("SELECT cook_time_min FROM recipes").fetchone()[0] == 180
    assert conn.execute("SELECT count(*) FROM recipe_ingredients").fetchone()[0] == 1


def test_unused_ingredients_are_removed_but_inventory_links_stay(db_path, conn):
    add_raw(conn, lines=("тамаринд: 10 г", "кумкват: 2 шт"))
    import_raw_recipes(db_path)
    kumquat = conn.execute("SELECT id FROM ingredients WHERE name = 'Кумкват'").fetchone()[0]
    conn.execute(
        "INSERT INTO inventory (name, ingredient_id, quantity, unit) VALUES ('Кумкват', ?, 1, 'шт')",
        (kumquat,),
    )
    add_raw(conn, lines=("морковь: 1 шт",))

    import_raw_recipes(db_path)

    names = {row[0] for row in conn.execute("SELECT name FROM ingredients")}
    assert "Тамаринд" not in names
    assert "Кумкват" in names
    assert "Соль" in names  # известные продукты остаются и без рецептов


def test_time_and_servings_from_notes(db_path, conn):
    add_raw(conn, ingredient_notes=["(на 100 человек, время приготовления 2 часа)"])
    import_raw_recipes(db_path)
    assert tuple(conn.execute("SELECT servings, cook_time_min FROM recipes").fetchone()) == (100, 120)


def test_ingredient_group_and_note_are_saved(db_path, conn):
    recipe = RawRecipe(
        title="Пирог",
        source_url="https://example.org/2",
        ingredients=[IngredientLine("мука: 500 г (просеять)", "Для теста")],
        steps=["Замесить тесто"],
    )
    item_id = conn.execute(
        "INSERT INTO raw_items (source, item_id, revision, title, status) "
        "VALUES ('test', '2', '1', 'Пирог', 'parsed')"
    ).lastrowid
    conn.execute(
        "INSERT INTO raw_recipes (raw_item_id, variant, title, source_url, license, payload) "
        "VALUES (?, 1, 'Пирог', ?, 'CC0', ?)",
        (item_id, recipe.source_url, json.dumps(asdict(recipe), ensure_ascii=False)),
    )
    import_raw_recipes(db_path)
    row = conn.execute("SELECT group_name, note FROM recipe_ingredients").fetchone()
    assert tuple(row) == ("Для теста", "просеять")


def test_reimport_adds_nothing_and_keeps_ids(db_path, conn):
    add_raw(conn)
    import_raw_recipes(db_path)
    recipe_id = conn.execute("SELECT id FROM recipes").fetchone()[0]
    counts = [
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("recipes", "ingredients", "recipe_ingredients", "cooking_instructions")
    ]

    import_raw_recipes(db_path)

    assert conn.execute("SELECT id FROM recipes").fetchone()[0] == recipe_id
    assert counts == [
        conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("recipes", "ingredients", "recipe_ingredients", "cooking_instructions")
    ]


def test_reimport_keeps_cooking_history(db_path, conn):
    add_raw(conn)
    import_raw_recipes(db_path)
    recipe_id = conn.execute("SELECT id FROM recipes").fetchone()[0]
    conn.execute(
        "INSERT INTO cooking_history (recipe_id, cooked_on, rating) VALUES (?, '2026-11-05', 'liked')",
        (recipe_id,),
    )
    add_raw(conn, title="Суп новый", steps=("Варить 25 минут",))

    import_raw_recipes(db_path)

    row = conn.execute(
        "SELECT r.title, h.rating FROM cooking_history h JOIN recipes r ON r.id = h.recipe_id"
    ).fetchone()
    assert tuple(row) == ("Суп новый", "liked")


def test_changed_composition_replaces_old_lines(db_path, conn):
    add_raw(conn, lines=("морковь: 2 шт", "соль"))
    import_raw_recipes(db_path)
    add_raw(conn, lines=("свёкла: 1 шт",))
    import_raw_recipes(db_path)
    names = conn.execute(
        "SELECT i.name FROM recipe_ingredients r JOIN ingredients i ON i.id = r.ingredient_id"
    ).fetchall()
    assert [row[0] for row in names] == ["Свёкла"]


def test_duplicate_is_not_imported(db_path, conn):
    add_raw(conn, url="https://example.org/1", title="Суп")
    add_raw(conn, url="https://example.org/2", title="Суп")
    stats = import_raw_recipes(db_path)
    assert (stats.imported, stats.duplicates) == (1, 1)
    assert conn.execute("SELECT count(*) FROM recipes").fetchone()[0] == 1


def test_recipe_without_usable_ingredients_is_skipped(db_path, conn):
    add_raw(conn, lines=("2465 1387 985",))
    stats = import_raw_recipes(db_path)
    assert (stats.imported, stats.without_ingredients) == (0, 1)


def test_spellings_in_different_recipes_become_one_ingredient(db_path, conn):
    add_raw(conn, url="https://example.org/1", title="Первый", lines=("лук репчатый: 1 шт",))
    add_raw(conn, url="https://example.org/2", title="Второй", lines=("2 луковицы",))
    add_raw(conn, url="https://example.org/3", title="Третий", lines=("50 г репчатого лука",))
    import_raw_recipes(db_path)
    used = conn.execute("SELECT count(DISTINCT ingredient_id) FROM recipe_ingredients").fetchone()[0]
    assert used == 1


def test_learned_alias_applies_on_reimport(db_path, conn):
    add_raw(conn, lines=("гхи: 50 г",))
    import_raw_recipes(db_path)
    butter = conn.execute("SELECT id FROM ingredients WHERE name = 'Масло сливочное'").fetchone()[0]

    add_alias(conn, "гхи", butter)
    import_raw_recipes(db_path)

    assert conn.execute("SELECT ingredient_id FROM recipe_ingredients").fetchone()[0] == butter


def test_failed_import_changes_nothing(db_path, conn):
    add_raw(conn, url="https://example.org/1", title="Хороший")
    add_raw(conn, url="https://example.org/2", title="Плохой")
    conn.execute(
        "UPDATE raw_recipes SET payload = '{}' WHERE title = 'Плохой'"
    )
    with pytest.raises(KeyError):
        import_raw_recipes(db_path)
    assert conn.execute("SELECT count(*) FROM recipes").fetchone()[0] == 0


def test_migration_constraint_on_estimated_flag(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO recipes (title, time_is_estimated) VALUES ('Х', 2)")

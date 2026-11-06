"""Сопоставление ингредиентов: «лука репчатого», «репчатый лук» и «onion» — один продукт.

Ключ названия — отсортированные начальные формы слов без описательных слов. Ключи
известных названий лежат в ingredients.normalized_name и ingredient_aliases, поэтому
каждая подтверждённая связка (add_alias) запоминается и применяется дальше.
"""
import re
import sqlite3
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from functools import lru_cache

import pymorphy3

# Слова, которые не меняют продукт: что купить и что лежит дома, от них не зависит
DESCRIPTORS = {
    "свежий", "свежемолотый", "крупный", "мелкий", "средний", "большой", "небольшой",
    "маленький", "спелый", "очищенный", "нарезать", "нарезанный", "тёртый", "тереть",
    "натёртый", "измельчённый", "рубленый", "молотый", "охлаждённый", "холодный", "тёплый",
    "горячий", "кипячёный", "сырой", "готовый", "хороший", "мелко", "крупно", "мягкий",
    "твёрдый", "толчёный", "промытый", "сушёный", "помол", "отварный", "отварной", "варёный",
    "очистить", "измельчить", "взбить", "постный", "жирный", "цельный", "пастеризованный",
    "отборный", "домашний", "обычный", "хороший", "в", "с",
}  # fmt: skip
DESCRIPTORS = {word.replace("ё", "е") for word in DESCRIPTORS}

# Формы, которые словарь считает отдельными словами, а не падежами продукта
LEMMA_FIXES = {"лука": "лук", "базилика": "базилик", "мята": "мята", "мяты": "мята"}

# Канонические продукты по категориям. Название слева показывается в интерфейсе.
KNOWN_INGREDIENTS = {
    "Мясо и птица": {
        "Куриная грудка": ["филе курицы", "куриное филе", "филе куриной грудки", "chicken breast"],
        "Курица": ["курятина", "кура", "тушка курицы", "цыплёнок", "chicken"],
        "Говядина": ["мякоть говядины", "говяжья вырезка", "beef"],
        "Свинина": ["мякоть свинины", "pork"],
        "Баранина": ["мякоть баранины", "lamb"],
        "Телятина": ["мякоть телятины"],
        "Фарш мясной": ["мясной фарш", "фарш"],
        "Индейка": ["филе индейки", "turkey"],
        "Бекон": ["bacon"],
    },
    "Рыба": {
        "Лосось": ["сёмга", "филе лосося", "salmon"],
        "Рыба": ["филе рыбы", "рыбное филе"],
    },
    "Овощи": {
        "Лук репчатый": ["лук", "луковица", "луковичка", "белый лук", "onion"],
        "Морковь": ["морковка", "carrot"],
        "Картофель": ["картошка", "клубни картофеля", "potato"],
        "Помидор": ["томат", "tomato"],
        "Огурец": ["cucumber"],
        "Чеснок": ["зубчик чеснока", "головка чеснока", "garlic"],
        "Капуста белокочанная": ["капуста", "белокочанная капуста", "cabbage"],
        "Свёкла": ["свекла", "бурак", "beet"],
        "Перец болгарский": ["сладкий перец", "болгарский перец", "перец сладкий", "bell pepper"],
        "Баклажан": ["синенькие", "eggplant"],
        "Кабачок": ["цукини", "zucchini"],
        "Шампиньоны": ["шампиньон", "mushroom"],
    },
    "Зелень и пряности": {
        "Петрушка": ["зелень петрушки", "петрушка зелень", "parsley"],
        "Укроп": ["зелень укропа", "dill"],
        "Мята": ["листья мяты", "mint"],
        "Базилик": ["зелень базилика", "basil"],
        "Кинза": ["кориандр зелень", "зелень кинзы", "cilantro"],
        "Лавровый лист": ["лавр", "лист лавровый", "bay leaf"],
        "Перец чёрный": ["чёрный перец", "перец чёрный горошком", "перец", "black pepper"],
        "Соль": ["соль поваренная", "поваренная соль", "морская соль", "salt"],
    },
    "Молочное и яйца": {
        "Яйцо": ["яйцо куриное", "куриное яйцо", "egg"],
        "Яичный желток": ["желток", "сырой желток", "egg yolk"],
        "Яичный белок": ["белок", "egg white"],
        "Сгущённое молоко": ["сгущёнка", "молоко сгущённое"],
        "Масло сливочное": ["сливочное масло", "butter"],
        "Молоко": ["коровье молоко", "milk"],
        "Сметана": ["sour cream"],
        "Сливки": ["сливка", "жирные сливки", "cream"],
        "Творог": ["cottage cheese"],
        "Сыр": ["cheese"],
    },
    "Бакалея": {
        "Мука пшеничная": ["мука", "пшеничная мука", "flour"],
        "Сахар": ["сахар-песок", "сахарный песок", "сахар песок", "sugar"],
        "Масло растительное": [
            "растительное масло", "подсолнечное масло", "масло подсолнечное", "vegetable oil",
        ],
        "Масло оливковое": ["оливковое масло", "olive oil"],
        "Рис": ["rice"],
        "Томатная паста": ["томат-паста", "томат-пюре", "томатное пюре", "tomato paste"],
        "Уксус": ["vinegar"],
        "Белое вино": ["вино белое сухое", "сухое белое вино"],
        "Красное вино": ["вино красное сухое", "сухое красное вино"],
        "Лимонный сок": ["сок лимона", "сок лимонов", "lemon juice"],
        "Вода": ["холодная вода", "кипяток", "water"],
    },
}  # fmt: skip


# Общие названия в рецептах и что их заменяет: если рецепт просит «масло», подойдёт любое
# из списка. Обратное неверно: рецепту со сливочным маслом подсолнечное не подходит.
GENERIC_INGREDIENTS = {
    "Масло": ["Масло сливочное", "Масло растительное", "Масло оливковое", "Масло топлёное"],
    "Жир": ["Масло сливочное", "Масло растительное", "Масло топлёное", "Смалец", "Сало"],
    "Мясо": ["Говядина", "Свинина", "Баранина", "Телятина", "Курица", "Индейка", "Куриная грудка"],
    "Рыба": ["Лосось"],
    "Зелень": ["Петрушка", "Укроп", "Кинза", "Базилик", "Мята"],
    "Мука пшеничная": [
        "Мука белая", "Мука высшего сорта", "Мука просеянная", "Мука пшеничная просеянная",
    ],
    "Мука белая": ["Мука пшеничная"],
}  # fmt: skip

MIN_SIMILARITY = 0.6


@lru_cache(maxsize=1)
def _morph() -> pymorphy3.MorphAnalyzer:
    return pymorphy3.MorphAnalyzer()


@lru_cache(maxsize=None)
def _lemma(word: str) -> str:
    return LEMMA_FIXES.get(word) or _morph().parse(word)[0].normal_form


def _words(text: str) -> list[str]:
    return re.findall(r"[a-zа-яё]+", text.lower())


def ingredient_key(name: str) -> str:
    """Ключ для сравнения: не зависит от падежа, порядка слов и буквы ё."""
    lemmas = [_lemma(word) for word in _words(name)]
    meaningful = [lemma for lemma in lemmas if lemma.replace("ё", "е") not in DESCRIPTORS] or lemmas
    return " ".join(sorted(meaningful)).replace("ё", "е")


def is_nominative(name: str) -> bool:
    """Подходит ли написание для показа: первое существительное в именительном падеже."""
    for word in _words(name):
        if not _morph().word_is_known(word):
            return True  # падеж редкого слова словарь только угадывает, верим написанию
        parse = _morph().parse(word)[0]
        if "NOUN" in parse.tag:
            return "nomn" in parse.tag
    return False


def to_nominative(name: str) -> str:
    """«жидких свежих дрожжей» -> «жидкие свежие дрожжи». Склоняем только до первого
    существительного включительно: «соль мелкого помола» менять не надо."""
    words = name.split()
    for index, word in enumerate(words):
        if not _morph().word_is_known(word):
            break
        parses = _morph().parse(word)
        # Разбор как существительного важнее: иначе «мяты» превратится в «мятая»
        noun = next((item for item in parses if {"NOUN", "gent"} in item.tag), None)
        inflected = (noun or parses[0]).inflect({"nomn"})
        if inflected:
            words[index] = inflected.word
        if noun or "NOUN" in parses[0].tag:
            break
    return " ".join(words)


def _without_leading_descriptors(name: str) -> str:
    """«Небольшие луковички» -> «луковички»: размер не часть названия продукта."""
    words = name.split()
    while len(words) > 1 and _lemma(words[0].lower()).replace("ё", "е") in DESCRIPTORS:
        words.pop(0)
    return " ".join(words)


class IngredientMatcher:
    """Находит или создаёт ингредиенты в рамках соединения с базой."""

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self._ids: dict[str, int] = {}
        self._fixed_names: set[int] = set()  # не переименовываем: название задано явно
        self._spellings: dict[int, Counter] = defaultdict(Counter)
        self._units: dict[int, Counter] = defaultdict(Counter)
        self._seed_known()
        self._load()

    def _seed_known(self) -> None:
        for category, products in KNOWN_INGREDIENTS.items():
            for name, aliases in products.items():
                key = ingredient_key(name)
                self.conn.execute(
                    "INSERT INTO ingredients (name, normalized_name, category) VALUES (?, ?, ?) "
                    "ON CONFLICT (normalized_name) DO UPDATE SET name = excluded.name, "
                    "category = excluded.category",
                    (name, key, category),
                )
                ingredient_id = self.conn.execute(
                    "SELECT id FROM ingredients WHERE normalized_name = ?", (key,)
                ).fetchone()[0]
                self._fixed_names.add(ingredient_id)
                for alias in aliases:
                    self._insert_alias(alias, ingredient_id)

    def _insert_alias(self, alias: str, ingredient_id: int) -> None:
        key = ingredient_key(alias)
        self.conn.execute(
            "INSERT OR IGNORE INTO ingredient_aliases (ingredient_id, alias, normalized_alias) "
            "VALUES (?, ?, ?)",
            (ingredient_id, alias, key),
        )

    def _load(self) -> None:
        # Сначала названия, потом псевдонимы: связка, заданная явно, важнее автоматической
        for row in self.conn.execute("SELECT id, normalized_name FROM ingredients"):
            self._ids[row["normalized_name"]] = row["id"]
        for row in self.conn.execute("SELECT ingredient_id, normalized_alias FROM ingredient_aliases"):
            self._ids[row["normalized_alias"]] = row["ingredient_id"]

    def find(self, name: str) -> int | None:
        return self._ids.get(ingredient_key(name))

    def resolve(self, name: str, unit: str | None = None) -> int:
        """Возвращает id ингредиента и запоминает написание для выбора названия."""
        key = ingredient_key(name)
        ingredient_id = self._ids.get(key)
        if ingredient_id is None:
            ingredient_id = self.conn.execute(
                "INSERT INTO ingredients (name, normalized_name) VALUES (?, ?)",
                (name[:1].upper() + name[1:], key),
            ).lastrowid
            self._ids[key] = ingredient_id
        self._spellings[ingredient_id][name.lower()] += 1
        if unit:
            self._units[ingredient_id][unit] += 1
        return ingredient_id

    def finish(self) -> None:
        """Выбирает показываемое название и основную единицу по собранной статистике."""
        for ingredient_id, spellings in self._spellings.items():
            if ingredient_id in self._fixed_names:
                continue
            nominative = [item[0] for item in spellings.most_common() if is_nominative(item[0])]
            best = nominative[0] if nominative else to_nominative(spellings.most_common(1)[0][0])
            best = _without_leading_descriptors(best)
            self.conn.execute(
                "UPDATE ingredients SET name = ? WHERE id = ?",
                (best[:1].upper() + best[1:], ingredient_id),
            )
        for ingredient_id, units in self._units.items():
            self.conn.execute(
                "UPDATE ingredients SET default_unit = ? WHERE id = ?",
                (units.most_common(1)[0][0], ingredient_id),
            )

    def remove_unused(self) -> None:
        """Убирает продукты, которые после смены правил разбора никто не использует.
        Связанные с инвентарём и явно заданные остаются."""
        fixed = ",".join(str(ingredient_id) for ingredient_id in self._fixed_names) or "0"
        self.conn.execute(
            f"DELETE FROM ingredients WHERE id NOT IN ({fixed}) "
            "AND id NOT IN (SELECT ingredient_id FROM recipe_ingredients) "
            "AND id NOT IN (SELECT ingredient_id FROM inventory WHERE ingredient_id IS NOT NULL) "
            "AND id NOT IN (SELECT ingredient_id FROM ingredient_aliases)"
        )


def find_ingredient(conn: sqlite3.Connection, name: str) -> int | None:
    """Ищет продукт по названию, ничего не создавая. Заданная связка важнее названия."""
    key = ingredient_key(name)
    row = conn.execute(
        "SELECT ingredient_id FROM ingredient_aliases WHERE normalized_alias = ? "
        "UNION ALL SELECT id FROM ingredients WHERE normalized_name = ? LIMIT 1",
        (key, key),
    ).fetchone()
    return row[0] if row else None


def suggest_ingredients(conn: sqlite3.Connection, name: str, limit: int = 5) -> list[tuple[int, str]]:
    """Похожие продукты: сначала с общими словами, затем близкие по написанию (опечатки)."""
    key = ingredient_key(name)
    words = set(key.split())
    scored = []
    for row in conn.execute("SELECT id, name, normalized_name FROM ingredients"):
        shared = len(words & set(row["normalized_name"].split()))
        similarity = SequenceMatcher(None, key, row["normalized_name"]).ratio()
        if shared or similarity >= MIN_SIMILARITY:
            scored.append((shared, similarity, row["id"], row["name"]))
    scored.sort(key=lambda item: (-item[0], -item[1], item[3]))
    return [(item[2], item[3]) for item in scored[:limit]]


def generic_ingredient_ids(conn: sqlite3.Connection, ingredient_id: int) -> list[int]:
    """Общие названия, которые этот продукт может заменить: для сливочного масла — «масло»."""
    row = conn.execute("SELECT name FROM ingredients WHERE id = ?", (ingredient_id,)).fetchone()
    if row is None:
        return []
    generics = []
    for generic, products in GENERIC_INGREDIENTS.items():
        if any(ingredient_key(product) == ingredient_key(row["name"]) for product in products):
            generic_id = find_ingredient(conn, generic)
            if generic_id is not None and generic_id != ingredient_id:
                generics.append(generic_id)
    return generics


def generic_children(conn: sqlite3.Connection) -> dict[int, list[int]]:
    """Какие продукты могут заменить общее название: id «масла» -> id конкретных масел."""
    children = {}
    for generic, products in GENERIC_INGREDIENTS.items():
        generic_id = find_ingredient(conn, generic)
        if generic_id is not None:
            ids = [find_ingredient(conn, product) for product in products]
            children[generic_id] = [item for item in ids if item is not None]
    return children


def add_alias(conn: sqlite3.Connection, alias: str, ingredient_id: int) -> None:
    """Запоминает связку навсегда: так система учится на подтверждённых совпадениях."""
    key = ingredient_key(alias)
    conn.execute(
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES (?, ?, ?) "
        "ON CONFLICT (normalized_alias) DO UPDATE SET ingredient_id = excluded.ingredient_id, "
        "alias = excluded.alias",
        (ingredient_id, alias, key),
    )

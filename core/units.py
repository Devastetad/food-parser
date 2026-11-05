"""Единицы измерения и пересчёт между ними."""

# Единица -> (группа, сколько базовых единиц в ней). Ложки и стакан приведены к мл.
CONVERTIBLE = {
    "г": ("mass", 1),
    "кг": ("mass", 1000),
    "мл": ("volume", 1),
    "л": ("volume", 1000),
    "ст. л.": ("volume", 15),
    "ч. л.": ("volume", 5),
    "стакан": ("volume", 200),
}
INVENTORY_UNITS = [
    "г", "кг", "мл", "л", "шт", "упаковка", "банка", "пучок", "головка", "зубчик",
    "ст. л.", "ч. л.", "стакан",
]  # fmt: skip


def convert(quantity: float, from_unit: str, to_unit: str) -> float | None:
    """Пересчитывает количество. None: единицы несовместимы, например граммы и штуки."""
    if from_unit == to_unit:
        return quantity
    source, target = CONVERTIBLE.get(from_unit), CONVERTIBLE.get(to_unit)
    if source is None or target is None or source[0] != target[0]:
        return None
    return quantity * source[1] / target[1]


# Сколько весит одна единица без точного веса. Это средние значения: для решения
# «хватит ли продуктов» их достаточно, для кондитерской точности нет.
UNIT_GRAMS = {
    "зубчик": 5, "долька": 15, "щепотка": 0.5, "пучок": 50, "веточка": 5, "горошина": 0.1,
    "головка": 80, "ломтик": 30, "кусок": 50, "банка": 400, "упаковка": 200, "стебель": 50,
    "горсть": 30, "капля": 0.05, "стручок": 15,
}  # fmt: skip
PIECE_GRAMS = {
    "Лук репчатый": 100, "Морковь": 100, "Картофель": 120, "Помидор": 120, "Огурец": 120,
    "Яйцо": 55, "Яичный желток": 18, "Яичный белок": 35, "Перец болгарский": 150,
    "Баклажан": 300, "Кабачок": 300, "Свёкла": 200, "Чеснок": 40, "Лимон": 100, "Яблоко": 150,
    "Лавровый лист": 0.2, "Курица": 1500, "Куриная грудка": 250,
}  # fmt: skip
DEFAULT_PIECE_GRAMS = 100


def grams_per_unit(unit: str | None, ingredient_name: str = "") -> float:
    """Вес единицы в граммах. Плотность жидкостей и сыпучих считаем равной воде."""
    if unit in CONVERTIBLE:
        return CONVERTIBLE[unit][1]
    if unit == "шт":
        return PIECE_GRAMS.get(ingredient_name, DEFAULT_PIECE_GRAMS)
    return UNIT_GRAMS.get(unit, DEFAULT_PIECE_GRAMS)


def to_grams(quantity: float, unit: str | None, ingredient_name: str = "") -> float:
    return quantity * grams_per_unit(unit, ingredient_name)

"""Разбор сырых строк рецепта: количество, единицы, время, порции, категория, техника."""
import re
from dataclasses import dataclass

MASS, VOLUME = "г", "мл"

# Регулярное выражение единицы -> (каноническая единица, множитель).
# Вес и объём сразу приводятся к граммам и миллилитрам. Имперские меры британские:
# так указано в рецептах источника.
UNITS = [
    (r"кг|килограмм\w*", MASS, 1000),
    (r"мг|миллиграмм\w*", MASS, 0.001),
    (r"г|гр|грамм\w*", MASS, 1),
    (r"мл|миллилитр\w*", VOLUME, 1),
    (r"л|литр\w*", VOLUME, 1000),
    (r"фунт\w*", MASS, 453.6),
    (r"унци\w*", MASS, 28.35),
    (r"пинт\w*", VOLUME, 568),
    (r"кварт\w*", VOLUME, 1136),
    (r"галлон\w*", VOLUME, 4546),
    (r"(?:ст\.?\s*л\.?|стол\.?\s*л\w*\.?|ст\.?\s*ложк\w*|столов\w*\s+ложк\w*)", "ст. л.", 1),
    (r"(?:ч\.?\s*л\.?|ч\.?\s*ложк\w*|чайн\w*\s+ложк\w*)", "ч. л.", 1),
    (r"кофейн\w*\s+ложк\w*", "ч. л.", 0.5),
    (r"стакан\w*|чашк\w*|чашек|чашечк\w*|ст\.(?=\s)", "стакан", 1),
    (r"шт|штук\w*|штуч\w*", "шт", 1),
    (r"зубчик\w*|зубок|зубк\w*", "зубчик", 1),
    (r"дольк\w*|долек", "долька", 1),
    (r"щепот\w*", "щепотка", 1),
    (r"пучок|пучк\w*|пучоч\w*", "пучок", 1),
    (r"веточ\w*|ветк\w*|веток", "веточка", 1),
    (r"горошин\w*", "горошина", 1),
    (r"головк\w*|головок", "головка", 1),
    (r"ломтик\w*", "ломтик", 1),
    (r"кусоч\w*|кус(?:о)?к\w*", "кусок", 1),
    (r"банк\w*|банок", "банка", 1),
    (r"упаковк\w*|упаковок|пачк\w*|пачек|пакет\w*", "упаковка", 1),
    (r"стебел\w*|стебл\w*", "стебель", 1),
    (r"горст\w*", "горсть", 1),
    (r"капл\w*|капел\w*", "капля", 1),
    (r"стручок|стручк\w*", "стручок", 1),
]
UNIT_PATTERNS = [
    (re.compile(rf"(?:{pattern})(?![а-яё])\.?", re.I), unit, factor)
    for pattern, unit, factor in UNITS
]

WORD_NUMBERS = {
    "один": 1, "одна": 1, "одно": 1, "одну": 1, "одного": 1, "одной": 1, "одному": 1,
    "два": 2, "две": 2, "двух": 2, "двум": 2, "три": 3, "трёх": 3, "трех": 3,
    "четыре": 4, "четырёх": 4, "пять": 5, "шесть": 6, "семь": 7, "восемь": 8,
    "девять": 9, "десять": 10, "полтора": 1.5, "полторы": 1.5,
    "пол": 0.5, "половина": 0.5, "половину": 0.5, "половинка": 0.5,
}  # fmt: skip
FRACTIONS = {"½": 0.5, "¼": 0.25, "¾": 0.75, "⅓": 1 / 3, "⅔": 2 / 3, "⅛": 0.125}

DASH = r"[-–—]"
SIMPLE_NUMBER = r"\d+(?:[.,]\d+)?"
FRACTION_CHARS = "".join(FRACTIONS)
ONE_NUMBER = (
    rf"(?:\d+\s+\d+/\d+|\d+/\d+|\d*[{FRACTION_CHARS}]|{SIMPLE_NUMBER}|"
    rf"(?:{'|'.join(WORD_NUMBERS)})(?![а-яё]))"
)
QUANTITY_RE = re.compile(rf"({ONE_NUMBER})(?:\s*{DASH}\s*({ONE_NUMBER}))?", re.I)
SEPARATOR_RE = re.compile(rf":|\s{DASH}+\s|\s{DASH}(?=\d)")
TO_TASTE_RE = re.compile(
    r"по вкусу|сколько нужно|сколько потребуется|немного|по желанию|для жарки|для подачи|"
    r"для смазывания|для обжаривания|для украшения|для посыпки|на кончике ножа|"
    r"примерно столько|несколько",
    re.I,
)
PARENTHESES_RE = re.compile(r"\([^()]*\)")
SIZE_WORDS_RE = re.compile(
    r"^(?:больш\w+|средн\w+|крупн\w+|небольш\w+|мелк\w+|маленьк\w+|полн\w+|неполн\w+|"
    r"хорош\w+|примерно|около|приблизительно|ещё|еще|свеж\w+|по),?\s+",
    re.I,
)
TAIL_RE = re.compile(r"\s+(?:для|на|из|без|в|с целью|либо)\s+.*$", re.I)
ALTERNATIVE_RE = re.compile(r"\s+или\s+", re.I)
LIST_SPLIT_RE = re.compile(r",\s+|\s+и\s+", re.I)
MAX_WORDS_TO_SPLIT = 3
NO_UNIT_MASS_FROM = 10  # «говядина: 54» в таблицах раскладки — граммы, «лук: 2» — штуки
BACKSLASH_FRACTION_RE = re.compile(r"(?<=\d)\\(?=\d)")
LETTERS_RE = re.compile(r"[а-яёa-z]{2}", re.I)
NOT_INGREDIENT_RE = re.compile(r"^(?:на\s+\d|время приготовления)", re.I)


@dataclass
class ParsedIngredient:
    name: str
    quantity: float | None = None
    unit: str | None = None
    note: str | None = None


def parse_number(text: str) -> float:
    text = text.strip().lower()
    if text in WORD_NUMBERS:
        return float(WORD_NUMBERS[text])
    if text[-1] in FRACTIONS:
        return (float(text[:-1]) if text[:-1] else 0) + FRACTIONS[text[-1]]
    if "/" in text:
        whole, _, fraction = text.rpartition(" ")
        top, bottom = fraction.split("/")
        if int(bottom) == 0:
            return 0.0  # опечатка в рецепте: такое количество отбрасывается
        return (float(whole) if whole else 0) + int(top) / int(bottom)
    return float(text.replace(",", "."))


def _read_quantity(match: re.Match) -> float:
    """Диапазон заменяем серединой: «2-3 шт» считаем как 2,5."""
    low = parse_number(match.group(1))
    return (low + parse_number(match.group(2))) / 2 if match.group(2) else low


def _read_unit(text: str) -> tuple[str | None, float, int]:
    """Ищет единицу в начале текста. Возвращает единицу, множитель и длину совпадения."""
    for pattern, unit, factor in UNIT_PATTERNS:
        match = pattern.match(text)
        if match:
            return unit, factor, match.end()
    return None, 1, 0


def _skip_size_words(text: str) -> str:
    while True:
        stripped = SIZE_WORDS_RE.sub("", text)
        if stripped == text:
            return text
        text = stripped


def _read_measure(text: str) -> tuple[float | None, str | None, str] | None:
    """Разбирает «2 ст. л. …», «щепотка», «большая щепотка». Возвращает остаток текста."""
    text = _skip_size_words(text.strip())
    match = QUANTITY_RE.match(text)
    if match:
        rest = _skip_size_words(text[match.end() :].lstrip(" -"))
        unit, factor, length = _read_unit(rest)
        return _read_quantity(match) * factor, unit, rest[length:].strip()
    unit, _, length = _read_unit(text)
    if unit and unit not in {MASS, VOLUME}:
        return 1.0, unit, text[length:].strip()
    return None


def _with_default_unit(quantity: float, unit: str | None, bare_unit: str | None = None) -> str:
    if unit:
        return unit
    if bare_unit:
        return bare_unit
    return MASS if quantity >= NO_UNIT_MASS_FROM else "шт"


def _clean_name(name: str) -> tuple[str, str | None]:
    """Отделяет от названия варианты замены и хвосты вроде «для варки»."""
    notes = []
    name = name.replace("«", "").replace("»", "").replace('"', "")
    name = re.sub(r"^(?:или|и|а также|плюс)\s+", "", name.strip(), flags=re.I)
    alternative = ALTERNATIVE_RE.split(name, maxsplit=1)
    if len(alternative) == 2:
        name = alternative[0]
        notes.append(f"или {alternative[1].strip()}")
    tail = TAIL_RE.search(name)
    if tail and tail.start() > 0:
        notes.append(tail.group(0).strip())
        name = name[: tail.start()]
    if "," in name:
        name, _, rest = name.partition(",")
        notes.append(rest.strip())
    name = name.strip(" .,:;-–—()")
    return (name if LETTERS_RE.search(name) else ""), "; ".join(notes) or None


def _join_notes(*notes: str | None) -> str | None:
    return "; ".join(note for note in notes if note) or None


def parse_ingredient(text: str, bare_unit: str | None = None) -> list[ParsedIngredient]:
    """Разбирает строку состава. «Соль, перец по вкусу» даёт два ингредиента.
    bare_unit: единица для числа без единицы; в рецептах её угадываем по величине."""
    text = BACKSLASH_FRACTION_RE.sub("/", text)
    text = re.sub(r"^\\(?=\d)", "1/", text)  # «\4 ст.»: единица потерялась при разборе таблицы
    parentheses = [part.strip("() ") for part in PARENTHESES_RE.findall(text)]
    plain = PARENTHESES_RE.sub(" ", text)
    plain = re.sub(r"\s+", " ", plain).strip(" .,;")
    note = _join_notes(*parentheses)
    if not LETTERS_RE.search(plain) or NOT_INGREDIENT_RE.match(plain):
        return []  # итоговая строка таблицы, заголовок «На 10 порций взять», время

    parsed = (
        _parse_with_separator(plain, bare_unit)
        or _parse_leading(plain, bare_unit)
        or _parse_trailing(plain, bare_unit)
    )
    if parsed:
        name, quantity, unit, detail = parsed
        name, name_note = _clean_name(name)
        several = quantity is None and LIST_SPLIT_RE.search(name)  # «соль и перец — по вкусу»
        if several:
            plain = name
        elif name:
            if quantity is None or quantity <= 0:
                quantity, unit = None, None
            else:
                quantity = round(quantity, 3)
            return [ParsedIngredient(name, quantity, unit, _join_notes(name_note, detail, note))]

    without_taste = TO_TASTE_RE.sub("", plain).strip(" .,;:-–—")
    parts = [part.strip(" .,") for part in LIST_SPLIT_RE.split(without_taste)]
    if len(parts) > 1 and all(0 < len(part.split()) <= MAX_WORDS_TO_SPLIT for part in parts):
        result = []
        for part in parts:
            name, name_note = _clean_name(part)
            if name:
                result.append(ParsedIngredient(name, note=_join_notes(name_note, note)))
        return result
    name, name_note = _clean_name(without_taste)
    return [ParsedIngredient(name, note=_join_notes(name_note, note))] if name else []


def _parse_with_separator(text: str, bare_unit: str | None = None):
    """«мука: 150 г», «Соль — щепотка», «Перец чёрный — по вкусу»."""
    match = SEPARATOR_RE.search(text)
    if not match:
        return None
    name, right = text[: match.start()].strip(), text[match.end() :].strip()
    if not LETTERS_RE.search(name):
        return None  # «0.5 - 1 кг мидий»: тире здесь внутри диапазона
    measure = _read_measure(right)
    if measure:
        quantity, unit, rest = measure
        if quantity is not None:
            unit = _with_default_unit(quantity, unit, bare_unit)
        return name, quantity, unit, rest or None
    if TO_TASTE_RE.search(right) or not right:
        return name, None, None, None
    return name, None, None, right


def _parse_leading(text: str, bare_unit: str | None = None):
    """«2 ст. л. каперсов», «3–4 яйца», «200 гр муки»."""
    measure = _read_measure(text)
    if not measure or not measure[2]:
        return None
    quantity, unit, rest = measure
    if not unit:
        # Номер строки таблицы: «1 Лук жареный 2 ст. л.». Настоящая мера стоит дальше
        trailing = _parse_trailing(rest, bare_unit)
        if trailing:
            return trailing
    return rest, quantity, unit or "шт", None


def _parse_trailing(text: str, bare_unit: str | None = None):
    """«Говядина 160 грамм», «чеснок 5 г 4 дольки», «Соль 4 2 4 2»: берём первую меру."""
    for match in QUANTITY_RE.finditer(text):
        before = text[: match.start()]
        if not before.strip() or (before[-1].isalpha() and not before.endswith(" ")):
            continue
        rest = text[match.end() :].lstrip()
        unit, factor, length = _read_unit(rest)
        quantity = _read_quantity(match) * factor
        return before.strip(), quantity, _with_default_unit(quantity, unit, bare_unit), None
    return None


# --- Время ---

DURATION_RE = re.compile(
    rf"({SIMPLE_NUMBER})(?:\s*{DASH}\s*({SIMPLE_NUMBER}))?\s*"
    # Формы перечислены явно: «час\w*» совпало бы с «2 части», «дн\w*» с «дно»
    r"(сут(?:ки|ок)|дн(?:я|ей)|день|дни|час(?:а|ов)?(?![а-яё])|ч(?![а-яё])\.?|"
    r"мин(?:ут[ауы]?)?(?![а-яё])\.?)",
    re.I,
)
EVERY_RE = re.compile(r"кажд\w+\s*$", re.I)
UNIT_MINUTES = {"с": 1440, "д": 1440, "ч": 60, "м": 1}


def _duration_minutes(match: re.Match) -> float:
    value = float((match.group(2) or match.group(1)).replace(",", "."))
    return value * UNIT_MINUTES[match.group(3)[0].lower()]


def parse_minutes(text: str | None) -> int | None:
    """«2 часа 20 минут» складываем. Из диапазона берём верхнюю границу: для лимита
    времени лучше ошибиться в большую сторону."""
    if not text:
        return None
    total = 0.0
    previous_end = None
    previous_value = 0.0
    for match in DURATION_RE.finditer(text):
        value = _duration_minutes(match)
        if previous_end is not None and re.fullmatch(
            rf"\s*{DASH}\s*", text[previous_end : match.start()]
        ):
            total += max(value - previous_value, 0)  # «40 минут - 1 час»
        else:
            total += value
        previous_end, previous_value = match.end(), value
    return round(total) or None


def parse_step_minutes(step: str) -> int | None:
    """Длительность шага: «тушить 10—15 минут». «Каждые 5 минут» не считается."""
    total = 0.0
    for match in DURATION_RE.finditer(step):
        if not EVERY_RE.search(step[: match.start()]):
            total += _duration_minutes(match)
    return round(total) or None


# --- Порции, сложность, калории ---

SERVINGS_RE = re.compile(rf"^\s*(\d+)(?:\s*{DASH}\s*\d+)?\s*(?:порц\w*|персон\w*|человек)?\s*(?:\(.*)?$", re.I)
NOTE_SERVINGS_RE = re.compile(r"\bна\s+(\d+)\s+(?:порц|персон|человек)", re.I)
NOTE_ONE_SERVING_RE = re.compile(r"(?:на|расч[её]та на)\s+одну\s+порцию", re.I)
NOTE_TIME_RE = re.compile(r"время приготовления[:\s]+(.+?)(?:[;)]|$)", re.I)
CALORIES_RE = re.compile(rf"^\s*(\d+)(?:\s*{DASH}\s*(\d+))?\s*(?:к?кал\w*)?\s*$", re.I)
MAX_CALORIES_PER_SERVING = 2500
DIFFICULTY = {"1": "easy", "2": "easy", "3": "medium", "4": "hard", "5": "hard"}


def parse_servings(text: str | None, notes: list[str] = ()) -> int | None:
    """Порции из карточки либо из заметок: «(на 100 человек…)», «на одну порцию»."""
    match = SERVINGS_RE.match(text) if text else None
    if match and int(match.group(1)) > 0:
        return int(match.group(1))
    for note in notes:
        if NOTE_ONE_SERVING_RE.search(note):
            return 1
        match = NOTE_SERVINGS_RE.search(note)
        if match and int(match.group(1)) > 0:
            return int(match.group(1))
    return None


def time_from_notes(notes: list[str]) -> int | None:
    for note in notes:
        match = NOTE_TIME_RE.search(note)
        if match:
            return parse_minutes(match.group(1))
    return None


def parse_difficulty(text: str | None) -> str | None:
    return DIFFICULTY.get(text.strip()) if text else None


def parse_calories(text: str | None) -> float | None:
    """Только значение на порцию: «441 ккал/100 г» и «1 чашка - 50 Ккал» не подходят."""
    match = CALORIES_RE.match(text) if text else None
    if not match:
        return None
    low = int(match.group(1))
    value = (low + int(match.group(2))) / 2 if match.group(2) else float(low)
    return value if value <= MAX_CALORIES_PER_SERVING else None  # это калорий на всё блюдо


# --- Категория, кухня ---

OTHER_CATEGORY = "Другое"
CATEGORY_RULES = [
    ("Соусы", r"соус|подлив|заправк|кетчуп|маринад|аджик|майонез|горчиц|хрен\b"),
    ("Супы", r"суп|борщ|\bщи\b|уха\b|солянк|бульон|рассольник|окрошк|харчо|гаспачо|консоме|похл[её]бк"),
    ("Салаты", r"салат|винегрет|оливье"),
    ("Напитки", r"напит|\bчай|кофе|компот|морс|квас|смузи|коктейл|какао|лимонад|глинтвейн|ликер|ликёр|лимончелл|сангри|пунш|пунч"),
    ("Десерты", r"десерт|торт|пирожн|морожен|кисель|варень|джем|желе|крем\b|печень|конфет|пудинг|мусс|сироп|безе|халва|пастила"),
    ("Выпечка", r"выпечк|хлеб|пирог|пирож|булоч|булк|блин|оладь|ватрушк|кулебяк|лаваш|пицц|сдоб|тесто|мучн|чебурек|лепешк|лепёшк"),
    ("Каши", r"каш[аи]|мюсли|овсянк"),
    ("Закуски", r"закуск|бутерброд|сэндвич|фастфуд|паштет|холодец|студень|тартин|канапе|икра|икры"),
    ("Вторые блюда", r"втор|мяс|рыбн|овощн|рагу|жарк|шашлык|пельмен|плов|котлет|колбас|яйц|яич|омлет|макарон|гуляш|голубц|лапш|спагетти|запеканк|тефтел|фрикадел|отбивн|бифштекс|стейк|гриль|рулет|тушён|тушен|жарен|печён|запечён"),
]
CATEGORY_PATTERNS = [(name, re.compile(rule, re.I)) for name, rule in CATEGORY_RULES]
TITLE_SAUCE_RE = re.compile(r"^(?:соус|подлив|заправк|маринад|аджик)", re.I)


PROTEIN_RE = re.compile(
    r"мяс|говяд|свин|баран|телят|кур[иа]|цыпл|рыб|индейк|утк|гус[ья]|кролик|колбас|сосиск|"
    r"ветчин|фарш|кревет|кальмар|лосос|сельд|треск",
    re.I,
)


def normalize_category(
    category: str | None, title: str, ingredient_names: list[str] = ()
) -> str:
    """Сводит десятки написаний к короткому списку. В названии «соус» ищем только в начале:
    «Мясо в соусе» — не соус. Блюдо с мясом или рыбой без других признаков — второе."""
    for name, pattern in CATEGORY_PATTERNS:
        if category and pattern.search(category):
            return name
    if TITLE_SAUCE_RE.match(title):
        return "Соусы"
    for name, pattern in CATEGORY_PATTERNS:
        if name != "Соусы" and pattern.search(title):
            return name
    if any(PROTEIN_RE.search(name) for name in ingredient_names):
        return "Вторые блюда"
    return OTHER_CATEGORY


def normalize_cuisine(cuisine: str | None) -> str | None:
    """В источнике кухни перечислены через запятую, берём первую как основную."""
    if not cuisine:
        return None
    first = cuisine.split(",")[0].strip()
    return first[:1].upper() + first[1:] if first else None


# --- Техника ---

EQUIPMENT_RULES = {
    "Плита": r"сковород|кастрюл|казан|сотейник|вар[ияе]т|свар[ия]|отвар[ия]|кип[яе]т|жар[ия]т|обжар|"
    r"поджар|туш[ия]т|пассеров|бланшир|прокип|уварит|выпарит|на огне",
    "Духовка": r"духовк|духовой|запе[кч]|выпека|выпеч|противен",
    "Микроволновка": r"микроволнов|свч",
    "Мультиварка": r"мультиварк",
    "Аэрогриль": r"аэрогрил",
    "Блендер": r"блендер",
    "Миксер": r"миксер",
    "Гриль": r"(?<!аэро)\bгрил[ьея]|мангал|на углях",
}
EQUIPMENT_PATTERNS = {name: re.compile(rule, re.I) for name, rule in EQUIPMENT_RULES.items()}


def detect_equipment(steps: list[str]) -> set[str]:
    text = " ".join(steps)
    return {name for name, pattern in EQUIPMENT_PATTERNS.items() if pattern.search(text)}

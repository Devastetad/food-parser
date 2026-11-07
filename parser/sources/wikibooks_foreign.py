"""Кулинарные книги Викиучебника на других языках, CC BY-SA 4.0, машинный перевод.

Как и в английском источнике, перевод делает scripts/translate_wikibooks.py, а источник только
читает готовый файл. Здесь собрано то, чем языки отличаются: адрес, разметка и заголовки.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote

import config
import mwparserfromhell

from parser.sources.wikibooks_en import WikibooksEnSource


@dataclass(frozen=True)
class Language:
    code: str
    nllb: str  # код языка в модели перевода
    prefix: str
    ingredients: str
    steps: str
    ignored: str
    before: tuple[tuple[str, str], ...] = ()  # правки до перевода

    @property
    def base_url(self) -> str:
        return f"https://{self.code}.wikibooks.org/wiki/"


LANGUAGES = {
    language.code: language
    for language in (
        Language(
            "fr",
            "fra_Latn",
            "Livre de cuisine/",
            r"ingr[ée]dients?\b.*",
            r"pr[ée]paration\b.*|recette|r[ée]alisation",
            r"variantes?|voir aussi|notes?|ustensiles?|r[ée]f[ée]rences?|d[ée]coration|conseils?",
        ),
        Language(
            "de",
            "deu_Latn",
            "Kochbuch/",
            r"zutaten\b.*",
            r"zubereitung\b.*",
            r"tipps.*|warnung|varianten|variationen|servieren|siehe auch|weblinks|kochgeschirr|"
            r"hinweise|anmerkungen",
            ((r"\bel\b", "esslöffel"), (r"\btl\b", "teelöffel"), (r"\bmsp\b\.?", "messerspitze")),
        ),
        Language(
            "it",
            "ita_Latn",
            "Libro di cucina/Ricette/",
            r"ingredienti\b.*",
            r"preparazione\b.*|procedimento",
            r"consigli|altri progetti|note|varianti|voci correlate|ricette correlate|"
            r"collegamenti esterni|bibliografia",
        ),
        Language(
            "es",
            "spa_Latn",
            "Artes culinarias/Recetas/",
            r"ingredientes\b.*",
            r"preparaci[óo]n\b.*|procedimiento",
            r"v[ée]ase tambi[ée]n|referencias|enlaces externos|galer[íi]a|recomendaciones|"
            r"notas?:?|comentarios",
        ),
    )
}

INLINE_TEMPLATE_RE = re.compile(
    r"\{\{\s*(i'?|ingrédient|ustensile|ing|ute|coc|rec|min|unité|w)\s*\|([^{}]*)\}\}", re.I
)
NO_INCLUDE_RE = re.compile(r"</?(?:noinclude|onlyinclude|includeonly)>", re.I)
FILE_PREFIX_RE = re.compile(r"\[\[\s*(?:Datei|Bild|Fichier|Immagine|Archivo|Imagen)\s*:", re.I)
CATEGORY_PREFIX_RE = re.compile(r"\[\[\s*(?:Kategorie|Catégorie|Categoria|Categoría)\s*:", re.I)
IMAGE_RE = re.compile(r"\[\[(?:File|Image):([^|\]]+)", re.I)
SERVINGS_RE = re.compile(
    r"\b(?:pour|per|für|para)\s+'*(\d+)'*\s+"
    r"(?:parts?|personnes?|persone|personen|portionen|convives|porzioni|personas|raciones)",
    re.I,
)
METRIC_ML = {"cl": 10, "dl": 100}
METRIC_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*(cl|dl)\b", re.I)
ES_RECIPE = "artes culinarias/datos de receta"
DE_BOX_RE = re.compile(r":?kochbuch/[ _]*vorlage/[ _]*rezeptbox", re.I)
DE_BOX_PARAMS = {
    "Kategorie": "Категория",
    "Portionen": "Порций",
    "Zubereitungszeit": "Время",
    "Schwierigkeitsgrad": "Сложность",
}


def page_url(language: Language, title: str) -> str:
    return language.base_url + quote(title.replace(" ", "_"), safe=":/")


def page_name(language: Language, title: str) -> str:
    """Название блюда: страница бывает вложенной, как «Boissons/Cocktails/Mojito»."""
    return title.removeprefix(language.prefix).rsplit("/", 1)[-1].strip()


def _to_millilitres(match: re.Match) -> str:
    value = float(match.group(1).replace(",", ".")) * METRIC_ML[match.group(2).lower()]
    return f"{value:g} ml"


def normalize_units(text: str) -> str:
    """Сантилитры переводчик превращает в литры, поэтому пересчитываем их заранее."""
    return METRIC_RE.sub(_to_millilitres, text)


def _inline_value(match: re.Match) -> str:
    """Шаблон вокруг продукта оставляет только то, что видит читатель."""
    params = [part.strip() for part in match.group(2).split("|") if "=" not in part]
    if match.group(1).lower() == "unité":
        return " ".join(params)
    return params[-1] if params else ""


def _param(template, name: str) -> str:
    return str(template.get(name).value).strip() if template.has(name) else ""


def _filled(value: str) -> str:
    """Незаполненная карточка хранит подсказку вроде «Nº de comensales», а не значение."""
    return value if re.search(r"\d", value) else ""


def _info(values: dict[str, str]) -> str:
    params = "".join(f"\n| {name} = {value}" for name, value in values.items() if value)
    return "{{Рецепт" + params + "\n}}\n"


def _expand_boxes(text: str) -> tuple[str, dict[str, str]]:
    """Карточки рецептов разных языков приводит к разделам и общим полям."""
    code = mwparserfromhell.parse(text)
    info = {}
    for template in code.filter_templates(recursive=False):
        name = str(template.name).strip().lower().replace("_", " ")
        if name == ES_RECIPE:
            info |= {
                "Порций": _filled(_param(template, "comensales")),
                "Время": _filled(_param(template, "tiempo")),
                "Сложность": _param(template, "dificultad"),
            }
            sections = (
                f"== Состав ==\n{_param(template, 'ingredientes')}\n"
                f"== Приготовление ==\n{_param(template, 'procedimiento')}\n"
            )
            code.replace(template, sections)
        elif DE_BOX_RE.fullmatch(name):
            info |= {ours: _param(template, theirs) for theirs, ours in DE_BOX_PARAMS.items()}
            code.remove(template)
    return str(code), info


def to_extractor_markup(language: Language, wikitext: str) -> str:
    """Приводит разметку языка к той, что понимает recipe_extractor."""
    text = NO_INCLUDE_RE.sub("", wikitext)
    while INLINE_TEMPLATE_RE.search(text):
        text = INLINE_TEMPLATE_RE.sub(_inline_value, text)
    text = FILE_PREFIX_RE.sub("[[File:", text)
    text = CATEGORY_PREFIX_RE.sub("[[Category:", text)
    text, info = _expand_boxes(text)

    if not info.get("Порций") and (servings := SERVINGS_RE.search(text)):
        info["Порций"] = servings.group(1)
    if image := IMAGE_RE.search(text):
        info["Изображение"] = image.group(1).strip()

    for pattern, heading in (
        (language.ingredients, "Состав"),
        (language.steps, "Приготовление"),
        (language.ignored, "Примечания"),
    ):
        text = re.sub(
            rf"^(=+)\s*'*(?:{pattern})'*:?\s*\1\s*$",
            lambda match, name=heading: f"{match.group(1)} {name} {match.group(1)}",
            text,
            flags=re.I | re.M,
        )
    return _info(info) + text


def translated_file(code: str):
    return config.SOURCES_DIR / f"wikibooks_{code}_ru.jsonl"


def _make_source(code: str) -> type[WikibooksEnSource]:
    class Source(WikibooksEnSource):
        name = f"wikibooks_{code}"

        def __init__(self, path=None):
            super().__init__(path or translated_file(code))

        def extract(self, item):
            recipes = super().extract(item)
            for recipe in recipes:
                recipe.title = drop_unknown(recipe.title)
                recipe.steps = [drop_unknown(step) for step in recipe.steps]
                recipe.description = recipe.description and drop_unknown(recipe.description)
                for line in recipe.ingredients:
                    line.text = drop_unknown(line.text)
                if DRINK_SECTION_RE.search(item.title):  # коктейли без раздела стали бы «блюдами»
                    recipe.category = "Напитки"
            return recipes

    return Source


DRINK_SECTION_RE = re.compile(r"/(?:Boissons|Bevande|Bebidas|Getränke)/")
UNKNOWN = "⁇"  # переводчик так помечает символ, которого не знает: кавычки, тире, дроби
RANGE_RE = re.compile(rf"(\d)\s*{UNKNOWN}\s*(\d)")


def drop_unknown(text: str) -> str:
    if UNKNOWN not in text:
        return text
    text = RANGE_RE.sub(r"\1–\2", text)
    return re.sub(r"\s+", " ", text.replace(UNKNOWN, " ")).strip()


# Итальянский перевод портит продукты (рикотта, дрожжи, кабачки), поэтому в базу не идёт
NOT_IMPORTED = {"it"}
FOREIGN_SOURCES = {
    f"wikibooks_{code}": _make_source(code) for code in LANGUAGES if code not in NOT_IMPORTED
}

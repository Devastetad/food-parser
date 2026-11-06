"""Английская Кулинарная книга Викиучебника, лицензия CC BY-SA 4.0, машинный перевод.

Перевод делает scripts/translate_wikibooks_en.py один раз: он долгий и требует отдельного
окружения с Argos Translate. Этот источник только читает готовый файл, поэтому сбор
рецептов не зависит от переводчика.
"""
import json
import re
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import quote

import config
from parser.base_parser import (
    BaseSource,
    IngredientLine,
    NoRecipeFound,
    RawRecipe,
    SourceItem,
    SourceUnavailable,
)

BASE_URL = "https://en.wikibooks.org/wiki/"
TRANSLATED_FILE = config.SOURCES_DIR / "wikibooks_en_ru.jsonl"

SUMMARY_RE = re.compile(r"\{\{\s*recipesummary", re.I)
PARAM_NAMES = {
    "category": "Категория",
    "servings": "Порций",
    "yield": "Порций",
    "time": "Время",
    "difficulty": "Сложность",
    "image": "Изображение",
}
PARAM_RE = re.compile(r"\|\s*(" + "|".join(PARAM_NAMES) + r")\s*=", re.I)
HEADINGS = [
    (re.compile(r"^(=+)\s*'*ingredients?'*:?\s*\1\s*$", re.I | re.M), "Состав"),
    (
        re.compile(
            r"^(=+)\s*'*(?:procedures?|directions?|method|instructions?|preparation|steps)'*:?\s*\1\s*$",
            re.I | re.M,
        ),
        "Приготовление",
    ),
    (
        re.compile(
            r"^(=+)\s*(?:notes.*|references?|see also|external links?|equipment|special equipment|"
            r"warnings?|serving|gallery|variations?|substitutions?)\s*\1\s*$",
            re.I | re.M,
        ),
        "Примечания",
    ),
]


def to_extractor_markup(wikitext: str) -> str:
    """Приводит английскую разметку к той, что понимает recipe_extractor."""
    text = SUMMARY_RE.sub("{{Рецепт", wikitext)
    text = PARAM_RE.sub(lambda match: f"| {PARAM_NAMES[match.group(1).lower()]} =", text)
    for pattern, heading in HEADINGS:
        text = pattern.sub(lambda match, name=heading: f"{match.group(1)} {name} {match.group(1)}", text)
    return text


def page_url(title: str) -> str:
    return BASE_URL + quote(title.replace(" ", "_"), safe=":/")


class WikibooksEnSource(BaseSource):
    name = "wikibooks_en"
    license = "CC BY-SA 4.0"

    def __init__(self, path: Path = TRANSLATED_FILE):
        self.path = path

    def prepare(self) -> None:
        if not self.path.exists():
            raise SourceUnavailable(
                f"Нет файла с переводом {self.path.name}: запустите scripts/translate_wikibooks_en.py"
            )

    def iter_items(self) -> Iterator[SourceItem]:
        with self.path.open(encoding="utf-8") as file:
            for line in file:
                page = json.loads(line)
                yield SourceItem(page["id"], page["revision"], page["title"], line)

    def extract(self, item: SourceItem) -> list[RawRecipe]:
        recipes = json.loads(item.content)["recipes"]
        if not recipes:
            raise NoRecipeFound("Нет разделов состава и приготовления")
        return [
            RawRecipe(
                **{
                    **recipe,
                    "ingredients": [IngredientLine(**line) for line in recipe["ingredients"]],
                }
            )
            for recipe in recipes
        ]

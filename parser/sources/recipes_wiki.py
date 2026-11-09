"""Recipes Wiki (recipes.fandom.com), лицензия CC BY-SA 3.0, машинный перевод с английского.

Статьи скачивает scripts/fetch_recipes_wiki.py, переводит scripts/translate_wikibooks.py
с --source recipes_wiki. Этот источник только читает готовый перевод.
"""
from urllib.parse import quote

import config
from parser.sources.wikibooks_en import WikibooksEnSource

BASE_URL = "https://recipes.fandom.com/wiki/"


def page_url(title: str) -> str:
    return BASE_URL + quote(title.replace(" ", "_"), safe=":/")


def translated_file():
    return config.SOURCES_DIR / "recipes_wiki_ru.jsonl"


class RecipesWikiSource(WikibooksEnSource):
    name = "recipes_wiki"
    license = "CC BY-SA 3.0"

    def __init__(self, path=None):
        super().__init__(path or translated_file())

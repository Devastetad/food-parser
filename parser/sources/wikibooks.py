"""Кулинарная книга русских Викиучебников, лицензия CC BY-SA 4.0.

Берём официальный дамп, а не API: так советует политика Wikimedia для роботов,
на всю базу нужен один файл (около 21 МБ), и сбор работает без сети на уже скачанном.
"""
import bz2
import xml.etree.ElementTree as ET
from collections.abc import Iterator
from pathlib import Path

import config
from parser.base_parser import BaseSource, RawRecipe, SourceItem, SourceUnavailable
from parser.http_client import FetchError, PoliteClient
from parser.recipe_extractor import extract_recipes

DUMP_URL = (
    "https://dumps.wikimedia.org/ruwikibooks/latest/"
    "ruwikibooks-latest-pages-articles.xml.bz2"
)
RECIPE_NAMESPACE = "104"
# Увеличить при изменении извлечения: все страницы обработаются заново
EXTRACTOR_VERSION = 1


class WikibooksSource(BaseSource):
    name = "wikibooks_ru"
    license = "CC BY-SA 4.0"

    def __init__(
        self,
        client: PoliteClient | None = None,
        dump_path: Path = config.SOURCES_DIR / "ruwikibooks-pages-articles.xml.bz2",
        dump_url: str = DUMP_URL,
    ):
        self.client = client or PoliteClient()
        self.dump_path = dump_path
        self.dump_url = dump_url

    def prepare(self) -> None:
        try:
            self.client.download(self.dump_url, self.dump_path)
        except FetchError as error:
            if not self.dump_path.exists():
                raise SourceUnavailable(f"Дамп недоступен: {error}") from error

    def iter_items(self) -> Iterator[SourceItem]:
        with bz2.open(self.dump_path) as dump:
            for _, element in ET.iterparse(dump):
                if element.tag.rpartition("}")[2] != "page":
                    continue
                item = _read_page(element)
                element.clear()
                if item:
                    yield item

    def extract(self, item: SourceItem) -> list[RawRecipe]:
        return extract_recipes(item.title, item.content)


def _read_page(page: ET.Element) -> SourceItem | None:
    if page.findtext("{*}ns") != RECIPE_NAMESPACE or page.find("{*}redirect") is not None:
        return None
    revision = page.find("{*}revision")
    return SourceItem(
        item_id=page.findtext("{*}id"),
        revision=f"{revision.findtext('{*}id')}:v{EXTRACTOR_VERSION}",
        title=page.findtext("{*}title"),
        content=revision.findtext("{*}text") or "",
    )

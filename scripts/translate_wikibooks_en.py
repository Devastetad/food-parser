"""Переводит рецепты английского Викиучебника на русский локально, бесплатно, через Argos.

Запускается отдельно от приложения и редко:
    python scripts/translate_wikibooks_en.py путь/к/enwikibooks-pages-articles.xml.bz2 [--limit N]

Нужно окружение с пакетами argostranslate и mwparserfromhell. Результат дописывается в
data/sources/wikibooks_en_ru.jsonl: прерванный перевод продолжится с места остановки.
"""
import argparse
import bz2
import json
import multiprocessing
import os
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from parser.base_parser import NoRecipeFound  # noqa: E402
from parser.recipe_extractor import extract_recipes  # noqa: E402
from parser.sources.wikibooks_en import (  # noqa: E402
    BASE_URL,
    TRANSLATED_FILE,
    page_url,
    to_extractor_markup,
)

WORKERS = 3
MAX_LINES = 45  # длиннее обычно справочные страницы, а не рецепты
RU_BASE_URL = "https://ru.wikibooks.org/wiki/"

# Термины, которые переводчик путает. Правим до перевода...
BEFORE = [
    (r"\bground (beef|pork|turkey|chicken|lamb|meat)\b", r"minced \1"),
    (r"\bground (black pepper|pepper|cinnamon|ginger|nutmeg|allspice|cumin|cloves)\b", r"\1"),
    (r"\beggplants?\b", "aubergine"),
    (r"\bcans? of\b", "jar of"),
    (r"\bcans?\b", "jar"),
    (r"\bdrained\b", "without liquid"),
    (r"\bsimmer(ing)?\b", "cook on low heat"),
    (r"\btbsps?\b\.?", "tablespoon"),
    (r"\btsps?\b\.?", "teaspoon"),
    (r"\b(all[- ]purpose|plain) flour\b", "wheat flour"),
    (r"\bcloves? (of )?garlic\b", "garlic cloves"),
    (r"\bminced\b", "chopped"),
    (r"\bsaut[eé]\b", "fry"),
    (r"\bpinch of\b", "pinch"),
    (r"\bstick of butter\b", "110 g butter"),
    (r"\bscallions?\b", "green onion"),
    (r"\bbell peppers?\b", "sweet pepper"),
]
# ...и после него
AFTER = [
    (r"на вкус\b", "по вкусу"),
    (r"\bизбит\w+", "взбитые"),
    (r"\bСиммер\w*", "Томите"),
    (r"\bфарширован\w+", "измельчённый"),
    (r"\bпобить\b", "взбить"),
    (r"\bскремблированн\w+ яйц\w+", "яичница-болтунья"),
    (r"\bчашк\w*", "стакан"),
    (r"\bгвоздик\w* чеснока", "зубчика чеснока"),
    (r"\bбанк\b", "банка"),
    (r"\bКука\b", "Готовьте"),
    (r"\bрастирайте всю", "натрите всю"),
    (r"\bкубик\b", "кубиками"),
]
FRACTION_RE = re.compile(r"(?:(\d+)[  -]+)?(\d+)\s*[⁄/]\s*(\d+)")
VULGAR = {"½": "1/2", "¼": "1/4", "¾": "3/4", "⅓": "1/3", "⅔": "2/3", "⅛": "1/8"}
LATIN_RE = re.compile(r"[A-Za-z]{3,}")
BEFORE = [(re.compile(pattern, re.I), value) for pattern, value in BEFORE]
AFTER = [(re.compile(pattern, re.I), value) for pattern, value in AFTER]

_translator = None


def _start_worker() -> None:
    global _translator
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    import argostranslate.translate

    _translator = argostranslate.translate.get_translation_from_codes("en", "ru")


def _decimal(match: re.Match) -> str:
    """Дроби переводчик портит, поэтому заменяем их десятичной записью заранее."""
    bottom = int(match.group(3))
    if bottom == 0:
        return match.group(0)
    value = int(match.group(1) or 0) + int(match.group(2)) / bottom
    return f"{value:.2f}".rstrip("0").rstrip(".")


def translate(text: str | None, lower: bool = False) -> str | None:
    if not text:
        return text
    for symbol, fraction in VULGAR.items():
        text = text.replace(symbol, f" {fraction}")
    text = FRACTION_RE.sub(_decimal, text)
    if lower:  # Заглавные сбивают переводчик: «Turkey» становится страной
        text = text.lower()
    for pattern, value in BEFORE:
        text = pattern.sub(value, text)
    result = _translator.translate(text)
    for pattern, value in AFTER:
        result = pattern.sub(value, result)
    return result


def translate_page(page: dict) -> str:
    """Возвращает строку JSONL. Страница без рецепта тоже записывается, чтобы не разбирать снова."""
    try:
        recipes = extract_recipes(page["title"], to_extractor_markup(page["text"]))
    except NoRecipeFound:
        recipes = []
    translated = []
    for recipe in recipes:
        if len(recipe.ingredients) + len(recipe.steps) > MAX_LINES:
            continue
        data = asdict(recipe)
        title = translate(recipe.title, lower=True)
        if LATIN_RE.search(title):
            continue  # название не перевелось: по такому блюду не понять, что это
        data["title"] = title[:1].upper() + title[1:]
        data["source_url"] = recipe.source_url.replace(
            page_url(page["title"]).replace(BASE_URL, RU_BASE_URL), page_url(page["title"])
        )
        if data["image_url"]:
            data["image_url"] = data["image_url"].replace(RU_BASE_URL, BASE_URL)
        for key in ("description", "category", "cuisine", "time_text", "calories_text"):
            data[key] = translate(data[key])
        groups = {}
        for line in data["ingredients"]:
            line["text"] = translate(line["text"], lower=True)
            if line["group"]:
                line["group"] = groups.setdefault(line["group"], translate(line["group"]))
        data["steps"] = [translate(step) for step in recipe.steps]
        data["ingredient_notes"] = [translate(note) for note in recipe.ingredient_notes]
        if any(LATIN_RE.search(line["text"]) for line in data["ingredients"]):
            continue  # непереведённый продукт нельзя сопоставить с запасами
        translated.append(data)
    return json.dumps(
        {"id": page["id"], "revision": page["revision"], "title": page["title"], "recipes": translated},
        ensure_ascii=False,
    )


def read_pages(dump: Path, done: set[str]):
    with bz2.open(dump) as file:
        for _, element in ET.iterparse(file):
            if element.tag.rpartition("}")[2] != "page":
                continue
            title = element.findtext("{*}title") or ""
            if title.startswith("Cookbook:") and element.find("{*}redirect") is None:
                page_id = element.findtext("{*}id")
                revision = element.find("{*}revision")
                text = revision.findtext("{*}text") or ""
                if page_id not in done and re.search(r"^=+\s*'*ingredients?", text, re.I | re.M):
                    yield {
                        "id": page_id,
                        "revision": revision.findtext("{*}id"),
                        "title": title,
                        "text": text,
                    }
            element.clear()


def main() -> None:
    arguments = argparse.ArgumentParser()
    arguments.add_argument("dump", type=Path)
    arguments.add_argument("--limit", type=int)
    options = arguments.parse_args()

    TRANSLATED_FILE.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if TRANSLATED_FILE.exists():
        with TRANSLATED_FILE.open(encoding="utf-8") as file:
            done = {json.loads(line)["id"] for line in file}
    # Короткие страницы первыми: простые блюда нужнее всего и будут готовы раньше
    pages = sorted(read_pages(options.dump, done), key=lambda page: len(page["text"]))
    pages = pages[: options.limit]
    print(f"Готово {len(done)}, осталось {len(pages)}", flush=True)

    with (
        multiprocessing.Pool(WORKERS, initializer=_start_worker) as pool,
        TRANSLATED_FILE.open("a", encoding="utf-8") as output,
    ):
        for number, line in enumerate(pool.imap(translate_page, pages), start=1):
            output.write(line + "\n")
            output.flush()
            if number % 50 == 0:
                print(number, flush=True)


if __name__ == "__main__":
    main()

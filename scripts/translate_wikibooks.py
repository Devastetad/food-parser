"""Переводит рецепты Викиучебников на русский локально, бесплатно, через Argos.

Запускается отдельно от приложения и редко:
    python scripts/translate_wikibooks.py путь/к/дампу.xml.bz2 [--lang fr] [--limit N]

Нужно окружение с пакетами argostranslate, ctranslate2, sentencepiece и mwparserfromhell.
Результат дописывается в data/sources/wikibooks_<язык>_ru.jsonl: прерванный перевод продолжится
с места остановки. Английский переводит Argos, остальные языки — модель NLLB в формате
CTranslate2, папку с ней указывает NLLB_MODEL: Argos через английский портил их заметно сильнее.
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
from parser.recipe_extractor import page_url as ru_page_url  # noqa: E402
from parser.sources.wikibooks_en import (  # noqa: E402
    BASE_URL,
    TRANSLATED_FILE,
    page_url,
    to_extractor_markup,
)
from parser.sources.wikibooks_foreign import (  # noqa: E402
    LANGUAGES,
    normalize_units,
    page_name,
    translated_file,
)
from parser.sources.wikibooks_foreign import page_url as foreign_page_url  # noqa: E402
from parser.sources.wikibooks_foreign import (  # noqa: E402
    to_extractor_markup as foreign_markup,
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
    (r"\bзуб\w* чеснока", "зубчика чеснока"),
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

VARIANT_RE = re.compile(r"\s*\(вариант \d+\)$")
NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)?")
SENTENCE_RE = re.compile(r"(?<=[.!?;])\s+")
NLLB_THREADS = 3

_translator = None
_nllb = None
_spm = None
_language = None
_cache: dict[str, str] = {}


class NumbersChanged(Exception):
    """Перевод потерял число: рецепт с неверным количеством или временем хуже, чем никакой."""


def _start_worker(code: str, lock) -> None:
    global _translator, _nllb, _spm, _language
    os.environ.setdefault("OMP_NUM_THREADS", "2")
    if code != "en":
        import ctranslate2
        import sentencepiece

        model = Path(os.environ["NLLB_MODEL"])
        _language = LANGUAGES[code]
        _spm = sentencepiece.SentencePieceProcessor(model_file=str(model / "sentencepiece.bpe.model"))
        _nllb = ctranslate2.Translator(str(model), device="cpu", compute_type="int8", intra_threads=NLLB_THREADS)
        return
    import argostranslate.translate

    with lock:  # одновременная загрузка моделей падает на Windows с отказом в доступе
        _translator = argostranslate.translate.get_translation_from_codes("en", "ru")
        _translator.translate("test")


def _nllb_translate(sentences: list[str]) -> list[str]:
    batch = [[_language.nllb, *_spm.encode(text, out_type=str), "</s>"] for text in sentences]
    results = _nllb.translate_batch(
        batch,
        target_prefix=[["rus_Cyrl"]] * len(batch),
        beam_size=4,
        max_batch_size=32,
        max_decoding_length=200,
        repetition_penalty=1.2,
        no_repeat_ngram_size=3,
    )
    return [_spm.decode(result.hypotheses[0][1:]) for result in results]


def _prepare(text: str, lower: bool) -> str:
    """Одинаковая подготовка до перевода: дроби, регистр, единицы и правки языка."""
    for symbol, fraction in VULGAR.items():
        text = text.replace(symbol, f" {fraction}")
    text = FRACTION_RE.sub(_decimal, text)
    if lower:  # Заглавные сбивают переводчик: «Turkey» становится страной
        text = text.lower()
    if _nllb:
        text = normalize_units(text)
        for pattern, value in _language.before:
            text = re.sub(pattern, value, text, flags=re.I)
    return text


def _prefetch(texts: list[tuple[str, bool]]) -> None:
    """Переводит все строки рецепта одной пачкой: по одной модель работает в разы медленнее."""
    _cache.clear()
    plan = []
    for text, lower in texts:
        if text:
            prepared = _prepare(text, lower)
            plan.append((prepared, SENTENCE_RE.split(prepared)))
    flat = list(dict.fromkeys(sentence for _, sentences in plan for sentence in sentences))
    done = dict(zip(flat, _nllb_translate(flat))) if flat else {}
    for prepared, sentences in plan:
        _cache[prepared] = " ".join(done[sentence] for sentence in sentences)


def _numbers(text: str) -> set[str]:
    return {number.replace(",", ".") for number in NUMBER_RE.findall(text)}


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
    text = _prepare(text, lower)
    if _nllb:
        result = _cache[text]
        for pattern, value in AFTER:
            result = pattern.sub(value, result)
        if not _numbers(text) - {"1"} <= _numbers(result):  # единицу пишут и словом: «один час»
            raise NumbersChanged(text)
        return result
    for pattern, value in BEFORE:
        text = pattern.sub(value, text)
    result = _translator.translate(text)
    for pattern, value in AFTER:
        result = pattern.sub(value, result)
    return result


def translate_page(page: dict) -> str:
    """Возвращает строку JSONL. Страница без рецепта тоже записывается, чтобы не разбирать снова."""
    if _language:
        name = page_name(_language, page["title"])
        markup = foreign_markup(_language, page["text"])
        url = foreign_page_url(_language, page["title"])
    else:
        name, markup, url = page["title"], to_extractor_markup(page["text"]), page_url(page["title"])
    try:
        recipes = extract_recipes(name, markup)
    except NoRecipeFound:
        recipes = []
    translated = []
    for recipe in recipes:
        if len(recipe.ingredients) + len(recipe.steps) > MAX_LINES:
            continue
        try:
            data = _translate_recipe(recipe, name, url)
        except NumbersChanged:
            continue
        if data:
            translated.append(data)
    return json.dumps(
        {"id": page["id"], "revision": page["revision"], "title": page["title"], "recipes": translated},
        ensure_ascii=False,
    )


def _translate_recipe(recipe, name: str, url: str) -> dict | None:
    data = asdict(recipe)
    variant = VARIANT_RE.search(recipe.title)
    suffix = variant.group(0) if variant else ""
    if _nllb:
        lines = [line["text"] for line in data["ingredients"]]
        fields = [data[key] for key in ("description", "category", "cuisine", "time_text", "calories_text")]
        groups = [line["group"] for line in data["ingredients"]]
        _prefetch(
            [(recipe.title.removesuffix(suffix), True), *((text, True) for text in lines)]
            + [(text, False) for text in fields + groups + recipe.steps + recipe.ingredient_notes]
        )
    title = translate(recipe.title.removesuffix(suffix), lower=True)
    if LATIN_RE.search(title):
        return None  # название не перевелось: по такому блюду не понять, что это
    data["title"] = title[:1].upper() + title[1:] + suffix
    data["source_url"] = recipe.source_url.replace(ru_page_url(name), url)
    if data["image_url"]:
        data["image_url"] = data["image_url"].replace(
            RU_BASE_URL, _language.base_url if _language else BASE_URL
        )
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
        return None  # непереведённый продукт нельзя сопоставить с запасами
    return data


def read_pages(dump: Path, done: set[str], code: str):
    prefix = LANGUAGES[code].prefix if code != "en" else "Cookbook:"
    with bz2.open(dump) as file:
        for _, element in ET.iterparse(file):
            if element.tag.rpartition("}")[2] != "page":
                continue
            title = element.findtext("{*}title") or ""
            if title.startswith(prefix) and element.find("{*}redirect") is None:
                page_id = element.findtext("{*}id")
                revision = element.find("{*}revision")
                text = revision.findtext("{*}text") or ""
                wanted = code != "en" or re.search(r"^=+\s*'*ingredients?", text, re.I | re.M)
                if page_id not in done and wanted:
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
    arguments.add_argument("--lang", choices=["en", *LANGUAGES], default="en")
    arguments.add_argument("--limit", type=int)
    options = arguments.parse_args()

    target = TRANSLATED_FILE if options.lang == "en" else translated_file(options.lang)
    target.parent.mkdir(parents=True, exist_ok=True)
    done = set()
    if target.exists():
        with target.open(encoding="utf-8") as file:
            done = {json.loads(line)["id"] for line in file}
    # Короткие страницы первыми: простые блюда нужнее всего и будут готовы раньше
    pages = sorted(read_pages(options.dump, done, options.lang), key=lambda page: len(page["text"]))
    pages = pages[: options.limit]
    print(f"Готово {len(done)}, осталось {len(pages)}", flush=True)

    with (
        multiprocessing.Manager() as manager,
        multiprocessing.Pool(
            WORKERS, initializer=_start_worker, initargs=(options.lang, manager.Lock())
        ) as pool,
        target.open("a", encoding="utf-8") as output,
    ):
        for number, line in enumerate(pool.imap(translate_page, pages), start=1):
            output.write(line + "\n")
            output.flush()
            if number % 50 == 0:
                print(number, flush=True)


if __name__ == "__main__":
    main()

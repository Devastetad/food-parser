"""Извлечение рецептов из вики-разметки страниц Кулинарной книги Викиучебника."""
import re
from dataclasses import dataclass, field
from urllib.parse import quote

import mwparserfromhell

from parser.base_parser import IngredientLine, NoRecipeFound, RawRecipe

BASE_URL = "https://ru.wikibooks.org/wiki/"
MAX_PLAIN_INGREDIENT = 120  # длиннее без маркера списка: это уже пояснение, а не продукт
MIN_INLINE_INGREDIENTS = 3  # столько пунктов списка нужно, чтобы считать раздел рецептом
MIN_INLINE_STEP = 40  # короче: подпись или ремарка, а не шаг приготовления

INGREDIENTS, STEPS, IGNORED, OTHER = "ingredients", "steps", "ignored", "other"
STEPS_RE = re.compile(r"приготовлен|инструкц|технолог|способ|как готовить|как варить")
INGREDIENTS_RE = re.compile(r"состав|ин?гр[еи]диент|продукт|компонент")
IGNORED_RE = re.compile(
    r"примечани|ссылк|см\. также|литератур|галере|источник|совет|подача|"
    r"оборудован|посуда|коммент|видео|истори"
)
GENERIC_HEADINGS = {"рецепт", "рецепты", "основной рецепт"}

NOISE_RE = re.compile(
    r"<!--.*?-->|<ref[^>]*/>|<ref[^>]*>.*?</ref>|<gallery.*?</gallery>|__[A-ZА-Я]+__|"
    r"</?(?:div|blockquote|center|span|small|big)\b[^>]*>",
    re.S | re.I,
)
LINE_BREAK_RE = re.compile(r"<br\s*/?>", re.I)
BROKEN_LINK_RE = re.compile(r"\[\[[^\]|]*\|?")
FOOTNOTE_RE = re.compile(r"\[\d+\]")
FILE_LINK_RE = re.compile(
    r"\[\[(?:Файл|File|Изображение|Image|Категория|Category):"
    r"(?:[^\[\]]|\[\[[^\[\]]*\]\])*\]\]",
    re.I,
)
HEADING_RE = re.compile(r"^(={2,6})\s*(.+?)\s*\1\s*$", re.M)
TABLE_RE = re.compile(r"^\{\|.*?^\|\}", re.S | re.M)
LIST_MARKER_RE = re.compile(r"^[*#•]+\s*")
INLINE_SEPARATOR_RE = re.compile(r"[,;]\s+(?![^()]*\))")
IMAGE_NAME_RE = re.compile(r"(?:\[\[)?(?:\w+:)?([^|\]\[]+\.\w{3,4})", re.I)


@dataclass
class _Section:
    level: int
    heading: str
    body: str


@dataclass
class _Draft:
    context: str | None
    ingredients: list[IngredientLine] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return bool(self.ingredients and self.steps)


def page_url(title: str) -> str:
    return BASE_URL + quote(title.replace(" ", "_"), safe=":/")


def extract_recipes(page_title: str, wikitext: str) -> list[RawRecipe]:
    """Возвращает все рецепты страницы: один вариант блюда даёт один рецепт."""
    text = LINE_BREAK_RE.sub(" ", NOISE_RE.sub("", wikitext))
    code = mwparserfromhell.parse(text)
    infobox = _read_infobox(code)
    for template in code.filter_templates(recursive=False):
        code.remove(template)
    text = FILE_LINK_RE.sub("", str(code))  # после карточки: в ней картинка бывает ссылкой

    first_heading = HEADING_RE.search(text)
    intro = text[: first_heading.start()] if first_heading else text
    sections = _split_sections(text)
    drafts = _group_variants(sections)
    if not drafts:
        raise NoRecipeFound(_explain_rejection(sections))

    name = page_title.split(":", 1)[-1].strip()
    url = page_url(page_title)
    titles = _variant_titles(name, drafts)
    recipes = []
    for number, (draft, title) in enumerate(zip(drafts, titles), start=1):
        recipes.append(
            RawRecipe(
                title=title,
                source_url=url if len(drafts) == 1 else f"{url}#вариант-{number}",
                ingredients=draft.ingredients,
                steps=draft.steps,
                ingredient_notes=draft.notes,
                description=_first_paragraph(intro),
                category=infobox.get("Категория"),
                cuisine=infobox.get("Кухня"),
                time_text=infobox.get("Время"),
                servings_text=infobox.get("Порций") or infobox.get("Порции"),
                difficulty_text=infobox.get("Сложность"),
                calories_text=infobox.get("Энергетическая ценность"),
                image_url=_image_url(infobox.get("Изображение")),
            )
        )
    return recipes


def _read_infobox(code) -> dict[str, str]:
    for template in code.filter_templates(recursive=False):
        if str(template.name).strip().lower() == "рецепт":
            values = {}
            for param in template.params:
                name = str(param.name).strip()
                # Из ссылки на файл strip_code оставил бы только подпись вроде «200px»
                value = str(param.value) if name == "Изображение" else param.value.strip_code()
                if value.strip():
                    values[name] = value.strip()
            return values
    return {}


def _image_url(value: str | None) -> str | None:
    match = IMAGE_NAME_RE.match(value.strip()) if value else None
    if not match:
        return None
    return f"{BASE_URL}Special:FilePath/{quote(match.group(1).strip().replace(' ', '_'))}"


def _split_sections(text: str) -> list[_Section]:
    matches = list(HEADING_RE.finditer(text))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections.append(
            _Section(len(match.group(1)), _clean(match.group(2)), text[match.end() : end])
        )
    return sections


def _classify(heading: str) -> str:
    # Латинская «c» в «Cостав» встречается в реальных страницах
    normalized = heading.lower().replace("ё", "е").replace("c", "с").strip(" :")
    if normalized in GENERIC_HEADINGS:
        return OTHER
    if STEPS_RE.search(normalized):
        return STEPS
    if INGREDIENTS_RE.search(normalized):
        return INGREDIENTS
    if IGNORED_RE.search(normalized):
        return IGNORED
    return OTHER


def _group_variants(sections: list[_Section]) -> list[_Draft]:
    """Собирает разделы в варианты: новый состав после готовых шагов начинает новый рецепт."""
    drafts: list[_Draft] = []
    draft = _Draft(context=None)
    active_kind = None
    active_level = 0

    def flush() -> None:
        nonlocal draft
        if draft.complete:
            drafts.append(draft)
        draft = _Draft(context=draft.context)

    for section in sections:
        if active_kind and section.level > active_level:  # подраздел: «Тесто» внутри «Состава»
            if _classify(section.heading) == STEPS:  # бывает вложен в «Ингредиенты»
                draft.steps += _read_steps(section.body)
                active_kind = STEPS
            elif active_kind == INGREDIENTS:
                _add_ingredients(draft, section.body, section.heading)
            elif active_kind == STEPS:
                draft.steps += _read_steps(section.body)
            continue

        kind = _classify(section.heading)
        active_kind, active_level = kind, section.level
        if kind == INGREDIENTS:
            if draft.complete:
                flush()
            ingredients_body, inline_steps = _split_numbered_steps(section.body)
            _add_ingredients(draft, ingredients_body)
            draft.steps += inline_steps
        elif kind == STEPS:
            draft.steps += _read_steps(section.body)
        elif kind == IGNORED:
            flush()
        elif _is_generic(section.heading) and draft.ingredients and not draft.steps:
            # «== Рецепт ==» сразу после состава: это шаги
            draft.steps += _read_steps(section.body)
            active_kind = STEPS
        elif draft.ingredients and not draft.steps and _has_list(section.body):
            # «== Тесто ==» рядом с «== Состав ==», а не внутри него
            _add_ingredients(draft, section.body, section.heading)
            active_kind = INGREDIENTS
        else:
            flush()
            draft.context = None if _is_generic(section.heading) else section.heading
            active_kind = None
            _read_inline_recipe(draft, section.body)
            if draft.complete:
                flush()
    flush()
    return drafts


def _split_numbered_steps(body: str) -> tuple[str, list[str]]:
    """В разделе состава с двумя списками маркированный — продукты, нумерованный — шаги."""
    lines = body.splitlines()
    numbered = [line for line in lines if line.startswith("#")]
    if not numbered or not any(line.startswith("*") for line in lines):
        return body, []
    rest = [line for line in lines if not line.startswith("#")]
    return "\n".join(rest), _read_steps("\n".join(numbered))


def _read_inline_recipe(draft: _Draft, body: str) -> None:
    """Раздел без подзаголовков: список продуктов, а после него текст приготовления."""
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    bullets = [index for index, line in enumerate(lines) if line.startswith(("*", "•"))]
    if len(bullets) < MIN_INLINE_INGREDIENTS:
        return
    steps = _read_steps("\n".join(lines[bullets[-1] + 1 :]))
    steps = [step for step in steps if len(step) >= MIN_INLINE_STEP]
    if steps:
        _add_ingredients(draft, "\n".join(lines[index] for index in bullets))
        draft.steps += steps


def _is_generic(heading: str) -> bool:
    return heading.lower().strip(" :") in GENERIC_HEADINGS


def _variant_titles(name: str, drafts: list[_Draft]) -> list[str]:
    if len(drafts) == 1:
        return [name]
    titles = []
    for number, draft in enumerate(drafts, start=1):
        if not draft.context:
            title = f"{name} (вариант {number})"
        elif name.lower() in draft.context.lower():
            title = draft.context
        else:
            title = f"{name}: {draft.context}"
        if title in titles:
            title = f"{title} (вариант {number})"
        titles.append(title)
    return titles


def _explain_rejection(sections: list[_Section]) -> str:
    kinds = {_classify(section.heading) for section in sections}
    if INGREDIENTS in kinds and STEPS in kinds:
        return "Разделы состава и приготовления пусты"
    if INGREDIENTS in kinds:
        return "Нет раздела приготовления"
    if STEPS in kinds:
        return "Нет раздела состава"
    return "Нет разделов состава и приготовления"


def _has_list(body: str) -> bool:
    return any(LIST_MARKER_RE.match(line.strip()) for line in body.splitlines())


def _add_ingredients(draft: _Draft, body: str, group: str | None = None) -> None:
    body = TABLE_RE.sub(lambda match: _table_to_list(match.group(0)), body)
    has_list = _has_list(body)
    for raw_line in body.splitlines():
        line = raw_line.strip()
        is_group_marker = line.startswith(";")
        is_item = bool(LIST_MARKER_RE.match(line))
        text = _clean(LIST_MARKER_RE.sub("", line).lstrip(";:"))
        if not text:
            continue
        if is_group_marker or (text.endswith(":") and len(text) <= 60):
            group = text.rstrip(":")
        elif is_item:
            draft.ingredients.append(IngredientLine(text, group))
        elif has_list:
            draft.notes.append(text)
        else:
            for part in INLINE_SEPARATOR_RE.split(text):
                part = part.strip(" .")
                if len(part) > MAX_PLAIN_INGREDIENT:
                    draft.notes.append(part)
                elif part:
                    draft.ingredients.append(IngredientLine(part, group))


def _table_to_list(table: str) -> str:
    """Таблицу превращает в маркированный список: одна строка таблицы — один продукт."""
    items = []
    for row in re.split(r"^\|-.*$", table, flags=re.M):
        cells = []
        for line in row.splitlines():
            line = line.strip()
            if not line.startswith("|") or line.startswith(("|}", "|+")):
                continue
            for cell in line[1:].split("||"):
                # Атрибуты отделены чертой: «style="…" | текст». Вики-ссылки не трогаем.
                cell = re.sub(r"^[^\[\]]*\|", "", cell).strip()
                if cell and cell not in {"—", "-", "–"}:
                    cells.append(cell)
        if cells:
            items.append("* " + " ".join(cells))
    return "\n".join(items)


def _read_steps(body: str) -> list[str]:
    body = TABLE_RE.sub(lambda match: _table_to_list(match.group(0)), body)
    steps = []
    for raw_line in body.splitlines():
        text = _clean(re.sub(r"^[*#•:;]+\s*", "", raw_line.strip()))
        if len(text) >= 3:
            steps.append(text)
    return steps


def _first_paragraph(intro: str) -> str | None:
    for block in re.split(r"\n\s*\n", intro):
        text = _clean(block)
        if text:
            return text
    return None


def _clean(text: str) -> str:
    plain = mwparserfromhell.parse(text).strip_code(normalize=True, collapse=True)
    plain = FOOTNOTE_RE.sub("", BROKEN_LINK_RE.sub("", plain))  # страницы бывают с битой разметкой
    return re.sub(r"\s+", " ", plain).strip(" \t.,;|")

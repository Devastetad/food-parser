"""Поиск дублей по содержанию: один рецепт бывает на разных страницах источника."""
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher

MIN_STEPS_SIMILARITY = 0.85


@dataclass
class _Known:
    title: str
    steps: str


class DuplicateIndex:
    """Дубль: тот же состав с теми же количествами и то же название или почти те же шаги.

    Рецепты с разными количествами дублями не считаются: «Соус Хрен 1» и «Соус Хрен 2» —
    разные варианты блюда.
    """

    def __init__(self):
        self._by_composition: dict[frozenset, list[_Known]] = defaultdict(list)

    def add_if_new(self, title: str, composition: list[tuple], steps: list[str]) -> bool:
        """Запоминает рецепт. Возвращает False, если такой уже встречался."""
        key = frozenset(composition)
        text = " ".join(steps).lower()
        for known in self._by_composition[key]:
            if known.title.lower() == title.lower() or _similar(known.steps, text):
                return False
        self._by_composition[key].append(_Known(title, text))
        return True


def _similar(first: str, second: str) -> bool:
    return SequenceMatcher(None, first, second).ratio() >= MIN_STEPS_SIMILARITY

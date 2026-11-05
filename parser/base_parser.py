"""Контракт источника рецептов и общий цикл сбора.

Новый источник: класс-наследник BaseSource в parser/sources и строка в SOURCES
в parser/sources/__init__.py. Цикл сбора, прогресс и защита от повторов общие.
"""
import json
from abc import ABC, abstractmethod
from collections.abc import Iterator
from contextlib import closing
from dataclasses import asdict, dataclass, field
from pathlib import Path

import config
from database.database import connect, transaction


class SourceUnavailable(Exception):
    """Источник недоступен, локальной копии его данных нет."""


class NoRecipeFound(Exception):
    """На странице нет рецепта. Текст исключения объясняет почему."""


@dataclass(frozen=True)
class SourceItem:
    """Страница источника. revision меняется, когда её надо обработать заново."""

    item_id: str
    revision: str
    title: str
    content: str


@dataclass
class IngredientLine:
    text: str
    group: str | None = None  # «Для теста», «Для начинки»


@dataclass
class RawRecipe:
    """Рецепт как в источнике: числа и единицы разбирает этап нормализации."""

    title: str
    source_url: str
    ingredients: list[IngredientLine]
    steps: list[str]
    ingredient_notes: list[str] = field(default_factory=list)
    description: str | None = None
    category: str | None = None
    cuisine: str | None = None
    time_text: str | None = None
    servings_text: str | None = None
    difficulty_text: str | None = None
    calories_text: str | None = None
    image_url: str | None = None


class BaseSource(ABC):
    name: str
    license: str

    @abstractmethod
    def prepare(self) -> None:
        """Проверяет доступность источника. При отказе бросает SourceUnavailable."""

    @abstractmethod
    def iter_items(self) -> Iterator[SourceItem]:
        """Перебирает страницы источника в постоянном порядке."""

    @abstractmethod
    def extract(self, item: SourceItem) -> list[RawRecipe]:
        """Возвращает рецепты страницы. Если их нет, бросает NoRecipeFound."""


@dataclass
class RunStats:
    parsed: int = 0
    rejected: int = 0
    errors: int = 0
    skipped: int = 0
    recipes: int = 0


def run_source(
    source: BaseSource, db_path: Path = config.DB_PATH, limit: int | None = None
) -> RunStats:
    """Собирает рецепты. limit ограничивает число обработанных за запуск страниц.

    Каждая страница сохраняется своей транзакцией, поэтому прерванный сбор при новом
    запуске пропустит готовое и продолжит с места остановки.
    """
    stats = RunStats()
    with closing(connect(db_path)) as conn:
        conn.execute(
            "UPDATE parser_runs SET status = 'interrupted', finished_at = CURRENT_TIMESTAMP "
            "WHERE source = ? AND status = 'running'",
            (source.name,),
        )
        run_id = conn.execute(
            "INSERT INTO parser_runs (source) VALUES (?)", (source.name,)
        ).lastrowid
        try:
            source.prepare()
            for item in source.iter_items():
                if limit is not None and stats.parsed + stats.rejected + stats.errors >= limit:
                    break
                if _is_done(conn, source.name, item):
                    stats.skipped += 1
                else:
                    _process(conn, source, item, stats)
        except BaseException as error:
            status = "failed" if isinstance(error, Exception) else "interrupted"
            _finish_run(conn, run_id, status, stats, str(error))
            raise
        _finish_run(conn, run_id, "finished", stats)
    return stats


def _is_done(conn, source: str, item: SourceItem) -> bool:
    """Ошибочные страницы не считаются готовыми: их пробуем при каждом запуске."""
    row = conn.execute(
        "SELECT revision, status FROM raw_items WHERE source = ? AND item_id = ?",
        (source, item.item_id),
    ).fetchone()
    return row is not None and row["revision"] == item.revision and row["status"] != "error"


def _process(conn, source: BaseSource, item: SourceItem, stats: RunStats) -> None:
    recipes: list[RawRecipe] = []
    reason = None
    try:
        recipes = source.extract(item)
        status = "parsed"
        stats.parsed += 1
        stats.recipes += len(recipes)
    except NoRecipeFound as error:
        status, reason = "rejected", str(error)
        stats.rejected += 1
    except Exception as error:  # сбой на одной странице не должен останавливать весь сбор
        status, reason = "error", f"{type(error).__name__}: {error}"
        stats.errors += 1
    _save_item(conn, source, item, status, reason, recipes)


def _save_item(conn, source, item, status, reason, recipes) -> None:
    with transaction(conn):
        raw_item_id = conn.execute(
            "INSERT INTO raw_items (source, item_id, revision, title, status, reason) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT (source, item_id) DO UPDATE SET revision = excluded.revision, "
            "title = excluded.title, status = excluded.status, reason = excluded.reason, "
            "processed_at = CURRENT_TIMESTAMP RETURNING id",
            (source.name, item.item_id, item.revision, item.title, status, reason),
        ).fetchone()[0]
        conn.execute("DELETE FROM raw_recipes WHERE raw_item_id = ?", (raw_item_id,))
        conn.executemany(
            "INSERT INTO raw_recipes (raw_item_id, variant, title, source_url, license, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    raw_item_id,
                    variant,
                    recipe.title,
                    recipe.source_url,
                    source.license,
                    json.dumps(asdict(recipe), ensure_ascii=False),
                )
                for variant, recipe in enumerate(recipes, start=1)
            ],
        )


def _finish_run(conn, run_id: int, status: str, stats: RunStats, message: str | None = None):
    conn.execute(
        "UPDATE parser_runs SET finished_at = CURRENT_TIMESTAMP, status = ?, parsed = ?, "
        "rejected = ?, errors = ?, skipped = ?, recipes = ?, message = ? WHERE id = ?",
        (
            status,
            stats.parsed,
            stats.rejected,
            stats.errors,
            stats.skipped,
            stats.recipes,
            message,
            run_id,
        ),
    )

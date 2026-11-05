"""История приготовленных блюд. Рецепт в базе и приготовленное блюдо — разные сущности."""
import sqlite3
from dataclasses import dataclass
from datetime import date

from core.inventory_manager import get_item, mark_finished
from database.database import transaction

RATINGS = {
    "liked": "Понравилось",
    "ok": "Нормально",
    "disliked": "Не понравилось",
    "tired": "Надоело",
}
MAX_COMMENT_LENGTH = 1000
ROUNDING_TOLERANCE = 0.001


class HistoryError(ValueError):
    """Данные не подходят. Текст исключения показывается пользователю."""


@dataclass
class HistoryEntry:
    id: int
    recipe_id: int
    title: str
    cooked_on: date
    rating: str | None
    comment: str | None
    servings: int | None


@dataclass
class CookedSummary:
    """Всё, что важно планировщику о блюде: сколько раз, когда и как оценено последний раз."""

    recipe_id: int
    times: int
    last_cooked: date
    last_rating: str | None


def _check(rating: str | None, comment: str | None) -> tuple[str | None, str | None]:
    if rating is not None and rating not in RATINGS:
        raise HistoryError(f"Неизвестная оценка: {rating}")
    comment = (comment or "").strip() or None
    if comment and len(comment) > MAX_COMMENT_LENGTH:
        raise HistoryError(f"Комментарий длиннее {MAX_COMMENT_LENGTH} символов")
    return rating, comment


def record_cooked(
    conn: sqlite3.Connection,
    recipe_id: int,
    cooked_on: date,
    servings: int,
    rating: str | None = None,
    comment: str | None = None,
    deductions: list[tuple[int, float]] = (),
    today: date | None = None,
) -> int:
    """Записывает приготовление и списывает подтверждённое количество одной транзакцией."""
    rating, comment = _check(rating, comment)
    if cooked_on > (today or date.today()):
        raise HistoryError("Дата приготовления ещё не наступила")
    if servings is None or servings <= 0:
        raise HistoryError("Количество порций должно быть больше нуля")
    if conn.execute("SELECT 1 FROM recipes WHERE id = ?", (recipe_id,)).fetchone() is None:
        raise HistoryError("Рецепт не найден")

    with transaction(conn):
        entry_id = conn.execute(
            "INSERT INTO cooking_history (recipe_id, cooked_on, rating, comment, servings) "
            "VALUES (?, ?, ?, ?, ?)",
            (recipe_id, cooked_on.isoformat(), rating, comment, servings),
        ).lastrowid
        for inventory_id, amount in deductions:
            _deduct(conn, inventory_id, amount)
        _mark_in_plan(conn, recipe_id, cooked_on)
    return entry_id


def _mark_in_plan(conn: sqlite3.Connection, recipe_id: int, cooked_on: date) -> None:
    """Блюдо из плана отмечается приготовленным: список покупок больше на него не считает."""
    row = conn.execute(
        "SELECT i.id FROM meal_plan_items i WHERE i.recipe_id = ? AND i.cooked_on IS NULL "
        "ORDER BY i.plan_id DESC, i.day_index LIMIT 1",
        (recipe_id,),
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE meal_plan_items SET cooked_on = ? WHERE id = ?",
            (cooked_on.isoformat(), row["id"]),
        )


def _deduct(conn: sqlite3.Connection, inventory_id: int, amount: float) -> None:
    if amount < 0:
        raise HistoryError("Нельзя списать отрицательное количество")
    if amount == 0:
        return
    item = get_item(conn, inventory_id)
    left = item.quantity - amount
    if left < -ROUNDING_TOLERANCE:
        raise HistoryError(f"{item.name}: дома меньше, чем указано к списанию")
    if left <= ROUNDING_TOLERANCE:
        mark_finished(conn, inventory_id)
    else:
        conn.execute("UPDATE inventory SET quantity = ? WHERE id = ?", (left, inventory_id))


def list_history(conn: sqlite3.Connection) -> list[HistoryEntry]:
    rows = conn.execute(
        "SELECT h.*, r.title FROM cooking_history h JOIN recipes r ON r.id = h.recipe_id "
        "ORDER BY h.cooked_on DESC, h.id DESC"
    ).fetchall()
    return [
        HistoryEntry(
            row["id"], row["recipe_id"], row["title"], date.fromisoformat(row["cooked_on"]),
            row["rating"], row["comment"], row["servings"],
        )
        for row in rows
    ]  # fmt: skip


def update_entry(
    conn: sqlite3.Connection, entry_id: int, rating: str | None, comment: str | None
) -> None:
    rating, comment = _check(rating, comment)
    conn.execute(
        "UPDATE cooking_history SET rating = ?, comment = ? WHERE id = ?",
        (rating, comment, entry_id),
    )


def delete_entry(conn: sqlite3.Connection, entry_id: int) -> None:
    """Удаляет запись. Списанные продукты не возвращаются: они уже съедены."""
    conn.execute("DELETE FROM cooking_history WHERE id = ?", (entry_id,))


def cooked_recipe_ids(conn: sqlite3.Connection) -> set[int]:
    return {row[0] for row in conn.execute("SELECT DISTINCT recipe_id FROM cooking_history")}


def cooked_summary(conn: sqlite3.Connection) -> dict[int, CookedSummary]:
    summary: dict[int, CookedSummary] = {}
    for row in conn.execute(
        "SELECT recipe_id, cooked_on, rating FROM cooking_history ORDER BY cooked_on, id"
    ):
        cooked_on = date.fromisoformat(row["cooked_on"])
        known = summary.get(row["recipe_id"])
        summary[row["recipe_id"]] = CookedSummary(
            row["recipe_id"],
            (known.times if known else 0) + 1,
            cooked_on,
            row["rating"] or (known.last_rating if known else None),
        )
    return summary

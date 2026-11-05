"""Хранение готовых планов питания."""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from core.history_manager import cooked_recipe_ids
from core.meal_planner import MEALS, NEW_ONLY, Plan, PlanRuleViolation
from database.database import transaction


@dataclass
class SavedItem:
    id: int
    day: int
    meal: str
    recipe_id: int
    title: str
    servings: int
    cooked_on: date | None


@dataclass
class SavedPlan:
    id: int
    name: str
    start: date
    days: int
    people: int
    mode: str
    warnings: list[str]
    items: list[SavedItem]

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)

    def items_on(self, day: date) -> list[SavedItem]:
        return [item for item in self.items if item.day == (day - self.start).days]


def period_name(days: int) -> str:
    return {1: "День", 7: "Неделя", 30: "Месяц"}.get(days, f"{days} дн.")


def save_plan(conn: sqlite3.Connection, plan: Plan) -> int:
    """Сохраняет план. Режим B ещё раз сверяется с историей на момент сохранения."""
    request = plan.request
    if request.mode == NEW_ONLY:
        cooked = cooked_recipe_ids(conn)
        old = [item.title for item in plan.items if item.recipe_id in cooked]
        if old:
            raise PlanRuleViolation(f"Эти блюда уже приготовлены: {', '.join(old)}")
    name = f"{period_name(request.days)} с {request.start:%d.%m.%Y}"
    with transaction(conn):
        plan_id = conn.execute(
            "INSERT INTO meal_plans (name, start_date, days, people, mode, warnings) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                name,
                request.start.isoformat(),
                request.days,
                request.people,
                request.mode,
                json.dumps(plan.warnings, ensure_ascii=False),
            ),
        ).lastrowid
        conn.executemany(
            "INSERT INTO meal_plan_items (plan_id, day_index, meal, recipe_id, servings) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (plan_id, item.day, item.meal, item.recipe_id, item.servings)
                for item in plan.items
            ],
        )
    return plan_id


def _read(conn: sqlite3.Connection, row: sqlite3.Row) -> SavedPlan:
    items = [
        SavedItem(
            item["id"], item["day_index"], item["meal"], item["recipe_id"], item["title"],
            item["servings"],
            date.fromisoformat(item["cooked_on"]) if item["cooked_on"] else None,
        )
        for item in conn.execute(
            "SELECT i.*, r.title FROM meal_plan_items i JOIN recipes r ON r.id = i.recipe_id "
            "WHERE i.plan_id = ? ORDER BY i.day_index, i.id",
            (row["id"],),
        )
    ]  # fmt: skip
    return SavedPlan(
        row["id"], row["name"], date.fromisoformat(row["start_date"]), row["days"], row["people"],
        row["mode"], json.loads(row["warnings"]), items,
    )  # fmt: skip


def list_plans(conn: sqlite3.Connection) -> list[SavedPlan]:
    rows = conn.execute("SELECT * FROM meal_plans ORDER BY id DESC").fetchall()
    return [_read(conn, row) for row in rows]


def get_plan(conn: sqlite3.Connection, plan_id: int) -> SavedPlan | None:
    row = conn.execute("SELECT * FROM meal_plans WHERE id = ?", (plan_id,)).fetchone()
    return _read(conn, row) if row else None


def current_plan(conn: sqlite3.Connection, today: date | None = None) -> SavedPlan | None:
    """Самый свежий план, в период которого попадает сегодняшний день."""
    today = today or date.today()
    for plan in list_plans(conn):
        if plan.start <= today <= plan.end:
            return plan
    return None


def delete_plan(conn: sqlite3.Connection, plan_id: int) -> None:
    conn.execute("DELETE FROM meal_plans WHERE id = ?", (plan_id,))


def meal_title(meal: str) -> str:
    return MEALS[meal]

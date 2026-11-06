"""Хранение готовых планов питания."""
import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from core.history_manager import cooked_recipe_ids
from core.meal_planner import (
    MEALS,
    NEW_ONLY,
    Plan,
    PlanRequest,
    PlanRuleViolation,
    Replacement,
    find_replacements,
)
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
    params: dict

    @property
    def end(self) -> date:
        return self.start + timedelta(days=self.days - 1)

    def items_on(self, day: date) -> list[SavedItem]:
        return [item for item in self.items if item.day == (day - self.start).days]


def period_name(days: int) -> str:
    return {1: "День", 7: "Неделя", 30: "Месяц"}.get(days, f"{days} дн.")


def _params_of(request: PlanRequest) -> dict:
    return {
        "meals_per_day": request.meals_per_day,
        "max_minutes": request.max_minutes,
        "include_unknown_time": request.include_unknown_time,
        "budget": request.budget,
        "diets": request.diets,
        "excluded": request.excluded,
        "equipment": None if request.equipment is None else sorted(request.equipment),
        "allow_old": request.allow_old,
        "pinned": request.pinned,
        "target_kcal": request.target_kcal,
        "only_from_stock": request.only_from_stock,
    }


def _request_of(plan: "SavedPlan") -> PlanRequest:
    """Условия плана. У планов, сохранённых раньше, их нет: действуют значения по умолчанию."""
    params = dict(plan.params)
    if params.get("equipment") is not None:
        params["equipment"] = set(params["equipment"])
    return PlanRequest(
        start=plan.start, days=plan.days, people=plan.people, mode=plan.mode, **params
    )


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
            "INSERT INTO meal_plans (name, start_date, days, people, mode, warnings, params) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                request.start.isoformat(),
                request.days,
                request.people,
                request.mode,
                json.dumps(plan.warnings, ensure_ascii=False),
                json.dumps(_params_of(request), ensure_ascii=False),
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
        row["mode"], json.loads(row["warnings"]), items, json.loads(row["params"]),
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


def replacements_for(
    conn: sqlite3.Connection, plan: SavedPlan, item: SavedItem, limit: int = 5
) -> list[Replacement]:
    others = [
        (other.day, other.meal, other.recipe_id, other.cooked_on is not None)
        for other in plan.items
        if other.id != item.id
    ]
    return find_replacements(
        conn, _request_of(plan), others, item.day, item.meal, item.recipe_id, limit
    )


def replace_item(conn: sqlite3.Connection, plan: SavedPlan, item: SavedItem, recipe_id: int) -> None:
    """Меняет блюдо. Режим «только новые» сверяется с историей и здесь."""
    if plan.mode == NEW_ONLY and recipe_id in cooked_recipe_ids(conn):
        raise PlanRuleViolation("Это блюдо уже приготовлено, а план только из новых")
    conn.execute(
        "UPDATE meal_plan_items SET recipe_id = ? WHERE id = ? AND cooked_on IS NULL",
        (recipe_id, item.id),
    )


def meal_title(meal: str) -> str:
    return MEALS[meal]

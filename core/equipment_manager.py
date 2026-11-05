"""Кухонная техника пользователя."""
import sqlite3
from dataclasses import dataclass

MAX_NAME_LENGTH = 60
BUILT_IN = {
    "Плита", "Духовка", "Микроволновка", "Мультиварка", "Аэрогриль", "Блендер", "Миксер", "Гриль",
}  # fmt: skip


class EquipmentError(ValueError):
    """Действие невозможно. Текст исключения показывается пользователю."""


@dataclass
class Equipment:
    id: int
    name: str
    is_available: bool
    recipes: int

    @property
    def is_custom(self) -> bool:
        return self.name not in BUILT_IN


def list_equipment(conn: sqlite3.Connection) -> list[Equipment]:
    rows = conn.execute(
        "SELECT e.id, e.name, e.is_available, count(r.recipe_id) AS recipes "
        "FROM kitchen_equipment e LEFT JOIN recipe_equipment r ON r.equipment_id = e.id "
        "GROUP BY e.id ORDER BY e.id"
    ).fetchall()
    return [
        Equipment(row["id"], row["name"], bool(row["is_available"]), row["recipes"]) for row in rows
    ]


def set_available(conn: sqlite3.Connection, equipment_id: int, available: bool) -> None:
    conn.execute(
        "UPDATE kitchen_equipment SET is_available = ? WHERE id = ?",
        (int(available), equipment_id),
    )


def add_equipment(conn: sqlite3.Connection, name: str) -> int:
    """Новый прибор сразу считается доступным: пользователь добавляет то, что у него есть."""
    name = " ".join((name or "").split())
    if not name:
        raise EquipmentError("Укажите название прибора")
    if len(name) > MAX_NAME_LENGTH:
        raise EquipmentError(f"Название длиннее {MAX_NAME_LENGTH} символов")
    # lower() в Python: в SQLite сравнение без учёта регистра не работает для кириллицы
    known = {row["name"].casefold() for row in conn.execute("SELECT name FROM kitchen_equipment")}
    if name.casefold() in known:
        raise EquipmentError(f"«{name}» уже есть в списке")
    return conn.execute(
        "INSERT INTO kitchen_equipment (name, is_available) VALUES (?, 1)", (name,)
    ).lastrowid


def delete_equipment(conn: sqlite3.Connection, equipment_id: int) -> None:
    """Удаляет только свой прибор: стандартный список ТЗ всегда на месте."""
    row = conn.execute("SELECT name FROM kitchen_equipment WHERE id = ?", (equipment_id,)).fetchone()
    if row and row["name"] in BUILT_IN:
        raise EquipmentError("Стандартный прибор можно только выключить")
    try:
        conn.execute("DELETE FROM kitchen_equipment WHERE id = ?", (equipment_id,))
    except sqlite3.IntegrityError as error:
        raise EquipmentError("Прибор нужен рецептам, его можно только выключить") from error

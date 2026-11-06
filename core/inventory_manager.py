"""Инвентарь продуктов: партии с количеством и сроком годности."""
import math
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta

from core.ingredient_matcher import (
    add_alias,
    find_ingredient,
    generic_ingredient_ids,
    is_nominative,
    to_nominative,
)
from core.units import INVENTORY_UNITS, convert, to_grams
from database.database import transaction
from parser.normalizer import parse_ingredient

MAX_NAME_LENGTH = 100
EXPIRING_SOON_DAYS = 3
_UNCHANGED = object()
LIST_SEPARATOR_RE = re.compile(r"[;\n]+|,\s+")  # «1,5 кг» без пробела не разрывается


class InventoryError(ValueError):
    """Данные не подходят. Текст исключения показывается пользователю."""


@dataclass
class InventoryItem:
    id: int
    name: str
    ingredient_id: int | None
    ingredient_name: str | None
    quantity: float
    unit: str
    added_on: date
    expires_on: date | None
    finished_on: date | None

    def days_left(self, today: date | None = None) -> int | None:
        if self.expires_on is None:
            return None
        return (self.expires_on - (today or date.today())).days


def _check_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise InventoryError("Укажите название продукта")
    if len(name) > MAX_NAME_LENGTH:
        raise InventoryError(f"Название длиннее {MAX_NAME_LENGTH} символов")
    return name


def _check_quantity(quantity: float, allow_zero: bool = False) -> float:
    if quantity is None or not math.isfinite(quantity):
        raise InventoryError("Укажите количество числом")
    if quantity < 0 or (quantity == 0 and not allow_zero):
        raise InventoryError("Количество должно быть больше нуля")
    return float(quantity)


def parse_quantity(text: str) -> float:
    """Количество из текста. Принимает и запятую, и точку: числовая ячейка Streamlit
    при русской локали молча выбрасывает точку, и 0.5 превращается в 5."""
    try:
        return float(str(text).strip().replace(",", "."))
    except ValueError as error:
        raise InventoryError(f"«{text}» не похоже на количество") from error


def _check_unit(unit: str) -> str:
    if unit not in INVENTORY_UNITS:
        raise InventoryError(f"Неизвестная единица: {unit}")
    return unit


def _iso(value: date | None) -> str | None:
    return value.isoformat() if value else None


def add_item(
    conn: sqlite3.Connection,
    name: str,
    quantity: float,
    unit: str,
    expires_on: date | None = None,
) -> int:
    """Добавляет продукт. Такая же партия (продукт, единица, срок) просто пополняется."""
    name = _check_name(name)
    quantity = _check_quantity(quantity)
    unit = _check_unit(unit)
    ingredient_id = find_ingredient(conn, name)

    same_product = "ingredient_id = ?" if ingredient_id else "ingredient_id IS NULL AND name = ?"
    existing = conn.execute(
        f"SELECT id FROM inventory WHERE {same_product} AND unit = ? "
        "AND expires_on IS ? AND finished_on IS NULL",
        (ingredient_id or name, unit, _iso(expires_on)),
    ).fetchone()
    if existing:
        conn.execute(
            "UPDATE inventory SET quantity = quantity + ? WHERE id = ?", (quantity, existing["id"])
        )
        return existing["id"]
    return conn.execute(
        "INSERT INTO inventory (name, ingredient_id, quantity, unit, expires_on) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, ingredient_id, quantity, unit, _iso(expires_on)),
    ).lastrowid


@dataclass
class QuickItem:
    name: str
    quantity: float
    unit: str


def parse_quick_list(text: str) -> list[QuickItem]:
    """Разбирает «молоко 1 л, яйца 10, лук 1 кг». Без количества считаем 1 шт."""
    items = []
    for part in LIST_SEPARATOR_RE.split(text or ""):
        for parsed in parse_ingredient(part.strip(), bare_unit="шт") if part.strip() else []:
            name = parsed.name if is_nominative(parsed.name) else to_nominative(parsed.name)
            quantity, unit = parsed.quantity or 1.0, parsed.unit or "шт"
            if unit not in INVENTORY_UNITS:
                quantity, unit = to_grams(quantity, unit, name), "г"
            if unit in ("г", "мл") and quantity >= 1000:  # нормализатор переводит кг в граммы
                quantity, unit = quantity / 1000, "кг" if unit == "г" else "л"
            items.append(QuickItem(name[:1].upper() + name[1:], quantity, unit))
    return items


def add_quick_items(conn: sqlite3.Connection, items: list[QuickItem]) -> int:
    """Добавляет список целиком: при ошибке в одной строке не добавится ничего."""
    with transaction(conn):
        for item in items:
            add_item(conn, item.name, item.quantity, item.unit)
    return len(items)


def get_item(conn: sqlite3.Connection, item_id: int) -> InventoryItem:
    items = _select(conn, "WHERE inv.id = ?", (item_id,))
    if not items:
        raise InventoryError("Продукт уже удалён")
    return items[0]


def list_items(conn: sqlite3.Connection) -> list[InventoryItem]:
    """Продукты дома: сначала те, что испортятся раньше, без срока — в конце."""
    return _select(
        conn,
        "WHERE inv.finished_on IS NULL "
        "ORDER BY inv.expires_on IS NULL, inv.expires_on, inv.name COLLATE NOCASE",
    )


def list_finished(conn: sqlite3.Connection) -> list[InventoryItem]:
    return _select(conn, "WHERE inv.finished_on IS NOT NULL ORDER BY inv.finished_on DESC, inv.id DESC")


def list_expiring(
    conn: sqlite3.Connection, days: int = EXPIRING_SOON_DAYS, today: date | None = None
) -> list[InventoryItem]:
    """Просроченные и те, что испортятся в ближайшие дни."""
    limit = (today or date.today()) + timedelta(days=days)
    return _select(
        conn,
        "WHERE inv.finished_on IS NULL AND inv.expires_on <= ? ORDER BY inv.expires_on",
        (limit.isoformat(),),
    )


def list_unmatched(conn: sqlite3.Connection) -> list[InventoryItem]:
    """Продукты, которых система не нашла в базе: рецепты их пока не учитывают."""
    return _select(conn, "WHERE inv.finished_on IS NULL AND inv.ingredient_id IS NULL ORDER BY inv.name")


def _select(conn, condition: str, parameters: tuple = ()) -> list[InventoryItem]:
    rows = conn.execute(
        "SELECT inv.*, ing.name AS ingredient_name FROM inventory inv "
        f"LEFT JOIN ingredients ing ON ing.id = inv.ingredient_id {condition}",
        parameters,
    ).fetchall()
    return [
        InventoryItem(
            id=row["id"],
            name=row["name"],
            ingredient_id=row["ingredient_id"],
            ingredient_name=row["ingredient_name"],
            quantity=row["quantity"],
            unit=row["unit"],
            added_on=date.fromisoformat(row["added_on"]),
            expires_on=date.fromisoformat(row["expires_on"]) if row["expires_on"] else None,
            finished_on=date.fromisoformat(row["finished_on"]) if row["finished_on"] else None,
        )
        for row in rows
    ]


def update_item(
    conn: sqlite3.Connection,
    item_id: int,
    quantity: float | None = None,
    unit: str | None = None,
    expires_on: date | None | object = _UNCHANGED,
) -> None:
    """Меняет поля партии. Новая единица без нового количества пересчитывает его:
    1 кг станет 1000 г. Несовместимая единица оставляет число как есть."""
    item = get_item(conn, item_id)
    new_unit = _check_unit(unit) if unit is not None else item.unit
    if quantity is not None:
        new_quantity = _check_quantity(quantity, allow_zero=True)
    else:
        new_quantity = convert(item.quantity, item.unit, new_unit)
        if new_quantity is None:
            new_quantity = item.quantity
    new_expiry = item.expires_on if expires_on is _UNCHANGED else expires_on

    if new_quantity == 0:
        # Количество партии не затираем: при возврате на полку оно подставится снова
        new_quantity = item.quantity
        finished_on = _iso(date.today())
    else:
        finished_on = _iso(item.finished_on)
    conn.execute(
        "UPDATE inventory SET quantity = ?, unit = ?, expires_on = ?, finished_on = ? WHERE id = ?",
        (new_quantity, new_unit, _iso(new_expiry), finished_on, item_id),
    )


def mark_finished(conn: sqlite3.Connection, item_id: int, today: date | None = None) -> None:
    get_item(conn, item_id)
    conn.execute(
        "UPDATE inventory SET finished_on = ? WHERE id = ?",
        ((today or date.today()).isoformat(), item_id),
    )


def restore_item(conn: sqlite3.Connection, item_id: int) -> None:
    get_item(conn, item_id)
    conn.execute("UPDATE inventory SET finished_on = NULL WHERE id = ?", (item_id,))


def delete_item(conn: sqlite3.Connection, item_id: int) -> None:
    conn.execute("DELETE FROM inventory WHERE id = ?", (item_id,))


def link_item(conn: sqlite3.Connection, item_id: int, ingredient_id: int) -> None:
    """Связывает продукт с ингредиентом и запоминает название на будущее."""
    item = get_item(conn, item_id)
    with transaction(conn):
        add_alias(conn, item.name, ingredient_id)
        conn.execute(
            "UPDATE inventory SET ingredient_id = ? WHERE id = ?", (ingredient_id, item_id)
        )


def count_recipes_with(conn: sqlite3.Connection, ingredient_id: int | None) -> int:
    """Сколько рецептов может использовать продукт, включая общие названия:
    сливочное масло подходит и там, где написано просто «масло»."""
    if ingredient_id is None:
        return 0
    ids = [ingredient_id, *generic_ingredient_ids(conn, ingredient_id)]
    marks = ",".join("?" * len(ids))
    return conn.execute(
        f"SELECT count(DISTINCT recipe_id) FROM recipe_ingredients WHERE ingredient_id IN ({marks})",
        ids,
    ).fetchone()[0]

"""Список покупок по выбранному плану."""
import sqlite3
from itertools import groupby

import streamlit as st

from core.inventory_manager import InventoryError
from core.plan_manager import current_plan, list_plans
from core.shopping_list import build_shopping_list, move_purchased_to_inventory, set_purchased
from interface.components.shared import flash, format_quantity, open_db, plural, show_flash

show_flash()
st.title("Список покупок")

with open_db() as conn:
    plans = list_plans(conn)
    today_plan = current_plan(conn)

if not plans:
    st.write("Список появится, когда будет план питания.")
    st.page_link("interface/pages/generator.py", label="Открыть генератор", icon=":material/auto_awesome:")
    st.stop()

default = next((index for index, plan in enumerate(plans) if today_plan and plan.id == today_plan.id), 0)
chosen = st.selectbox("План", plans, index=default, format_func=lambda plan: plan.name)

with open_db() as conn:
    lines = build_shopping_list(conn, chosen.id)

if not lines:
    st.write("Докупать ничего не нужно: всё есть дома.")
    st.stop()

bought = sum(line.purchased for line in lines)
total = len(lines)
st.caption(f"Куплено {bought} из {total} {plural(total, 'продукта', 'продуктов', 'продуктов')}")
st.progress(bought / total)


def amount_text(line) -> str:
    if line.quantity is None:
        return "по вкусу"
    if line.unit == "г" and line.quantity >= 1000:
        return f"{format_quantity(line.quantity / 1000)} кг"
    if line.unit == "мл" and line.quantity >= 1000:
        return f"{format_quantity(line.quantity / 1000)} л"
    return f"{format_quantity(line.quantity)} {line.unit}"


for category, group in groupby(lines, key=lambda line: line.category or "Другое"):
    st.subheader(category)
    for line in group:
        checked = st.checkbox(
            f"{line.name} — {amount_text(line)}",
            value=line.purchased,
            key=f"buy_{chosen.id}_{line.ingredient_id}",
        )
        st.caption("Для: " + ", ".join(line.recipes))
        if checked != line.purchased:
            with open_db() as conn:
                set_purchased(conn, chosen.id, line.ingredient_id, checked)
            st.rerun()

if bought and st.button("Перенести купленное в инвентарь", type="primary"):
    try:
        with open_db() as conn:
            moved = move_purchased_to_inventory(conn, chosen.id)
    except (InventoryError, sqlite3.Error) as error:
        st.error(f"Не удалось перенести: {error}")
    else:
        flash(f"В инвентарь добавлено продуктов: {moved}")
        st.rerun()

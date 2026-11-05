"""Главная: что есть дома и что надо использовать в первую очередь."""
from datetime import date

import streamlit as st

from core.inventory_manager import list_expiring, list_items
from core.plan_manager import current_plan, meal_title
from core.recipe_manager import count_recipes
from interface.components.shared import expiry_label, format_quantity, open_db

with open_db() as conn:
    recipes = count_recipes(conn)
    items = list_items(conn)
    expiring = list_expiring(conn)
    plan = current_plan(conn)

st.title("Главная")

first, second, third = st.columns(3)
first.metric("Рецептов в базе", recipes)
second.metric("Продуктов дома", len(items))
third.metric("Скоро испортятся", len(expiring))

if plan:
    st.subheader("Сегодня по плану")
    today_items = plan.items_on(date.today())
    for item in today_items:
        done = " · приготовлено" if item.cooked_on else ""
        st.markdown(f"{meal_title(item.meal)}: **{item.title}**{done}")
    if not today_items:
        st.write("На сегодня в плане нет блюд.")
    st.page_link("interface/pages/plans.py", label="Весь рацион", icon=":material/calendar_month:")
else:
    st.page_link(
        "interface/pages/generator.py", label="Составить план", icon=":material/auto_awesome:"
    )

if not items:
    st.subheader("Начните с продуктов")
    st.write("Добавьте то, что есть дома: планы питания будут строиться вокруг этих запасов.")
    st.page_link("interface/pages/inventory.py", label="Открыть инвентарь", icon=":material/kitchen:")
if expiring:
    st.subheader("Использовать в первую очередь")
    for item in expiring:
        st.markdown(
            f"**{item.name}** · {format_quantity(item.quantity)} {item.unit} · "
            f"{expiry_label(item.expires_on)}"
        )

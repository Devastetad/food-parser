"""Мой рацион: сохранённые планы на день, неделю и месяц."""
from datetime import timedelta

import streamlit as st

from core.plan_manager import delete_plan, list_plans, meal_title
from interface.components.shared import flash, open_db, show_flash

show_flash()
st.title("Мой рацион")

with open_db() as conn:
    plans = list_plans(conn)

if not plans:
    st.write("Планов пока нет. Составьте первый в генераторе.")
    st.page_link("interface/pages/generator.py", label="Открыть генератор", icon=":material/auto_awesome:")
    st.stop()


def open_recipe(recipe_id: int) -> None:
    st.session_state["open_recipe"] = recipe_id


chosen = st.selectbox("План", plans, format_func=lambda plan: plan.name)
for warning in chosen.warnings:
    st.warning(warning)

for day in range(chosen.days):
    items = [item for item in chosen.items if item.day == day]
    if not items:
        continue
    on_date = chosen.start + timedelta(days=day)
    with st.expander(f"{on_date:%d.%m.%Y}", expanded=chosen.days <= 7):
        for item in items:
            meal_column, title_column, status_column = st.columns(
                [1, 4, 1], vertical_alignment="center"
            )
            meal_column.write(meal_title(item.meal))
            if title_column.button(item.title, key=f"plan_item_{item.id}", type="tertiary"):
                open_recipe(item.recipe_id)
                st.switch_page("interface/pages/recipes.py")
            if item.cooked_on:
                status_column.caption("Приготовлено")

if st.button("Удалить этот план"):
    with open_db() as conn:
        delete_plan(conn, chosen.id)
    flash(f"Удалён план: {chosen.name}")
    st.rerun()

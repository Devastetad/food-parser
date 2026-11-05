"""Генератор: параметры плана, предпросмотр, сохранение."""
import sqlite3
from datetime import date, timedelta

import streamlit as st

from core.equipment_manager import list_equipment
from core.history_manager import list_history
from core.inventory_manager import list_items
from core.meal_planner import (
    MEALS,
    MIXED,
    NEW_ONLY,
    Plan,
    PlanRequest,
    PlanRuleViolation,
    build_plan,
)
from core.plan_manager import save_plan
from core.preferences import DIETS, load_preferences
from core.recipe_manager import list_ingredient_names
from interface.components.shared import flash, format_quantity, open_db, plural, show_flash

PERIODS = {"День": 1, "Неделя": 7, "Месяц": 30}
PREVIEW_KEY = "plan_preview"
UNKNOWN_TIME_NOTE = "Рецепты без указанного времени нельзя проверить на лимит"

show_flash()
st.title("Генератор")

with open_db() as conn:
    preferences = load_preferences(conn)
    equipment = list_equipment(conn)
    stock_items = [item for item in list_items(conn) if item.ingredient_id]
    history = list_history(conn)
    ingredient_names = list_ingredient_names(conn)

owned = [item.name for item in equipment if item.is_available]
old_recipes = {entry.recipe_id: entry.title for entry in history}

mode_label = st.radio(
    "Какие блюда использовать",
    ["Смешанный рацион", "Только новые блюда"],
    horizontal=True,
    captions=["Можно повторять блюда из истории", "Ни одного уже приготовленного блюда"],
)
mode = MIXED if mode_label == "Смешанный рацион" else NEW_ONLY

allow_old, pinned = True, []
if mode == MIXED:
    allow_old = st.checkbox("Разрешить подбирать блюда из истории автоматически", value=True)
    if old_recipes:
        pinned = st.multiselect(
            "Обязательно включить из истории",
            list(old_recipes),
            format_func=old_recipes.get,
            placeholder="Выберите блюда",
        )
elif history:
    count = len(old_recipes)
    st.caption(f"Будет исключено {count} {plural(count, 'блюдо', 'блюда', 'блюд')} из истории.")

period_column, start_column, meals_column, people_column = st.columns(4)
period = period_column.selectbox("Период", list(PERIODS), index=1)
start = start_column.date_input("Начало", value=date.today(), format="DD.MM.YYYY")
meals = meals_column.number_input(
    "Приёмов пищи", min_value=1, max_value=5, value=preferences.meals_per_day
)
people = people_column.number_input("Порций на блюдо", min_value=1, max_value=20, value=preferences.people)

time_column, budget_column, kcal_column = st.columns(3)
max_minutes = time_column.number_input(
    "Время на блюдо, минут", min_value=5, value=preferences.max_minutes, step=5, placeholder="Без лимита"
)
budget = budget_column.number_input(
    "Бюджет на покупки, руб.", min_value=0, value=None, step=500, placeholder="Без ограничения"
)
target_kcal = kcal_column.number_input(
    "Калорий в день", min_value=800, max_value=6000, value=preferences.target_kcal, step=100,
    placeholder="Не учитывать",
)  # fmt: skip

with st.expander("Продукты, техника и ограничения"):
    chosen_items = st.multiselect(
        "Продукты, которые можно использовать",
        [item.id for item in stock_items],
        default=[item.id for item in stock_items],
        format_func={
            item.id: f"{item.name}, {format_quantity(item.quantity)} {item.unit}" for item in stock_items
        }.get,
        placeholder="Запасы не использовать",
    )
    chosen_equipment = st.multiselect(
        "Доступная техника", [item.name for item in equipment], default=owned, placeholder="Без техники"
    )
    diets = st.multiselect(
        "Питание", list(DIETS), default=preferences.diets, format_func=DIETS.get, placeholder="Без ограничений"
    )
    excluded = st.multiselect(
        "Исключить продукты",
        ingredient_names,
        default=[name for name in preferences.excluded if name in ingredient_names],
        accept_new_options=True,
        placeholder="Начните вводить название",
    )
    include_unknown = st.checkbox("Брать рецепты без указанного времени", help=UNKNOWN_TIME_NOTE)

if not owned and not chosen_equipment:
    st.warning("Техника не выбрана: подойдут только блюда, где она не нужна. Отметьте её в настройках.")

if st.button("Составить план", type="primary"):
    request = PlanRequest(
        start=start,
        days=PERIODS[period],
        meals_per_day=meals,
        people=people,
        max_minutes=max_minutes,
        include_unknown_time=include_unknown,
        budget=budget,
        diets=diets,
        excluded=excluded,
        equipment=set(chosen_equipment),
        mode=mode,
        allow_old=allow_old,
        pinned=pinned,
        target_kcal=target_kcal,
    )
    try:
        with st.spinner("Подбираю блюда"), open_db() as conn:
            st.session_state[PREVIEW_KEY] = build_plan(conn, request, set(chosen_items))
    except PlanRuleViolation as error:
        st.error(str(error))


def preview(plan: Plan) -> None:
    filled = len(plan.items)
    st.subheader("Предварительный план")
    for warning in plan.warnings:
        st.warning(warning)
    missing = sum(item.missing_lines > 0 for item in plan.items)
    first, second, third = st.columns(3)
    first.metric("Блюд подобрано", f"{filled} из {plan.slots_total}")
    second.metric("Блюд с докупкой", missing)
    third.metric("Покупки, примерно", f"{round(plan.cost)} ₽")
    if not plan.items:
        return
    for day in sorted({item.day for item in plan.items}):
        on_date = plan.request.start + timedelta(days=day)
        st.markdown(f"**{on_date:%d.%m.%Y}**")
        for item in [entry for entry in plan.items if entry.day == day]:
            old = " · из истории" if item.is_old else ""
            st.markdown(f"{MEALS[item.meal]}: {item.title}{old}")
    if st.button("Сохранить план", type="primary"):
        try:
            with open_db() as conn:
                save_plan(conn, plan)
        except (PlanRuleViolation, sqlite3.Error) as error:
            st.error(str(error))
        else:
            del st.session_state[PREVIEW_KEY]
            flash("План сохранён")
            st.switch_page("interface/pages/plans.py")


if PREVIEW_KEY in st.session_state:
    preview(st.session_state[PREVIEW_KEY])

"""Подробная карточка рецепта."""
import sqlite3
from datetime import date

import streamlit as st

from core.equipment_manager import list_equipment
from core.history_manager import RATINGS, HistoryError, cooked_summary, record_cooked
from core.inventory_manager import InventoryError
from core.recipe_manager import Recipe, get_recipe
from core.stock import LineCheck, LineStatus, Stock, find_substitutes, is_optional
from interface.components.shared import flash, format_quantity, open_db, plural

DIFFICULTY = {"easy": "Просто", "medium": "Средне", "hard": "Сложно"}


def minutes_label(recipe: Recipe) -> str:
    if recipe.minutes is None:
        return "не указано"
    hours, minutes = divmod(recipe.minutes, 60)
    parts = []
    if hours:
        parts.append(f"{hours} ч")
    if minutes:
        parts.append(f"{minutes} мин")
    prefix = "около " if recipe.time_is_estimated else ""
    return prefix + " ".join(parts)


def amount_label(check: LineCheck, factor: float) -> str:
    line = check.line
    if line.quantity is None:
        return "по вкусу"
    return f"{format_quantity(line.quantity * factor)} {line.unit}"


def status_label(check: LineCheck) -> str:
    if check.status is LineStatus.ENOUGH:
        return ":green[есть]"
    if is_optional(check.line):
        return ":gray[можно без этого]"
    if check.status is LineStatus.PARTIAL:
        missing = format_quantity(check.missing_quantity)
        return f":orange[не хватает {missing} {check.line.unit}]"
    return ":red[нужно купить]"


def ingredients_section(checks: list[LineCheck], factor: float, stock: Stock) -> None:
    st.subheader("Ингредиенты")
    group = None
    for check in checks:
        if check.line.group != group:
            group = check.line.group
            if group:
                st.markdown(f"**{group}**")
        st.markdown(f"{check.line.name} — {amount_label(check, factor)} · {status_label(check)}")
        hints = [check.line.note] if check.line.note else []
        if check.status is not LineStatus.ENOUGH:
            with open_db() as conn:
                substitutes = find_substitutes(conn, stock, check.line)
            if substitutes:
                hints.append("Можно заменить: " + ", ".join(substitutes))
        if hints:
            st.caption(" · ".join(hints))


def cooked_form(recipe: Recipe, servings: int, stock: Stock) -> None:
    """Список к списанию показывается заранее: пользователь подтверждает фактический расход."""
    with st.expander("Приготовил", icon=":material/check_circle:"):
        deductions = stock.plan_deduction(recipe, servings)
        with st.form(f"cooked_{recipe.id}"):
            date_column, rating_column = st.columns(2)
            cooked_on = date_column.date_input(
                "Дата", value=date.today(), max_value=date.today(), format="DD.MM.YYYY"
            )
            rating = rating_column.selectbox(
                "Оценка", list(RATINGS), index=None, format_func=RATINGS.get, placeholder="Без оценки"
            )
            comment = st.text_input("Комментарий", placeholder="Что изменить в следующий раз")
            amounts = []
            if deductions:
                st.markdown("**Сколько продуктов ушло**")
                for item in deductions:
                    value = st.number_input(
                        f"{item.name}, {item.unit} (дома {format_quantity(item.available)})",
                        min_value=0.0,
                        max_value=float(item.available),
                        value=min(float(item.amount), float(item.available)),
                        key=f"deduct_{recipe.id}_{item.inventory_id}",
                    )
                    amounts.append((item.inventory_id, value))
            else:
                st.caption("Из инвентаря списывать нечего.")
            submitted = st.form_submit_button("Сохранить в историю", type="primary")
        if submitted:
            try:
                with open_db() as conn:
                    record_cooked(conn, recipe.id, cooked_on, servings, rating, comment, amounts)
            except (HistoryError, InventoryError, sqlite3.Error) as error:
                st.error(str(error))
            else:
                flash(f"Записано в историю: {recipe.title}")
                st.rerun()


def show_recipe(recipe_id: int) -> None:
    with open_db() as conn:
        recipe = get_recipe(conn, recipe_id)
        stock = Stock.load(conn)
        owned = {item.name for item in list_equipment(conn) if item.is_available}
        cooked = cooked_summary(conn).get(recipe_id)
    if recipe is None:
        st.error("Рецепт не найден")
        return

    st.title(recipe.title)
    if recipe.description:
        st.write(recipe.description)

    info_column, image_column = st.columns([2, 1])
    with info_column:
        time_column, servings_column, level_column = st.columns(3)
        time_column.markdown(f"Время  \n**{minutes_label(recipe)}**")
        servings = servings_column.number_input(
            "Порций", min_value=1, max_value=500, value=recipe.base_servings, key=f"servings_{recipe.id}"
        )
        level_column.markdown(f"Сложность  \n**{DIFFICULTY.get(recipe.difficulty, 'не указана')}**")
        if recipe.servings is None:
            st.caption(f"Число порций в рецепте не указано, количества считаются как на {recipe.base_servings}.")
        if cooked:
            times = plural(cooked.times, "раз", "раза", "раз")
            rating = f", оценка: {RATINGS[cooked.last_rating]}" if cooked.last_rating else ""
            st.caption(f"Готовили {cooked.times} {times}, последний раз {cooked.last_cooked:%d.%m.%Y}{rating}")
    if recipe.image_url:
        image_column.image(recipe.image_url, width="stretch")

    factor = recipe.factor(servings)
    checks = stock.check_recipe(recipe, factor)
    ingredients_section(checks, factor, stock)

    to_buy = [
        check
        for check in checks
        if check.status is not LineStatus.ENOUGH and not is_optional(check.line)
    ]
    if to_buy:
        count = len(to_buy)
        st.caption(f"Докупить: {count} {plural(count, 'продукт', 'продукта', 'продуктов')}")
    else:
        st.caption("Все продукты есть дома")

    if recipe.equipment:
        st.subheader("Техника")
        for name in recipe.equipment:
            st.markdown(f"{name} · " + (":green[есть]" if name in owned else ":red[нет дома]"))

    st.subheader("Приготовление")
    for step in recipe.steps:
        suffix = f" *({step.minutes} мин)*" if step.minutes else ""
        st.markdown(f"{step.number}. {step.text}{suffix}")

    if recipe.calories:
        prefix = "Примерно на порцию" if recipe.protein_g is not None else "На порцию"
        details = ""
        if recipe.protein_g is not None:
            details = (
                f", белки {format_quantity(recipe.protein_g)} г, "
                f"жиры {format_quantity(recipe.fat_g)} г, углеводы {format_quantity(recipe.carbs_g)} г"
            )
        st.caption(f"{prefix}: {round(recipe.calories)} ккал{details}")

    cooked_form(recipe, servings, stock)
    if recipe.source_url:
        st.caption(f"Источник: [{recipe.source}]({recipe.source_url}), лицензия {recipe.license}")

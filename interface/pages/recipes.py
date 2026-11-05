"""Рецепты: поиск по базе и переход в карточку."""
import streamlit as st

from core.equipment_manager import list_equipment
from core.recipe_manager import list_categories, load_recipes, search_recipes
from core.stock import LineStatus, Stock
from interface.components.recipe_card import minutes_label, show_recipe
from interface.components.shared import open_db, plural, show_flash

PAGE_SIZE = 30
OPEN_KEY = "open_recipe"

show_flash()


def open_recipe(recipe_id: int) -> None:
    st.session_state[OPEN_KEY] = recipe_id


def close_recipe() -> None:
    st.session_state.pop(OPEN_KEY, None)


def missing_count(stock: Stock, recipe) -> int:
    return sum(check.status is not LineStatus.ENOUGH for check in stock.check_recipe(recipe))


def recipe_list() -> None:
    st.title("Рецепты")
    with open_db() as conn:
        recipes = list(load_recipes(conn).values())
        categories = list_categories(conn)
        owned = {item.name for item in list_equipment(conn) if item.is_available}
        stock = Stock.load(conn)

    text_column, category_column, time_column = st.columns([3, 2, 1])
    text = text_column.text_input(
        "Поиск", placeholder="Название или продукт", icon=":material/search:"
    )
    category = category_column.selectbox("Категория", categories, index=None, placeholder="Все")
    max_minutes = time_column.number_input(
        "До минут", min_value=0, value=None, step=10, placeholder="Любое"
    )
    equipment_column, stock_column = st.columns(2)
    only_owned = equipment_column.checkbox("Только с моей техникой")
    from_stock = stock_column.checkbox("Сначала те, где меньше докупать")

    found = search_recipes(recipes, text, category, max_minutes, owned if only_owned else None)
    if from_stock:
        found.sort(key=lambda recipe: missing_count(stock, recipe))
    count = len(found)
    st.caption(f"Найдено {count} {plural(count, 'рецепт', 'рецепта', 'рецептов')}")
    if not found:
        st.write("Ничего не нашлось. Попробуйте убрать часть условий.")
        return

    shown = st.session_state.setdefault("recipes_shown", PAGE_SIZE)
    for recipe in found[:shown]:
        title_column, meta_column = st.columns([3, 2], vertical_alignment="center")
        title_column.button(
            recipe.title,
            key=f"open_{recipe.id}",
            type="tertiary",
            on_click=open_recipe,
            args=(recipe.id,),
        )
        missing = missing_count(stock, recipe)
        buy = f"докупить {missing}" if missing else "всё есть дома"
        meta_column.caption(f"{recipe.category} · {minutes_label(recipe)} · {buy}")
    if count > shown and st.button("Показать ещё"):
        st.session_state["recipes_shown"] = shown + PAGE_SIZE
        st.rerun()


if OPEN_KEY in st.session_state:
    st.button("К списку рецептов", icon=":material/arrow_back:", on_click=close_recipe)
    show_recipe(st.session_state[OPEN_KEY])
else:
    recipe_list()

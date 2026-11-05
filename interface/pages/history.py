"""История: все приготовленные блюда с оценками."""
import streamlit as st

from core.history_manager import RATINGS, delete_entry, list_history, update_entry
from interface.components.shared import flash, open_db, plural, show_flash

show_flash()
st.title("История")

with open_db() as conn:
    entries = list_history(conn)

if not entries:
    st.write("Здесь появятся блюда, которые вы приготовили. Отметьте первое в карточке рецепта.")
    st.page_link("interface/pages/recipes.py", label="Открыть рецепты", icon=":material/menu_book:")
    st.stop()

count = len(entries)
unique = len({entry.recipe_id for entry in entries})
st.caption(
    f"{count} {plural(count, 'запись', 'записи', 'записей')}, "
    f"{unique} {plural(unique, 'разное блюдо', 'разных блюда', 'разных блюд')}"
)

for entry in entries:
    with st.container(border=True):
        title_column, rating_column, delete_column = st.columns(
            [3, 2, 1], vertical_alignment="center"
        )
        servings = f" · порций: {entry.servings}" if entry.servings else ""
        title_column.markdown(f"**{entry.title}**  \n{entry.cooked_on:%d.%m.%Y}{servings}")
        rating = rating_column.selectbox(
            f"Оценка: {entry.title}",
            list(RATINGS),
            index=list(RATINGS).index(entry.rating) if entry.rating else None,
            format_func=RATINGS.get,
            placeholder="Без оценки",
            key=f"rating_{entry.id}",
            label_visibility="collapsed",
        )
        if rating != entry.rating:
            with open_db() as conn:
                update_entry(conn, entry.id, rating, entry.comment)
        if delete_column.button("Удалить", key=f"delete_{entry.id}", width="stretch"):
            with open_db() as conn:
                delete_entry(conn, entry.id)
            flash(f"Запись удалена: {entry.title}")
            st.rerun()
        if entry.comment:
            st.caption(entry.comment)

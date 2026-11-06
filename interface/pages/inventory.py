"""Инвентарь: добавление продуктов, таблица запасов, закончившиеся и нераспознанные."""
import sqlite3
from datetime import date

import pandas as pd
import streamlit as st

from core.ingredient_matcher import suggest_ingredients
from core.inventory_manager import (
    EXPIRING_SOON_DAYS,
    InventoryError,
    add_item,
    add_quick_items,
    count_recipes_with,
    delete_item,
    get_item,
    link_item,
    list_finished,
    list_items,
    list_unmatched,
    mark_finished,
    parse_quantity,
    parse_quick_list,
    restore_item,
    update_item,
)
from core.recipe_manager import list_ingredient_names
from core.units import INVENTORY_UNITS
from database.database import transaction
from interface.components.shared import (
    expiry_label,
    flash,
    format_quantity,
    is_phone,
    open_db,
    show_flash,
)

# (просрочено, скоро испортится): на тёмном фоне нужны более светлые оттенки
EXPIRY_COLORS = {"light": ("#B3261E", "#8A5A00"), "dark": ("#FF8A80", "#E6B450")}

show_flash()
st.title("Инвентарь")


def add_form() -> None:
    with open_db() as conn:
        names = list_ingredient_names(conn)
    with st.form("add_product", clear_on_submit=True, border=True):
        name_column, quantity_column, unit_column, expiry_column = st.columns([3, 1, 1, 1.4])
        name = name_column.selectbox(
            "Продукт",
            names,
            index=None,
            accept_new_options=True,
            placeholder="Начните вводить название",
        )
        quantity = quantity_column.number_input("Количество", min_value=0.0, value=1.0, step=1.0)
        unit = unit_column.selectbox("Единица", INVENTORY_UNITS, index=INVENTORY_UNITS.index("шт"))
        expires_on = expiry_column.date_input("Годен до", value=None, format="DD.MM.YYYY")
        submitted = st.form_submit_button("Добавить", type="primary")
    if submitted:
        try:
            with open_db() as conn:
                add_item(conn, name, quantity, unit, expires_on)
        except (InventoryError, sqlite3.Error) as error:
            st.error(str(error))
        else:
            flash(f"Добавлено: {name}")
            st.rerun()


def quick_add() -> None:
    """Сначала показываем, как понят список: ошибка разбора видна до записи."""
    with st.expander("Добавить списком", icon=":material/playlist_add:"):
        version = st.session_state.setdefault("quick_version", 0)
        text = st.text_area(
            "Продукты через запятую или с новой строки",
            placeholder="молоко 1 л, яйца 10, лук 1 кг",
            key=f"quick_{version}",
        )
        items = parse_quick_list(text)
        for item in items:
            st.markdown(f"{item.name} — {format_quantity(item.quantity)} {item.unit}")
        if items and st.button(f"Добавить продуктов: {len(items)}", type="primary"):
            try:
                with open_db() as conn:
                    count = add_quick_items(conn, items)
            except (InventoryError, sqlite3.Error) as error:
                st.error(f"Ничего не добавлено. {error}")
            else:
                st.session_state["quick_version"] = version + 1
                flash(f"Добавлено продуктов: {count}")
                st.rerun()


def stock_frame(items) -> pd.DataFrame:
    with open_db() as conn:
        recipes = [count_recipes_with(conn, item.ingredient_id) for item in items]
    return pd.DataFrame(
        {
            "id": [item.id for item in items],
            "Продукт": [item.name for item in items],
            "Количество": [format_quantity(item.quantity) for item in items],
            "Единица": [item.unit for item in items],
            "Годен до": pd.to_datetime([item.expires_on for item in items]),
            "Срок": [expiry_label(item.expires_on) for item in items],
            "Рецептов": recipes,
            "Закончился": [False] * len(items),
            "Удалить": [False] * len(items),
        }
    ).set_index("id")


def expiry_style(items):
    """Цвет срока: красный — просрочено, янтарный — скоро испортится."""
    colors = {}
    for item in items:
        days = item.days_left()
        if days is not None and days <= EXPIRING_SOON_DAYS:
            overdue, soon = EXPIRY_COLORS[st.context.theme.type or "light"]
            colors[expiry_label(item.expires_on)] = overdue if days < 0 else soon
    return lambda label: f"color: {colors[label]}" if label in colors else ""


def as_date(value) -> date | None:
    return None if pd.isna(value) else pd.Timestamp(value).date()


def save_changes(original: pd.DataFrame, edited: pd.DataFrame) -> int:
    """Применяет правки одной транзакцией: при ошибке в строке не меняется ничего."""
    changed = 0
    with open_db() as conn, transaction(conn):
        for item_id, row in edited.iterrows():
            before = original.loc[item_id]
            item_id = int(item_id)
            if row["Удалить"]:
                delete_item(conn, item_id)
                changed += 1
                continue
            if row["Закончился"]:
                mark_finished(conn, item_id)
                changed += 1
                continue
            quantity_changed = row["Количество"] != before["Количество"]
            new_unit = None if row["Единица"] == before["Единица"] else row["Единица"]
            old_expiry, new_expiry = as_date(before["Годен до"]), as_date(row["Годен до"])
            if not quantity_changed and new_unit is None and old_expiry == new_expiry:
                continue
            try:
                new_quantity = parse_quantity(row["Количество"]) if quantity_changed else None
                update_item(conn, item_id, new_quantity, new_unit, new_expiry)
            except InventoryError as error:
                raise InventoryError(f"{row['Продукт']}: {error}") from error
            changed += 1
    return changed


def stock_table(items) -> None:
    if not items:
        st.write("Дома пока пусто. Добавьте первый продукт в форме выше.")
        return
    search = st.text_input(
        "Поиск",
        placeholder="Найти продукт",
        label_visibility="collapsed",
        icon=":material/search:",
    )
    if search:
        items = [item for item in items if search.casefold() in item.name.casefold()]
        if not items:
            st.caption("Ничего не нашлось")
            return

    original = stock_frame(items)
    version = st.session_state.setdefault("stock_version", 0)
    edited = st.data_editor(
        original.style.map(expiry_style(items), subset=["Срок"]),
        key=f"stock_{version}",
        hide_index=True,
        width="stretch",
        placeholder="—",
        disabled=["Продукт", "Срок", "Рецептов"],
        column_config={
            "Продукт": st.column_config.TextColumn(width=120),
            "Количество": st.column_config.TextColumn(
                "Кол-во", width=85, required=True, alignment="right"
            ),
            "Единица": st.column_config.SelectboxColumn(
                width=85, options=INVENTORY_UNITS, required=True
            ),
            "Годен до": st.column_config.DateColumn(width=100, format="DD.MM.YYYY"),
            "Срок": st.column_config.TextColumn(width=140),
            "Рецептов": st.column_config.NumberColumn(
                width=70, help="Сколько рецептов из базы используют этот продукт"
            ),
            "Закончился": st.column_config.CheckboxColumn(width=105),
            "Удалить": st.column_config.CheckboxColumn(width=85),
        },
    )
    if not edited.equals(original):
        if st.button("Сохранить изменения", type="primary"):
            try:
                changed = save_changes(original, edited)
            except (InventoryError, sqlite3.Error) as error:
                st.error(f"Изменения не сохранены. {error}")
            else:
                st.session_state["stock_version"] = version + 1
                flash(f"Сохранено продуктов: {changed}")
                st.rerun()


def stock_cards(items) -> None:
    """Вид для телефона: широкая таблица там неудобна, каждый продукт — своя карточка."""
    if not items:
        st.write("Дома пока пусто. Добавьте первый продукт в форме выше.")
        return
    search = st.text_input(
        "Поиск", placeholder="Найти продукт", label_visibility="collapsed", icon=":material/search:"
    )
    for item in items:
        if search and search.casefold() not in item.name.casefold():
            continue
        title = f"{item.name} · {format_quantity(item.quantity)} {item.unit}"
        with st.expander(title):
            label = expiry_label(item.expires_on)
            if label:
                st.caption(label)
            quantity = st.number_input(
                "Количество", min_value=0.0, value=float(item.quantity), key=f"quantity_{item.id}"
            )
            unit = st.selectbox(
                "Единица",
                INVENTORY_UNITS,
                index=INVENTORY_UNITS.index(item.unit),
                key=f"unit_{item.id}",
            )
            expires_on = st.date_input(
                "Годен до", value=item.expires_on, format="DD.MM.YYYY", key=f"expiry_{item.id}"
            )
            if st.button("Сохранить", key=f"save_{item.id}", type="primary", width="stretch"):
                try:
                    with open_db() as conn:
                        update_item(conn, item.id, quantity, unit, expires_on)
                except (InventoryError, sqlite3.Error) as error:
                    st.error(str(error))
                else:
                    flash(f"Сохранено: {item.name}")
                    st.rerun()
            if st.button("Закончился", key=f"finish_{item.id}", width="stretch"):
                with open_db() as conn:
                    mark_finished(conn, item.id)
                flash(f"{item.name} закончился")
                st.rerun()
            if st.button("Удалить", key=f"remove_{item.id}", width="stretch"):
                with open_db() as conn:
                    delete_item(conn, item.id)
                flash(f"Удалено: {item.name}")
                st.rerun()


def finished_list(items) -> None:
    if not items:
        st.caption("Здесь появятся продукты, которые закончились. Их можно вернуть одной кнопкой.")
        return
    for item in items:
        name_column, back_column, delete_column = st.columns([4, 1, 1], vertical_alignment="center")
        name_column.write(f"{item.name} · {format_quantity(item.quantity)} {item.unit}")
        if back_column.button("Вернуть", key=f"back_{item.id}", width="stretch"):
            with open_db() as conn:
                restore_item(conn, item.id)
            flash(f"{item.name} снова дома")
            st.rerun()
        if delete_column.button("Удалить", key=f"delete_{item.id}", width="stretch"):
            with open_db() as conn:
                delete_item(conn, item.id)
            flash(f"Удалено: {item.name}")
            st.rerun()


def unmatched_section(items) -> None:
    """Связка запоминается: следующий раз такой продукт распознается сам."""
    if not items:
        return
    st.subheader("Не найдено в рецептах")
    st.caption("Подскажите, что это за продукт, иначе планы питания его не учтут.")
    for item in items:
        with open_db() as conn:
            options = dict(suggest_ingredients(conn, item.name))
        if not options:
            st.write(f"{item.name}: похожих продуктов нет, такого в рецептах не встречалось")
            continue
        name_column, choice_column, button_column = st.columns([2, 3, 1], vertical_alignment="bottom")
        name_column.write(item.name)
        choice = choice_column.selectbox(
            f"Что такое «{item.name}»",
            list(options),
            format_func=options.get,
            key=f"link_choice_{item.id}",
            label_visibility="collapsed",
        )
        if button_column.button("Связать", key=f"link_{item.id}", width="stretch"):
            with open_db() as conn:
                name = get_item(conn, item.id).name
                link_item(conn, item.id, choice)
            flash(f"{name} теперь значит «{options[choice]}»")
            st.rerun()


if is_phone():
    with st.expander("Добавить продукт", icon=":material/add:"):
        add_form()
else:
    add_form()
quick_add()
with open_db() as conn:
    stock = list_items(conn)
    finished = list_finished(conn)
    unmatched = list_unmatched(conn)

home_tab, finished_tab = st.tabs([f"Дома · {len(stock)}", f"Закончились · {len(finished)}"])
with home_tab:
    if is_phone():
        stock_cards(stock)
    else:
        stock_table(stock)
    unmatched_section(unmatched)
with finished_tab:
    finished_list(finished)

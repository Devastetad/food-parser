"""Настройки: кухонная техника, резервные копии, состояние базы."""
import sqlite3

import streamlit as st

import config
from core.preferences import DIETS, Preferences, load_preferences, save_preferences
from core.equipment_manager import (
    EquipmentError,
    add_equipment,
    delete_equipment,
    list_equipment,
    set_available,
)
from database.database import (
    BackupError,
    SchemaTooNewError,
    create_backup,
    export_database,
    list_backups,
    restore_backup,
    schema_version,
    table_counts,
)
from interface.components.shared import SYNC_KEY, flash, open_db, plural, show_flash

show_flash()
st.title("Настройки")


def equipment_section() -> None:
    st.subheader("Кухонная техника")
    st.caption("Отметьте то, что есть дома: рецепты с другой техникой предлагаться не будут.")
    with open_db() as conn:
        equipment = list_equipment(conn)

    columns = st.columns(2)
    for index, item in enumerate(equipment):
        column = columns[index % 2]
        label = item.name
        if item.recipes:
            label += f" · {item.recipes} {plural(item.recipes, 'рецепт', 'рецепта', 'рецептов')}"
        checked = column.checkbox(label, value=item.is_available, key=f"equipment_{item.id}")
        if checked != item.is_available:
            with open_db() as conn:
                set_available(conn, item.id, checked)

    for item in equipment:
        if item.is_custom and st.button(
            f"Убрать «{item.name}»", key=f"remove_equipment_{item.id}", type="tertiary"
        ):
            try:
                with open_db() as conn:
                    delete_equipment(conn, item.id)
            except EquipmentError as error:
                st.error(str(error))
            else:
                st.session_state.pop(f"equipment_{item.id}", None)
                st.rerun()

    with st.form("add_equipment", clear_on_submit=True, border=False):
        name_column, button_column = st.columns([3, 1], vertical_alignment="bottom")
        name = name_column.text_input("Другой прибор", placeholder="Например, су-вид")
        if button_column.form_submit_button("Добавить", width="stretch"):
            try:
                with open_db() as conn:
                    add_equipment(conn, name)
            except EquipmentError as error:
                st.error(str(error))
            else:
                flash(f"Добавлено: {name.strip()}")
                st.rerun()


def preferences_section() -> None:
    st.subheader("Предпочтения")
    st.caption("Эти значения подставляются в генератор по умолчанию.")
    with open_db() as conn:
        saved = load_preferences(conn)
    with st.form("preferences", border=False):
        people_column, meals_column, time_column, kcal_column = st.columns(4)
        people = people_column.number_input("Порций на блюдо", 1, 20, saved.people)
        meals = meals_column.number_input("Приёмов пищи", 1, 5, saved.meals_per_day)
        minutes = time_column.number_input(
            "Минут на блюдо", min_value=5, value=saved.max_minutes, step=5, placeholder="Без лимита"
        )
        kcal = kcal_column.number_input(
            "Калорий в день", min_value=800, max_value=6000, value=saved.target_kcal, step=100,
            placeholder="Не учитывать",
        )  # fmt: skip
        diets = st.multiselect(
            "Питание", list(DIETS), default=saved.diets, format_func=DIETS.get,
            placeholder="Без ограничений",
        )  # fmt: skip
        excluded = st.text_input(
            "Исключить продукты",
            value=", ".join(saved.excluded),
            placeholder="Через запятую: грибы, кинза",
        )
        if st.form_submit_button("Сохранить предпочтения"):
            names = [name.strip() for name in excluded.split(",") if name.strip()]
            with open_db() as conn:
                save_preferences(conn, Preferences(people, meals, minutes, diets, names, kcal))
            flash("Предпочтения сохранены")
            st.rerun()


def backup_section() -> None:
    st.subheader("Резервные копии")
    backups = list_backups(config.DB_PATH, config.BACKUP_DIR)
    st.caption(
        f"Копии создаются раз в сутки и перед обновлением базы, хранятся последние "
        f"{config.BACKUP_KEEP}. Сейчас их {len(backups)}."
    )
    if st.button("Создать копию сейчас"):
        try:
            path = create_backup(config.DB_PATH, config.BACKUP_DIR)
        except (BackupError, sqlite3.Error, OSError) as error:
            st.error(f"Копия не создана: {error}")
        else:
            flash(f"Копия создана: {path.name}")
            st.rerun()

    file_section()
    if not backups:
        return
    with st.expander("Восстановить из копии"):
        choice = st.selectbox("Копия", backups, format_func=lambda path: path.name)
        confirmed = st.checkbox("Заменить текущие данные этой копией")
        st.caption("Текущее состояние будет сохранено отдельной копией.")
        if st.button("Восстановить", disabled=not confirmed):
            try:
                restore_backup(choice, config.DB_PATH, config.BACKUP_DIR)
            except (BackupError, SchemaTooNewError, sqlite3.Error, OSError) as error:
                st.error(f"Не удалось восстановить: {error}")
            else:
                flash("Данные восстановлены")
                st.rerun()


def file_section() -> None:
    """Файл базы на устройстве: в браузерной версии это единственная защита от очистки браузера."""
    with st.expander("Перенести данные файлом"):
        st.caption("Сохраните файл, чтобы не потерять данные или перенести их на другое устройство.")
        if st.button("Подготовить файл базы"):
            st.session_state["database_file"] = export_database(config.DB_PATH)
        if "database_file" in st.session_state:
            st.download_button(
                "Скачать файл базы",
                st.session_state["database_file"],
                "food_parser.db",
                "application/x-sqlite3",
            )
        uploaded = st.file_uploader("Загрузить файл базы", type=["db"])
        confirmed = st.checkbox("Заменить текущие данные загруженным файлом", disabled=uploaded is None)
        if st.button("Загрузить", disabled=not (uploaded and confirmed)):
            target = config.BACKUP_DIR / "uploaded.db"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(uploaded.getvalue())
            try:
                restore_backup(target, config.DB_PATH, config.BACKUP_DIR)
            except (BackupError, SchemaTooNewError, sqlite3.Error, OSError) as error:
                st.error(f"Не удалось загрузить: {error}")
            else:
                flash("Данные загружены")
                st.rerun()
            finally:
                target.unlink(missing_ok=True)


def database_section() -> None:
    if st.session_state.get(SYNC_KEY):
        st.caption("Данные автоматически сохраняются на GitHub.")
    with st.expander("Состояние базы"):
        st.write(f"Файл: `{config.DB_PATH}`")
        st.write(f"Версия схемы: {schema_version(config.DB_PATH)}")
        st.dataframe(
            [
                {"Таблица": name, "Записей": count}
                for name, count in table_counts(config.DB_PATH).items()
            ],
            hide_index=True,
        )


equipment_section()
preferences_section()
backup_section()
database_section()

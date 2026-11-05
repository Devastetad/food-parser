"""Точка входа Streamlit. Разделы появляются в меню по мере готовности этапов."""
import shutil
import sqlite3

import streamlit as st

import config
from database.database import BackupError, SchemaTooNewError, init_db

st.set_page_config(
    page_title="Personal Food Parser", page_icon=":material/skillet:", layout="wide"
)


@st.cache_resource
def prepare_database() -> None:
    """Один раз на запуск сервера: Streamlit перевыполняет скрипт при каждом действии."""
    if config.SEED_DB.exists() and not config.DB_PATH.exists():
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.SEED_DB, config.DB_PATH)
    init_db(config.DB_PATH, config.BACKUP_DIR)


try:
    prepare_database()
except (sqlite3.Error, OSError, BackupError, SchemaTooNewError) as error:
    st.error(f"Не удалось открыть базу данных: {error}")
    st.stop()

page = st.navigation(
    [
        st.Page("interface/pages/home.py", title="Главная", icon=":material/home:", default=True),
        st.Page("interface/pages/inventory.py", title="Инвентарь", icon=":material/kitchen:"),
        st.Page("interface/pages/recipes.py", title="Рецепты", icon=":material/menu_book:"),
        st.Page("interface/pages/generator.py", title="Генератор", icon=":material/auto_awesome:"),
        st.Page("interface/pages/plans.py", title="Мой рацион", icon=":material/calendar_month:"),
        st.Page("interface/pages/history.py", title="История", icon=":material/history:"),
        st.Page("interface/pages/shopping.py", title="Список покупок", icon=":material/shopping_cart:"),
        st.Page("interface/pages/settings.py", title="Настройки", icon=":material/settings:"),
    ]
)
page.run()

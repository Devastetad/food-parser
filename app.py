"""Точка входа Streamlit."""
import shutil
import sqlite3

import streamlit as st

import config
from core.cloud_sync import CloudSync, SyncError
from database.database import BackupError, SchemaTooNewError, init_db
from interface.components.shared import SYNC_KEY, open_db, read_secret

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


@st.cache_resource
def connect_cloud(token: str, repository: str) -> CloudSync:
    """Кэш привязан к ключу: добавленный позже ключ подхватится без перезапуска."""
    sync = CloudSync(token, repository)
    with open_db() as conn:
        sync.load(conn)
    return sync


try:
    prepare_database()
    token, repository = read_secret("GITHUB_TOKEN"), read_secret("DATA_REPO")
    sync = connect_cloud(token, repository) if token and repository else None
except SyncError as error:
    # Работать дальше нельзя: новые записи затёрли бы сохранённые данные на GitHub
    st.error(f"Сохранённые данные не загрузились. {error}")
    if st.button("Попробовать снова"):
        st.cache_resource.clear()
        st.rerun()
    st.stop()
except (sqlite3.Error, OSError, BackupError, SchemaTooNewError) as error:
    st.error(f"Не удалось открыть базу данных: {error}")
    st.stop()

st.session_state[SYNC_KEY] = sync is not None

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

# st.rerun прерывает скрипт раньше этой строки, но следующий проход доходит сюда и сохраняет
if sync is not None:
    try:
        with open_db() as conn:
            sync.save_if_changed(conn)
    except SyncError as error:
        st.warning(f"Изменения пока не сохранены на GitHub. {error}. Попробую при следующем действии.")

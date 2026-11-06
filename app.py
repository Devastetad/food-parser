"""Точка входа Streamlit."""
import hashlib
import shutil
import sqlite3

import streamlit as st

import config
from core.cloud_sync import CloudSync, SyncError
from database.database import BackupError, SchemaTooNewError, init_db
from database.migrations import MIGRATIONS
from interface.components.shared import SYNC_KEY, open_db, read_secret

st.set_page_config(
    page_title="Personal Food Parser", page_icon=":material/skillet:", layout="wide"
)


def seed_version() -> str:
    if not config.SEED_DB.exists():
        return ""
    return hashlib.sha256(config.SEED_DB.read_bytes()).hexdigest()


@st.cache_resource
def prepare_database(schema_version: int, seed: str, cloud: bool) -> None:
    """Один раз на запуск сервера, версию схемы и версию рецептов.

    Новая база рецептов заменяет старую только при автосохранении: записи пользователя
    лежат на GitHub и вернутся сразу после замены. Без него база не трогается.
    """
    marker = config.DB_PATH.with_name(config.DB_PATH.name + ".seed")
    seen = marker.read_text(encoding="utf-8") if marker.exists() else None
    if seed and (not config.DB_PATH.exists() or (cloud and seen != seed)):
        config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(config.SEED_DB, config.DB_PATH)
    if seed:
        marker.write_text(seed, encoding="utf-8")
    init_db(config.DB_PATH, config.BACKUP_DIR)


@st.cache_resource
def connect_cloud(token: str, repository: str, seed: str) -> CloudSync:
    """Кэш привязан к ключу и рецептам: после обновления базы данные подгрузятся заново."""
    sync = CloudSync(token, repository)
    with open_db() as conn:
        sync.load(conn)
    return sync


try:
    token, repository = read_secret("GITHUB_TOKEN"), read_secret("DATA_REPO")
    has_cloud = bool(token and repository)
    seed = seed_version()
    prepare_database(len(MIGRATIONS), seed, has_cloud)
    sync = connect_cloud(token, repository, seed) if has_cloud else None
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

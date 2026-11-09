"""Точка входа Streamlit."""
import hashlib
import shutil
import sqlite3

import streamlit as st

import config
from core.cloud_sync import CloudSync, SyncError
from database.database import BackupError, SchemaTooNewError, init_db
from database.migrations import MIGRATIONS
from core.accounts import explain_store_error
from core.logging_setup import get_logger, setup_logging
from interface.components.auth import accounts_enabled, current_user, login_page, save_user_data, store
from interface.components.shared import SYNC_KEY, open_db, read_secret

setup_logging()
log = get_logger("app")

ABOUT = (
    "Personal Food Parser: рецепты и план питания из продуктов, которые есть дома. "
    "Учитывает запасы, сроки годности, кухонную технику и предпочтения, собирает список покупок."
)
st.set_page_config(
    page_title="Personal Food Parser: план питания из ваших продуктов",
    page_icon=":material/skillet:",
    layout="wide",
    menu_items={"About": ABOUT},
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


def start_accounts() -> None:
    """Без входа страницы приложения не показываются."""
    try:
        store()
    except Exception as error:
        log.exception("Хранилище аккаунтов недоступно")
        st.error("Сервис входа временно недоступен. Обновите страницу через минуту.")
        st.caption(f"Для владельца сайта: {explain_store_error(error)}")
        st.stop()
    if current_user() is None:
        login_page()
        st.stop()


def start_local() -> CloudSync | None:
    """Без аккаунтов: одна общая база, данные при желании сохраняются на GitHub."""
    try:
        token, repository = read_secret("GITHUB_TOKEN"), read_secret("DATA_REPO")
        has_cloud = bool(token and repository)
        seed = seed_version()
        prepare_database(len(MIGRATIONS), seed, has_cloud)
        return connect_cloud(token, repository, seed) if has_cloud else None
    except SyncError as error:
        # Работать дальше нельзя: новые записи затёрли бы сохранённые данные на GitHub
        log.error("Данные с GitHub не загрузились: %s", error)
        st.error(f"Сохранённые данные не загрузились. {error}")
        if st.button("Попробовать снова"):
            st.cache_resource.clear()
            st.rerun()
        st.stop()
    except (sqlite3.Error, OSError, BackupError, SchemaTooNewError) as error:
        log.error("База не открылась: %s", error)
        st.error(f"Не удалось открыть базу данных: {error}")
        st.stop()


def run_page(page) -> None:
    """Ошибка страницы попадает в журнал, а показывает её Streamlit как обычно."""
    try:
        page.run()
    except Exception as error:
        if type(error).__name__ not in ("RerunException", "StopException"):
            log.exception("Ошибка на странице %s", page.title)
        raise


def save_changes(sync: CloudSync | None) -> None:
    # st.rerun прерывает скрипт раньше, но следующий проход доходит сюда и сохраняет
    if accounts:
        try:
            save_user_data()
        except Exception:
            log.exception("Данные пользователя не сохранены")
            st.warning("Изменения пока не сохранены. Попробую при следующем действии.")
    elif sync is not None:
        try:
            with open_db() as conn:
                sync.save_if_changed(conn)
        except SyncError as error:
            log.warning("Автосохранение на GitHub не удалось: %s", error)
            st.warning(f"Изменения пока не сохранены на GitHub. {error}. Попробую при следующем действии.")


accounts = accounts_enabled()
if accounts:
    start_accounts()
    sync = None
else:
    sync = start_local()
st.session_state[SYNC_KEY] = sync is not None

pages = [
    st.Page("interface/pages/home.py", title="Главная", icon=":material/home:", default=True),
    st.Page("interface/pages/inventory.py", title="Инвентарь", icon=":material/kitchen:"),
    st.Page("interface/pages/recipes.py", title="Рецепты", icon=":material/menu_book:"),
    st.Page("interface/pages/generator.py", title="Генератор", icon=":material/auto_awesome:"),
    st.Page("interface/pages/plans.py", title="Мой рацион", icon=":material/calendar_month:"),
    st.Page("interface/pages/history.py", title="История", icon=":material/history:"),
    st.Page("interface/pages/shopping.py", title="Список покупок", icon=":material/shopping_cart:"),
    st.Page("interface/pages/settings.py", title="Настройки", icon=":material/settings:"),
]
if accounts:
    pages.append(st.Page("interface/pages/account.py", title="Аккаунт", icon=":material/person:"))
run_page(st.navigation(pages))
save_changes(sync)

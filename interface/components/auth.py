"""Вход, регистрация и база пользователя на время сессии.

Аккаунты включаются секретом DATABASE_URL. У каждого пользователя своя копия базы рецептов
на сервере, его записи хранятся в хранилище аккаунтов и подгружаются при входе.
"""
import re
import shutil
import sqlite3
from contextlib import closing

import streamlit as st

import config
from core.accounts import AccountError, AccountStore, User
from core.cloud_sync import CloudSync, SyncError, _hash, restore, snapshot
from core.logging_setup import get_logger
from database.database import connect, init_db
from interface.components.shared import DB_PATH_KEY, read_secret

log = get_logger("auth")
USER_KEY = "account_user"
SAVED_HASH_KEY = "account_saved_hash"
USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def accounts_enabled() -> bool:
    return bool(read_secret("DATABASE_URL"))


@st.cache_resource
def get_store(url: str) -> AccountStore:
    store = AccountStore(url)
    store.init_schema()
    return store


def store() -> AccountStore:
    return get_store(read_secret("DATABASE_URL"))


def current_user() -> User | None:
    return st.session_state.get(USER_KEY)


def _user_db_path(user_id: str):
    if not USER_ID_RE.match(user_id):  # id идёт в путь к файлу, поэтому только шестнадцатеричный
        raise ValueError("Неверный id пользователя")
    return config.USERS_DIR / f"{user_id}.db"


def _import_old_data(conn: sqlite3.Connection, user: User) -> bool:
    """Владелец сайта при первом входе получает записи, сохранённые раньше на GitHub."""
    owner = (read_secret("OWNER_EMAIL") or "").strip().lower()
    token, repository = read_secret("GITHUB_TOKEN"), read_secret("DATA_REPO")
    if not (owner and owner == user.email and token and repository):
        return False
    try:
        return CloudSync(token, repository).load(conn)
    except SyncError as error:
        log.warning("Старые данные с GitHub не перенесены: %s", error)
        return False


def open_user_db(user: User) -> None:
    """Свежая копия рецептов плюс записи пользователя из хранилища."""
    path = _user_db_path(user.id)
    path.parent.mkdir(parents=True, exist_ok=True)
    for old in path.parent.glob(f"{path.name}*"):  # вместе с -wal и -shm прошлой сессии
        old.unlink()
    if config.SEED_DB.exists():
        shutil.copy(config.SEED_DB, path)
    init_db(path, config.USERS_DIR / "backups" / user.id)
    data = store().load_data(user.id)
    with closing(connect(path)) as conn:
        if data is not None:
            restore(conn, data)
            saved = _hash(snapshot(conn))
        else:
            saved = None if _import_old_data(conn, user) else _hash(snapshot(conn))
    st.session_state[DB_PATH_KEY] = path
    st.session_state[SAVED_HASH_KEY] = saved


def save_user_data() -> None:
    """После каждого действия: записи уходят в хранилище, если изменились."""
    user = current_user()
    if user is None:
        return
    with closing(connect(st.session_state[DB_PATH_KEY])) as conn:
        data = snapshot(conn)
    current = _hash(data)
    if current == st.session_state.get(SAVED_HASH_KEY):
        return
    store().save_data(user.id, data, current)
    st.session_state[SAVED_HASH_KEY] = current


def sign_in(user: User) -> None:
    open_user_db(user)
    st.session_state[USER_KEY] = user


def sign_out() -> None:
    user = current_user()
    if user is not None:
        store().audit(user.id, "logout")
    for key in (USER_KEY, DB_PATH_KEY, SAVED_HASH_KEY):
        st.session_state.pop(key, None)


def login_page() -> None:
    st.title("Personal Food Parser")
    st.write("Рецепты и план питания из продуктов, которые есть дома. Войдите, чтобы продолжить.")
    sign_in_tab, sign_up_tab = st.tabs(["Вход", "Регистрация"])
    with sign_in_tab, st.form("sign_in"):
        email = st.text_input("Почта", autocomplete="email")
        password = st.text_input("Пароль", type="password", autocomplete="current-password")
        if st.form_submit_button("Войти", type="primary"):
            try:
                sign_in(store().authenticate(email, password))
            except AccountError as error:
                st.error(str(error))
            else:
                st.rerun()
    with sign_up_tab, st.form("sign_up"):
        email = st.text_input("Почта", key="new_email", autocomplete="email")
        password = st.text_input(
            "Пароль", type="password", key="new_password", autocomplete="new-password",
            help="Не меньше 8 символов",
        )  # fmt: skip
        repeat = st.text_input("Пароль ещё раз", type="password", key="new_repeat", autocomplete="new-password")
        if st.form_submit_button("Зарегистрироваться", type="primary"):
            if password != repeat:
                st.error("Пароли не совпадают")
            else:
                try:
                    sign_in(store().register(email, password))
                except AccountError as error:
                    st.error(str(error))
                else:
                    st.rerun()

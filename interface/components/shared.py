"""Общие части страниц: доступ к базе, форматирование, сообщения после перезапуска."""
import re
from contextlib import closing
from datetime import date

import streamlit as st
from streamlit.errors import StreamlitSecretNotFoundError

import config
from database.database import connect

FLASH_KEY = "flash_message"
SYNC_KEY = "cloud_sync_enabled"
DB_PATH_KEY = "user_db_path"
PHONE_RE = re.compile(r"iPhone|iPod|Android.*Mobile", re.I)


def read_secret(name: str) -> str | None:
    """Значение из секретов Streamlit. На компьютере их нет, и это нормально."""
    try:
        return st.secrets.get(name)
    except StreamlitSecretNotFoundError:
        return None


def is_phone() -> bool:
    return bool(PHONE_RE.search(st.context.headers.get("User-Agent", "")))


def current_db_path():
    """База вошедшего пользователя, а без аккаунтов — общая база приложения."""
    return st.session_state.get(DB_PATH_KEY) or config.DB_PATH


def open_db():
    """Соединение на время блока. Путь читается при вызове, а не при импорте."""
    return closing(connect(current_db_path()))


def flash(message: str) -> None:
    """Сообщение, которое переживёт st.rerun и покажется один раз."""
    st.session_state[FLASH_KEY] = message


def show_flash() -> None:
    message = st.session_state.pop(FLASH_KEY, None)
    if message:
        st.toast(message)


def format_quantity(value: float) -> str:
    return f"{value:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def plural(number: int, one: str, few: str, many: str) -> str:
    """plural(2, 'день', 'дня', 'дней') -> 'дня'."""
    remainder = abs(number) % 100
    if 11 <= remainder <= 14:
        return many
    remainder %= 10
    if remainder == 1:
        return one
    return few if 2 <= remainder <= 4 else many


def expiry_label(expires_on: date | None, today: date | None = None) -> str:
    if expires_on is None:
        return ""
    days = (expires_on - (today or date.today())).days
    if days < 0:
        return f"Просрочено на {-days} {plural(-days, 'день', 'дня', 'дней')}"
    if days == 0:
        return "Сегодня последний день"
    if days == 1:
        return "Остался 1 день"
    return f"{plural(days, 'Остался', 'Осталось', 'Осталось')} {days} {plural(days, 'день', 'дня', 'дней')}"

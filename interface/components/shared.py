"""Общие части страниц: доступ к базе, форматирование, сообщения после перезапуска."""
from contextlib import closing
from datetime import date

import streamlit as st

import config
from database.database import connect

FLASH_KEY = "flash_message"


def open_db():
    """Соединение на время блока. Путь читается при вызове, а не при импорте."""
    return closing(connect(config.DB_PATH))


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

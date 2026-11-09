"""Журнал работы: в консоль (её показывает Streamlit Cloud) и в файл, если диск доступен.

Пароли и данные пользователя в журнал не пишутся, почта только замаскированной.
"""
import logging
import logging.handlers
from pathlib import Path

import config

LOGGER_NAME = "food_parser"
FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(log_dir: Path | None = None, level: int = logging.INFO) -> logging.Logger:
    """Повторный вызов ничего не меняет: Streamlit выполняет скрипт на каждое действие."""
    logger = logging.getLogger(LOGGER_NAME)
    if logger.handlers:
        return logger
    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter(FORMAT)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    try:
        directory = log_dir or config.LOG_DIR
        directory.mkdir(parents=True, exist_ok=True)
        file = logging.handlers.RotatingFileHandler(
            directory / "app.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
    except OSError as error:
        logger.warning("Журнал в файл недоступен: %s", error)
    else:
        file.setFormatter(formatter)
        logger.addHandler(file)
    return logger


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(f"{LOGGER_NAME}.{name}")


def mask_email(email: str) -> str:
    """ivan@mail.ru -> i***@mail.ru: по журналу видно, чей вход, но адрес не раскрыт."""
    name, at, domain = email.partition("@")
    return f"{name[:1]}***{at}{domain}" if at else "***"

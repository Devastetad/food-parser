"""Пути и настройки приложения."""
import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
# В браузерной версии данные лежат в хранилище браузера, путь задаёт docs/index.html
DATA_DIR = Path(os.environ.get("FOOD_PARSER_DATA_DIR") or BASE_DIR / "data")
SEED_DB = BASE_DIR / "seed.db"  # начальная база с рецептами, есть только в браузерной версии
JOURNAL_MODE = os.environ.get("FOOD_PARSER_JOURNAL", "WAL")  # WAL недоступен в браузере
DB_PATH = DATA_DIR / "food_parser.db"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
USERS_DIR = DATA_DIR / "users"  # базы пользователей на сервере, источник данных — хранилище аккаунтов

BACKUP_KEEP = int(os.environ.get("FOOD_PARSER_BACKUP_KEEP", 20))  # сколько копий хранить
BACKUP_MAX_AGE = timedelta(days=1)  # как часто делать автоматическую копию

SOURCES_DIR = DATA_DIR / "sources"  # скачанные файлы источников рецептов

# Без адреса почты: приложение локальное и ничего не публикует
USER_AGENT = "PersonalFoodParser/0.1 (personal non-commercial local app)"
REQUEST_MIN_INTERVAL = 1.0  # секунд между запросами к одному сайту
REQUEST_MAX_ATTEMPTS = 5

DEFAULT_SERVINGS = 4  # если в рецепте не указано число порций

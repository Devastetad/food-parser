"""База рецептов в Postgres (Supabase): общая для всех, без файла в репозитории.

Таблицы base_* повторяют таблицы рецептов SQLite. Сервер при запуске скачивает их в свой
файл SQLite, и приложение дальше работает как раньше. Новую базу заливает
scripts/upload_recipe_base.py одной транзакцией: читатели видят либо старую, либо новую.
"""
import hashlib
import sqlite3
from contextlib import closing
from pathlib import Path

from core.accounts import AccountStore
from core.logging_setup import get_logger
from database.database import connect, init_db

log = get_logger("recipe_base")

# Порядок важен: справочники раньше рецептов, рецепты раньше их строк
TABLES = [
    "ingredients", "ingredient_aliases", "kitchen_equipment", "recipes",
    "recipe_ingredients", "cooking_instructions", "recipe_equipment",
]  # fmt: skip
PG_TYPES = {"INTEGER": "BIGINT", "REAL": "DOUBLE PRECISION", "TEXT": "TEXT"}
BATCH = 1000  # строк в одном INSERT: меньше обращений к серверу
PAGE = 20000  # строк за один запрос при скачивании: память сервера ограничена


def columns_of(conn: sqlite3.Connection, table: str) -> list[tuple[str, str]]:
    return [(row[1], row[2].upper() or "TEXT") for row in conn.execute(f"PRAGMA table_info({table})")]


def _key_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = sorted((row[5], row[1]) for row in conn.execute(f"PRAGMA table_info({table})") if row[5])
    return [name for _, name in rows]


def base_version(conn: sqlite3.Connection) -> str:
    """Хеш содержимого: одинаковая база даёт одинаковую версию."""
    digest = hashlib.sha256()
    for table in TABLES:
        order = ", ".join(_key_columns(conn, table))
        for row in conn.execute(f"SELECT * FROM {table} ORDER BY {order}"):
            digest.update(repr(tuple(row)).encode("utf-8"))
    return digest.hexdigest()


def upload(store: AccountStore, source: Path) -> tuple[str, dict[str, int]]:
    """Заменяет базу рецептов в Postgres содержимым файла SQLite."""
    counts = {}
    with closing(sqlite3.connect(source)) as local:
        version = base_version(local)
        statements = ["CREATE TABLE IF NOT EXISTS base_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"]
        for table in TABLES:
            columns = columns_of(local, table)
            keys = _key_columns(local, table)
            body = ", ".join(f"{name} {PG_TYPES.get(kind, 'TEXT')}" for name, kind in columns)
            statements.append(f"CREATE TABLE IF NOT EXISTS base_{table} ({body}, PRIMARY KEY ({', '.join(keys)}))")
        for statement in statements:
            store.execute(statement)
        if store.is_postgres:  # публичный API Supabase не должен менять рецепты
            for table in ["meta", *TABLES]:
                store.execute(f"ALTER TABLE base_{table} ENABLE ROW LEVEL SECURITY")

        def fill(cursor) -> None:
            for table in reversed(TABLES):
                cursor.execute(f"DELETE FROM base_{table}")
            for table in TABLES:
                names = [name for name, _ in columns_of(local, table)]
                rows = local.execute(f"SELECT {', '.join(names)} FROM {table}").fetchall()
                counts[table] = len(rows)
                for start in range(0, len(rows), BATCH):
                    chunk = rows[start : start + BATCH]
                    marks = ", ".join("(" + ", ".join(["?"] * len(names)) + ")" for _ in chunk)
                    values = [value for row in chunk for value in row]
                    cursor.execute(f"INSERT INTO base_{table} ({', '.join(names)}) VALUES {marks}", values)
            cursor.execute(
                "INSERT INTO base_meta (key, value) VALUES ('version', ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (version,),
            )

        store.transaction(fill)
    log.info("База рецептов загружена в хранилище: версия %s, %s", version[:12], counts)
    return version, counts


def remote_version(store: AccountStore) -> str | None:
    """None: базы в хранилище ещё нет, приложение берёт рецепты из файла seed.db."""
    try:
        row = store.query_one("SELECT value FROM base_meta WHERE key = 'version'")
    except Exception as error:
        if "base_meta" in str(error) or "does not exist" in str(error) or "no such table" in str(error):
            return None
        raise
    return row[0] if row else None


def download(store: AccountStore, target: Path, version: str) -> Path:
    """Собирает файл SQLite с рецептами из Postgres. Готовый файл появляется целиком или никак."""
    partial = target.with_name(target.name + ".part")
    partial.unlink(missing_ok=True)
    target.parent.mkdir(parents=True, exist_ok=True)
    init_db(partial, target.parent / "base_backups")
    with closing(connect(partial)) as local:
        local.execute("PRAGMA foreign_keys = OFF")
        local.execute("BEGIN")  # одна транзакция вместо записи на диск после каждой строки
        local.execute("DELETE FROM kitchen_equipment")  # начальные записи миграций заменяются базой
        for table in TABLES:
            names = [name for name, _ in columns_of(local, table)]
            keys = _key_columns(local, table)
            remote_names = set(store.column_names(f"SELECT * FROM base_{table} LIMIT 0"))
            names = [name for name in names if name in remote_names]
            offset = 0
            while True:
                rows = store.query_all(
                    f"SELECT {', '.join(names)} FROM base_{table} ORDER BY {', '.join(keys)} "
                    f"LIMIT {PAGE} OFFSET {offset}"
                )
                local.executemany(
                    f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})", rows
                )
                offset += len(rows)
                if len(rows) < PAGE:
                    break
        local.execute("COMMIT")
        local.execute("PRAGMA foreign_keys = ON")
        problems = local.execute("PRAGMA foreign_key_check").fetchall()
        if problems:
            raise ValueError(f"База рецептов из хранилища неполная: {len(problems)} битых ссылок")
        if base_version(local) != version:
            raise ValueError("База рецептов изменилась во время скачивания, попробую позже")
        local.execute("PRAGMA journal_mode = DELETE")  # один файл без -wal, его можно копировать
    partial.replace(target)
    log.info("База рецептов скачана: версия %s", version[:12])
    return target

"""Подключение к SQLite, транзакции, инициализация и резервные копии."""
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

import config
from database.migrations import MIGRATIONS, apply_migrations, get_version


class BackupError(Exception):
    """Резервная копия отсутствует или повреждена."""


class SchemaTooNewError(Exception):
    """База создана более новой версией приложения."""


def connect(db_path: Path = config.DB_PATH) -> sqlite3.Connection:
    """Открывает базу. Запись вне transaction() фиксируется сразу."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA journal_mode = {config.JOURNAL_MODE}")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Всё внутри блока применяется целиком либо, при исключении, не применяется."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def init_db(
    db_path: Path = config.DB_PATH,
    backup_dir: Path = config.BACKUP_DIR,
    migrations: list[str] = MIGRATIONS,
) -> None:
    """Готовит базу к работе. Перед миграцией существующей базы делает копию."""
    existed = db_path.exists()
    with closing(connect(db_path)) as conn:
        version = get_version(conn)
        if version > len(migrations):
            raise SchemaTooNewError(
                f"Версия базы {version}, приложение знает только {len(migrations)}. "
                "Обновите приложение."
            )
        if 0 < version < len(migrations):
            create_backup(db_path, backup_dir)
        apply_migrations(conn, migrations)
    if existed:
        backup_if_stale(db_path, backup_dir)


def export_database(db_path: Path = config.DB_PATH) -> bytes:
    """Вся база одним файлом в памяти: для скачивания на устройство."""
    with closing(connect(db_path)) as conn:
        return conn.serialize()


def table_counts(db_path: Path = config.DB_PATH) -> dict[str, int]:
    """Количество строк в каждой таблице."""
    with closing(connect(db_path)) as conn:
        names = [
            row["name"]
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        return {
            name: conn.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in names
        }


def schema_version(db_path: Path = config.DB_PATH) -> int:
    with closing(connect(db_path)) as conn:
        return get_version(conn)


def list_backups(
    db_path: Path = config.DB_PATH, backup_dir: Path = config.BACKUP_DIR
) -> list[Path]:
    """Копии этой базы, новые первыми. Метка времени в имени сортируется как текст."""
    return sorted(backup_dir.glob(f"{db_path.stem}_*.db"), reverse=True)


def create_backup(
    db_path: Path = config.DB_PATH,
    backup_dir: Path = config.BACKUP_DIR,
    keep: int | None = config.BACKUP_KEEP,
) -> Path:
    """Делает проверенную копию базы. keep=None оставляет старые копии на месте."""
    if not db_path.exists():
        raise BackupError(f"Нет базы для копирования: {db_path}")
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"{db_path.stem}_{datetime.now():%Y%m%d_%H%M%S_%f}.db"

    with closing(connect(db_path)) as source, closing(sqlite3.connect(target)) as copy:
        source.backup(copy)
    try:
        _check_integrity(target)
    except BackupError:
        target.unlink()
        raise

    if keep is not None:
        for old in list_backups(db_path, backup_dir)[keep:]:
            old.unlink()
    return target


def backup_if_stale(
    db_path: Path = config.DB_PATH, backup_dir: Path = config.BACKUP_DIR
) -> Path | None:
    """Делает копию, если последняя старше config.BACKUP_MAX_AGE."""
    backups = list_backups(db_path, backup_dir)
    if backups:
        newest = datetime.fromtimestamp(backups[0].stat().st_mtime)
        if datetime.now() - newest < config.BACKUP_MAX_AGE:
            return None
    return create_backup(db_path, backup_dir)


def restore_backup(
    backup_path: Path,
    db_path: Path = config.DB_PATH,
    backup_dir: Path = config.BACKUP_DIR,
    migrations: list[str] = MIGRATIONS,
) -> None:
    """Заменяет данные базы копией, сохранив текущее состояние отдельным файлом."""
    _check_integrity(backup_path)
    with closing(sqlite3.connect(backup_path)) as source:
        backup_version = get_version(source)
    if backup_version > len(migrations):
        raise SchemaTooNewError(
            f"Версия копии {backup_version}, приложение знает только {len(migrations)}."
        )
    if db_path.exists():
        # Без ротации: она могла бы удалить ту самую копию, которую восстанавливаем.
        create_backup(db_path, backup_dir, keep=None)
    with closing(sqlite3.connect(backup_path)) as source, closing(connect(db_path)) as target:
        source.backup(target)
    init_db(db_path, backup_dir, migrations)


def _check_integrity(path: Path) -> None:
    if not path.is_file():
        raise BackupError(f"Файл не найден: {path}")
    try:
        with closing(sqlite3.connect(path)) as conn:
            result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as error:
        raise BackupError(f"Файл повреждён: {path}: {error}") from error
    if result != "ok":
        raise BackupError(f"Файл повреждён: {path}: {result}")

import os
import sqlite3
import time
from contextlib import closing

import pytest

import config
from database.database import (
    BackupError,
    SchemaTooNewError,
    backup_if_stale,
    connect,
    create_backup,
    init_db,
    list_backups,
    restore_backup,
    table_counts,
    transaction,
)
from database.migrations import MIGRATIONS, apply_migrations, get_version

SPEC_TABLES = {
    "recipes",
    "ingredients",
    "recipe_ingredients",
    "cooking_instructions",
    "cooking_history",
    "inventory",
    "kitchen_equipment",
}


def add_recipe(conn, title="Борщ", source_url=None):
    return conn.execute(
        "INSERT INTO recipes (title, source_url) VALUES (?, ?)", (title, source_url)
    ).lastrowid


def recipe_titles(path):
    with closing(connect(path)) as conn:
        return [row["title"] for row in conn.execute("SELECT title FROM recipes ORDER BY id")]


# --- Инициализация и миграции ---


def test_init_creates_all_spec_tables(db_path):
    assert SPEC_TABLES <= set(table_counts(db_path))


def test_init_sets_latest_version_and_enables_foreign_keys(conn):
    assert get_version(conn) == len(MIGRATIONS)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1


def test_equipment_is_seeded_and_extensible(conn):
    names = {row["name"] for row in conn.execute("SELECT name FROM kitchen_equipment")}
    assert {"Плита", "Духовка", "Микроволновка", "Мультиварка"} <= names
    assert {"Аэрогриль", "Блендер", "Миксер", "Гриль"} <= names
    conn.execute("INSERT INTO kitchen_equipment (name) VALUES ('Су-вид')")


def test_repeated_init_keeps_data_and_makes_no_extra_rows(db_path, backup_dir):
    before = table_counts(db_path)
    init_db(db_path, backup_dir)
    assert table_counts(db_path) == before


def test_failed_migration_rolls_back_completely(db_path):
    broken = "CREATE TABLE half_done (id INTEGER); INSERT INTO no_such_table VALUES (1);"
    with closing(connect(db_path)) as conn:
        with pytest.raises(sqlite3.OperationalError):
            apply_migrations(conn, [*MIGRATIONS, broken])
        assert get_version(conn) == len(MIGRATIONS)
        assert not conn.in_transaction
    assert "half_done" not in table_counts(db_path)


def test_migration_backs_up_existing_database_first(db_path, backup_dir):
    with closing(connect(db_path)) as conn:
        add_recipe(conn)
    extended = [*MIGRATIONS, "CREATE TABLE extra (id INTEGER);"]

    init_db(db_path, backup_dir, extended)

    assert "extra" in table_counts(db_path)
    backups = list_backups(db_path, backup_dir)
    assert len(backups) == 1
    assert "extra" not in table_counts(backups[0])
    assert recipe_titles(backups[0]) == ["Борщ"]


def test_database_from_newer_app_is_refused(db_path, backup_dir):
    with closing(connect(db_path)) as conn:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")
    with pytest.raises(SchemaTooNewError):
        init_db(db_path, backup_dir)


# --- Транзакции ---


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(ValueError):
        with transaction(conn):
            add_recipe(conn)
            raise ValueError
    assert conn.execute("SELECT count(*) FROM recipes").fetchone()[0] == 0


def test_transaction_commits_on_success(conn):
    with transaction(conn):
        add_recipe(conn)
    assert conn.execute("SELECT count(*) FROM recipes").fetchone()[0] == 1


# --- Ограничения схемы ---


def test_recipe_with_history_cannot_be_deleted(conn):
    recipe_id = add_recipe(conn)
    conn.execute(
        "INSERT INTO cooking_history (recipe_id, cooked_on) VALUES (?, '2026-11-05')",
        (recipe_id,),
    )
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))


def test_recipe_is_not_in_history_until_cooked(conn):
    add_recipe(conn)
    assert conn.execute("SELECT count(*) FROM cooking_history").fetchone()[0] == 0


def test_deleting_recipe_removes_its_steps_and_ingredient_links(conn):
    recipe_id = add_recipe(conn)
    ingredient_id = conn.execute(
        "INSERT INTO ingredients (name, normalized_name) VALUES ('Свёкла', 'свёкла')"
    ).lastrowid
    conn.execute(
        "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) "
        "VALUES (?, ?, 300, 'г')",
        (recipe_id, ingredient_id),
    )
    conn.execute(
        "INSERT INTO cooking_instructions (recipe_id, step_number, description) "
        "VALUES (?, 1, 'Сварить бульон')",
        (recipe_id,),
    )

    conn.execute("DELETE FROM recipes WHERE id = ?", (recipe_id,))

    assert conn.execute("SELECT count(*) FROM recipe_ingredients").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM cooking_instructions").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM ingredients").fetchone()[0] == 1


def test_same_source_url_is_rejected_but_manual_recipes_are_not(conn):
    add_recipe(conn, source_url="https://example.org/1")
    with pytest.raises(sqlite3.IntegrityError):
        add_recipe(conn, source_url="https://example.org/1")
    add_recipe(conn, "Домашний 1")
    add_recipe(conn, "Домашний 2")


def test_one_alias_cannot_point_to_two_ingredients(conn):
    first = conn.execute(
        "INSERT INTO ingredients (name, normalized_name) VALUES ('Курица', 'курица')"
    ).lastrowid
    second = conn.execute(
        "INSERT INTO ingredients (name, normalized_name) VALUES ('Индейка', 'индейка')"
    ).lastrowid
    sql = (
        "INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) "
        "VALUES (?, 'Филе', 'филе')"
    )
    conn.execute(sql, (first,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql, (second,))


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO recipes (title) VALUES ('   ')",
        "INSERT INTO recipes (title, servings) VALUES ('Суп', 0)",
        "INSERT INTO recipes (title, cook_time_min) VALUES ('Суп', -5)",
        "INSERT INTO recipes (title, difficulty) VALUES ('Суп', 'легко')",
        "INSERT INTO inventory (name, quantity, unit) VALUES ('Рис', -1, 'г')",
        "INSERT INTO inventory (name, quantity, unit, expires_on) "
        "VALUES ('Рис', 1, 'г', 'завтра')",
    ],
)
def test_invalid_rows_are_rejected(conn, sql):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(sql)


@pytest.mark.parametrize(
    "rating, cooked_on",
    [("супер", "2026-11-05"), ("liked", "вчера"), ("liked", "2026-13-40")],
)
def test_invalid_history_is_rejected(conn, rating, cooked_on):
    recipe_id = add_recipe(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO cooking_history (recipe_id, cooked_on, rating) VALUES (?, ?, ?)",
            (recipe_id, cooked_on, rating),
        )


@pytest.mark.parametrize("rating", ["liked", "ok", "disliked", "tired", None])
def test_all_spec_ratings_are_accepted(conn, rating):
    recipe_id = add_recipe(conn)
    conn.execute(
        "INSERT INTO cooking_history (recipe_id, cooked_on, rating) "
        "VALUES (?, '2026-11-05', ?)",
        (recipe_id, rating),
    )


def test_inventory_keeps_batches_with_different_expiry(conn):
    sql = "INSERT INTO inventory (name, quantity, unit, expires_on) VALUES ('Молоко', 1, 'л', ?)"
    conn.execute(sql, ("2026-11-10",))
    conn.execute(sql, ("2026-11-20",))
    row = conn.execute("SELECT count(*), min(added_on) FROM inventory").fetchone()
    assert row[0] == 2
    assert row[1] is not None


# --- Резервные копии ---


def test_backup_contains_data_committed_to_wal(db_path, backup_dir):
    with closing(connect(db_path)) as conn, closing(connect(db_path)) as still_open:
        add_recipe(conn)
        still_open.execute("SELECT 1")
        backup = create_backup(db_path, backup_dir)
    assert recipe_titles(backup) == ["Борщ"]


def test_backup_rotation_keeps_newest(db_path, backup_dir):
    created = [create_backup(db_path, backup_dir, keep=2) for _ in range(4)]
    assert list_backups(db_path, backup_dir) == [created[3], created[2]]


def test_backup_of_missing_database_fails(tmp_path):
    with pytest.raises(BackupError):
        create_backup(tmp_path / "missing.db", tmp_path / "backups")


def test_restore_returns_old_data_and_saves_current_state(db_path, backup_dir):
    with closing(connect(db_path)) as conn:
        add_recipe(conn, "Старый")
    backup = create_backup(db_path, backup_dir)
    with closing(connect(db_path)) as conn:
        add_recipe(conn, "Новый")

    restore_backup(backup, db_path, backup_dir)

    assert recipe_titles(db_path) == ["Старый"]
    safety = list_backups(db_path, backup_dir)[0]
    assert recipe_titles(safety) == ["Старый", "Новый"]


def test_restore_works_for_oldest_backup_at_rotation_limit(db_path, backup_dir):
    with closing(connect(db_path)) as conn:
        add_recipe(conn, "Старый")
    oldest = create_backup(db_path, backup_dir)
    with closing(connect(db_path)) as conn:
        add_recipe(conn, "Новый")
    for _ in range(config.BACKUP_KEEP - 1):
        create_backup(db_path, backup_dir)
    assert oldest in list_backups(db_path, backup_dir)

    restore_backup(oldest, db_path, backup_dir)

    assert recipe_titles(db_path) == ["Старый"]


def test_corrupt_backup_is_refused_and_database_is_untouched(db_path, backup_dir, tmp_path):
    with closing(connect(db_path)) as conn:
        add_recipe(conn)
    corrupt = tmp_path / "corrupt.db"
    corrupt.write_bytes(b"this is not a database" * 100)

    with pytest.raises(BackupError):
        restore_backup(corrupt, db_path, backup_dir)

    assert recipe_titles(db_path) == ["Борщ"]


def test_restore_migrates_backup_from_older_schema(db_path, backup_dir):
    with closing(connect(db_path)) as conn:
        add_recipe(conn, "Старый")
    backup = create_backup(db_path, backup_dir)
    extended = [*MIGRATIONS, "CREATE TABLE extra (id INTEGER);"]
    init_db(db_path, backup_dir, extended)

    restore_backup(backup, db_path, backup_dir, extended)

    assert "extra" in table_counts(db_path)
    assert recipe_titles(db_path) == ["Старый"]


def test_backup_from_newer_app_is_refused_before_touching_database(
    db_path, backup_dir, tmp_path
):
    with closing(connect(db_path)) as conn:
        add_recipe(conn, "Текущий")
    newer = tmp_path / "newer.db"
    with closing(connect(newer)) as conn:
        conn.execute(f"PRAGMA user_version = {len(MIGRATIONS) + 1}")

    with pytest.raises(SchemaTooNewError):
        restore_backup(newer, db_path, backup_dir)

    assert recipe_titles(db_path) == ["Текущий"]


def test_stale_backup_policy(db_path, backup_dir):
    first = backup_if_stale(db_path, backup_dir)
    assert first is not None
    assert backup_if_stale(db_path, backup_dir) is None

    two_days_ago = time.time() - 2 * 24 * 3600
    os.utime(first, (two_days_ago, two_days_ago))
    assert backup_if_stale(db_path, backup_dir) is not None

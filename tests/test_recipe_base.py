import sqlite3
from contextlib import closing

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
from core import recipe_base
from core.accounts import AccountStore
from database.database import connect, init_db

APP = str(config.BASE_DIR / "app.py")


@pytest.fixture
def base_file(tmp_path):
    path = tmp_path / "base.db"
    init_db(path, tmp_path / "base_backups")
    with closing(connect(path)) as conn:
        conn.execute("INSERT INTO ingredients (id, name, normalized_name) VALUES (1, 'Мука', 'мука')")
        conn.execute("INSERT INTO ingredient_aliases (ingredient_id, alias, normalized_alias) VALUES (1, 'мучка', 'мучка')")
        for number in range(1, 2501):  # больше одной пачки вставки
            conn.execute(
                "INSERT INTO recipes (id, title, source_url, calories) VALUES (?, ?, ?, ?)",
                (number, f"Блин {number}", f"u{number}", 100.5),
            )
            conn.execute(
                "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit, raw_text) "
                "VALUES (?, 1, 200, 'г', '200 г муки')",
                (number,),
            )
            conn.execute(
                "INSERT INTO cooking_instructions (recipe_id, step_number, description) VALUES (?, 1, 'Жарить')",
                (number,),
            )
    return path


@pytest.fixture
def store(tmp_path):
    result = AccountStore(f"sqlite:///{tmp_path / 'accounts.db'}")
    result.init_schema()
    return result


def counts(path):
    with closing(sqlite3.connect(path)) as conn:
        return {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] for table in recipe_base.TABLES}


def test_upload_and_download_keep_every_row(store, base_file, tmp_path):
    assert recipe_base.remote_version(store) is None
    version, uploaded = recipe_base.upload(store, base_file)
    assert recipe_base.remote_version(store) == version
    assert uploaded["recipes"] == 2500
    copy = recipe_base.download(store, tmp_path / "copy.db", version)
    assert counts(copy) == counts(base_file)
    with closing(sqlite3.connect(copy)) as conn:
        assert conn.execute("SELECT title, calories FROM recipes WHERE id = 2500").fetchone() == ("Блин 2500", 100.5)
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "delete"


def test_new_upload_replaces_old_base(store, base_file, tmp_path):
    recipe_base.upload(store, base_file)
    with closing(connect(base_file)) as conn:
        conn.execute("DELETE FROM recipes WHERE id > 10")
    version, _ = recipe_base.upload(store, base_file)
    copy = recipe_base.download(store, tmp_path / "copy.db", version)
    assert counts(copy)["recipes"] == 10


def test_download_rejects_changed_base(store, base_file, tmp_path):
    recipe_base.upload(store, base_file)
    with pytest.raises(ValueError, match="изменилась"):
        recipe_base.download(store, tmp_path / "copy.db", "другая версия")
    assert not (tmp_path / "copy.db").exists()  # недокачанный файл не подменяет рабочий


def test_signed_in_user_gets_recipes_from_store(store, base_file, tmp_path, monkeypatch):
    recipe_base.upload(store, base_file)
    empty_seed = tmp_path / "seed.db"
    init_db(empty_seed, tmp_path / "seed_backups")
    monkeypatch.setattr(config, "SEED_DB", empty_seed)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "shared.db")
    st.cache_resource.clear()
    app = AppTest.from_file(APP, default_timeout=60)
    app.secrets["DATABASE_URL"] = f"sqlite:///{tmp_path / 'accounts.db'}"
    app.run()
    app.text_input(key="new_email").input("a@b.ru")
    app.text_input(key="new_password").input("correct horse 7")
    app.text_input(key="new_repeat").input("correct horse 7")
    app.button[1].click().run()
    assert not app.exception
    assert counts(app.session_state["user_db_path"])["recipes"] == 2500


def test_recipes_wiki_source_is_registered():
    from core.recipe_manager import SOURCE_NAMES
    from parser.sources import SOURCES
    from parser.sources.recipes_wiki import page_url

    assert "recipes_wiki" in SOURCES and "recipes_wiki" in SOURCE_NAMES
    assert page_url("Apple Pie") == "https://recipes.fandom.com/wiki/Apple_Pie"

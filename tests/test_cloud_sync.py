import base64
import json
import sqlite3
from contextlib import closing
from datetime import date

import pytest
import requests

from core.cloud_sync import CloudSync, SyncError, restore, snapshot
from core.equipment_manager import add_equipment, list_equipment, set_available
from core.history_manager import list_history, record_cooked
from core.ingredient_matcher import IngredientMatcher
from core.inventory_manager import add_item, list_items
from core.preferences import Preferences, load_preferences, save_preferences
from database.database import connect, init_db

TODAY = date(2026, 11, 5)


class Response:
    def __init__(self, status_code, payload=None, content=b""):
        self.status_code = status_code
        self._payload = payload
        self.content = content

    def json(self):
        return self._payload


class FakeGitHub:
    """Подменяет сессию requests и ведёт себя как API содержимого репозитория."""

    def __init__(self):
        self.headers = {}
        self.content = None
        self.sha = None
        self.version = 0
        self.writes = 0
        self.fail_with = None
        self.stale_once = False

    def request(self, method, url, timeout, headers=None, json=None):
        if self.fail_with:
            if isinstance(self.fail_with, Exception):
                raise self.fail_with
            return Response(self.fail_with)
        if method == "GET":
            if self.content is None:
                return Response(404)
            return Response(200, {"sha": self.sha}, self.content)
        if self.stale_once:
            self.stale_once = False
            self.sha = "newer"
            return Response(409)
        if self.content is not None and json.get("sha") != self.sha:
            return Response(409)
        self.content = base64.b64decode(json["content"])
        self.version += 1
        self.sha = f"sha{self.version}"
        self.writes += 1
        return Response(200, {"content": {"sha": self.sha}})


@pytest.fixture
def db(conn):
    IngredientMatcher(conn)
    return conn


@pytest.fixture
def github():
    return FakeGitHub()


def make_sync(github):
    return CloudSync("token", "me/data", github)


def second_device(tmp_path, source):
    """Чистая база, как после перезапуска сервера: тот же начальный набор, без записей."""
    path = tmp_path / "restarted.db"
    with closing(sqlite3.connect(path)) as target:
        source.backup(target)
    with closing(connect(path)) as conn:
        for table in ("cooking_history", "inventory", "user_preferences"):
            conn.execute(f"DELETE FROM {table}")
    return path


def add_recipe(conn, title="Суп"):
    return conn.execute("INSERT INTO recipes (title) VALUES (?)", (title,)).lastrowid


def test_snapshot_round_trip_survives_restart(db, github, tmp_path):
    recipe_id = add_recipe(db)
    add_item(db, "Лук", 3, "шт")
    add_item(db, "Гхи", 200, "г", TODAY)
    record_cooked(db, recipe_id, TODAY, 2, "liked", "вкусно", today=TODAY)
    save_preferences(db, Preferences(people=4, excluded=["грибы"]))
    set_available(db, list_equipment(db)[0].id, True)
    add_equipment(db, "Су-вид")
    make_sync(github).save_if_changed(db)

    with closing(connect(second_device(tmp_path, db))) as restarted:
        restarted.execute("UPDATE kitchen_equipment SET is_available = 0")
        restarted.execute("DELETE FROM kitchen_equipment WHERE name = 'Су-вид'")

        assert make_sync(github).load(restarted) is True

        assert sorted(item.name for item in list_items(restarted)) == ["Гхи", "Лук"]
        assert list_history(restarted)[0].comment == "вкусно"
        assert load_preferences(restarted).people == 4
        equipment = {item.name: item.is_available for item in list_equipment(restarted)}
        assert equipment["Плита"] is True and equipment["Су-вид"] is True


def test_first_start_without_file_is_not_an_error(db, github):
    assert make_sync(github).load(db) is False


def test_no_write_when_nothing_changed(db, github):
    sync = make_sync(github)
    sync.load(db)
    assert sync.save_if_changed(db) is False
    assert github.writes == 0


def test_change_is_written_once(db, github):
    sync = make_sync(github)
    sync.load(db)
    add_item(db, "Рис", 1, "кг")
    assert sync.save_if_changed(db) is True
    assert sync.save_if_changed(db) is False
    assert github.writes == 1


def test_recipes_are_not_stored(db, github):
    add_recipe(db, "Секретный рецепт")
    add_item(db, "Рис", 1, "кг")
    make_sync(github).save_if_changed(db)
    assert "Секретный рецепт" not in github.content.decode("utf-8")
    assert "recipes" not in json.loads(github.content)


def test_next_saves_use_new_version(db, github):
    sync = make_sync(github)
    add_item(db, "Рис", 1, "кг")
    sync.save_if_changed(db)
    add_item(db, "Лук", 1, "кг")
    sync.save_if_changed(db)
    assert github.writes == 2


def test_stale_version_is_refreshed_and_retried(db, github):
    sync = make_sync(github)
    add_item(db, "Рис", 1, "кг")
    sync.save_if_changed(db)
    github.stale_once = True
    add_item(db, "Лук", 1, "кг")
    assert sync.save_if_changed(db) is True
    assert "Лук" in github.content.decode("utf-8")


@pytest.mark.parametrize("failure", [500, 401, requests.ConnectionError("нет сети")])
def test_load_failure_is_reported(db, github, failure):
    github.fail_with = failure
    with pytest.raises(SyncError):
        make_sync(github).load(db)


def test_failed_save_is_retried_on_next_action(db, github):
    sync = make_sync(github)
    add_item(db, "Рис", 1, "кг")
    github.fail_with = 503
    with pytest.raises(SyncError):
        sync.save_if_changed(db)
    github.fail_with = None
    assert sync.save_if_changed(db) is True
    assert "Рис" in github.content.decode("utf-8")


def test_error_text_does_not_contain_token(db, github):
    github.fail_with = requests.ConnectionError("Bearer token-value")
    with pytest.raises(SyncError) as error:
        make_sync(github).load(db)
    assert "token-value" not in str(error.value)


def test_corrupt_file_keeps_database_and_is_reported(db, github):
    add_item(db, "Рис", 1, "кг")
    github.content, github.sha = b"not json", "sha"
    with pytest.raises(SyncError, match="не прочитались"):
        make_sync(github).load(db)
    assert [item.name for item in list_items(db)] == ["Рис"]


def test_bad_rows_roll_back_restore(db):
    add_item(db, "Рис", 1, "кг")
    with pytest.raises(sqlite3.Error):
        restore(db, {"inventory": [{"name": "Без количества"}]})
    assert [item.name for item in list_items(db)] == ["Рис"]


def test_learned_alias_survives_restart(db, github, tmp_path):
    from core.ingredient_matcher import add_alias, find_ingredient

    butter = find_ingredient(db, "масло сливочное")
    add_alias(db, "гхи", butter)
    make_sync(github).save_if_changed(db)
    with closing(connect(second_device(tmp_path, db))) as restarted:
        restarted.execute("DELETE FROM ingredient_aliases WHERE alias = 'гхи'")
        make_sync(github).load(restarted)
        assert find_ingredient(restarted, "гхи") == butter


def test_snapshot_is_stable(db):
    add_item(db, "Рис", 1, "кг")
    assert snapshot(db) == snapshot(db)


def test_plan_survives_restart(db, github, tmp_path):
    from core.meal_planner import PlanRequest, build_plan
    from core.plan_manager import list_plans, save_plan

    for title in ("Гуляш", "Рагу"):
        recipe_id = add_recipe(db, title)
        db.execute("UPDATE recipes SET category = 'Вторые блюда' WHERE id = ?", (recipe_id,))
        for ingredient in (1, 2):
            db.execute(
                "INSERT INTO recipe_ingredients (recipe_id, ingredient_id, quantity, unit) "
                "VALUES (?, ?, 100, 'г')",
                (recipe_id, ingredient),
            )
    save_plan(db, build_plan(db, PlanRequest(start=TODAY, days=2, meals_per_day=1, seed=1)))
    make_sync(github).save_if_changed(db)

    with closing(connect(second_device(tmp_path, db))) as restarted:
        restarted.execute("DELETE FROM meal_plans")
        make_sync(github).load(restarted)
        assert [len(plan.items) for plan in list_plans(restarted)] == [2]


def test_init_db_is_not_needed_twice(tmp_path):
    init_db(tmp_path / "a.db", tmp_path / "b")
    with closing(connect(tmp_path / "a.db")) as conn:
        assert snapshot(conn)["inventory"] == []


# --- Подключение в app.py ---


@pytest.fixture
def cloud_app(tmp_path, monkeypatch, github):
    import streamlit as st

    import config
    from core import cloud_sync

    db_path, backup_dir = tmp_path / "app.db", tmp_path / "backups"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    secrets = {"GITHUB_TOKEN": "token", "DATA_REPO": "me/data"}
    monkeypatch.setattr("interface.components.shared.read_secret", secrets.get)

    class ConnectedSync(cloud_sync.CloudSync):
        def __init__(self, token, repository):
            super().__init__(token, repository, github)

    monkeypatch.setattr(cloud_sync, "CloudSync", ConnectedSync)
    st.cache_resource.clear()
    return db_path


def run_app():
    from streamlit.testing.v1 import AppTest

    import config

    return AppTest.from_file(str(config.BASE_DIR / "app.py"), default_timeout=30).run()


def test_app_loads_saved_data_at_start(cloud_app, github):
    github.content = json.dumps(
        {"inventory": [{"name": "Рис", "quantity": 1, "unit": "кг", "added_on": "2026-11-05"}]}
    ).encode("utf-8")
    github.sha = "sha0"

    app = run_app()

    assert not app.exception
    assert app.metric[1].value == "1"


def test_app_saves_change_after_action(cloud_app, github):
    app = run_app()
    with closing(connect(cloud_app)) as conn:
        add_item(conn, "Рис", 1, "кг")
    app.run()
    assert not app.exception
    assert "Рис" in github.content.decode("utf-8")


def test_app_stops_when_saved_data_cannot_load(cloud_app, github):
    github.fail_with = 500

    app = run_app()

    assert not app.exception
    assert "не загрузились" in app.error[0].value
    assert app.button[0].label == "Попробовать снова"
    assert github.writes == 0
    assert not app.metric


def test_app_warns_when_save_fails(cloud_app, github):
    app = run_app()
    with closing(connect(cloud_app)) as conn:
        add_item(conn, "Рис", 1, "кг")
    github.fail_with = 503
    app.run()
    assert not app.exception
    assert "не сохранены" in app.warning[0].value


def test_settings_says_autosave_is_on(cloud_app):
    app = run_app()
    app.switch_page("interface/pages/settings.py").run()
    assert any("автоматически сохраняются" in item.value for item in app.caption)

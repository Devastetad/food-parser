"""Страницы запускаются настоящим Streamlit. Таблицу data_editor AppTest редактировать не умеет,
её сценарии проверены в браузере, логика сохранения покрыта в test_inventory.py."""
from contextlib import closing

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
from core.ingredient_matcher import IngredientMatcher
from core.inventory_manager import add_item, list_finished, list_items, mark_finished
from database.database import connect, create_backup, init_db, list_backups

APP = str(config.BASE_DIR / "app.py")
PAGES = str(config.BASE_DIR / "interface" / "pages") + "/"


@pytest.fixture
def app_db(tmp_path, monkeypatch):
    db_path, backup_dir = tmp_path / "app.db", tmp_path / "backups"
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BACKUP_DIR", backup_dir)
    init_db(db_path, backup_dir)
    with closing(connect(db_path)) as conn:
        IngredientMatcher(conn)
    st.cache_resource.clear()
    return db_path


def run(page):
    app = AppTest.from_file(PAGES + page, default_timeout=30).run()
    assert not app.exception
    return app


def text_of(app):
    parts = [item.value for group in (app.markdown, app.caption, app.subheader, app.title) for item in group]
    return " ".join(parts)


def with_db(db_path, action):
    with closing(connect(db_path)) as conn:
        return action(conn)


def test_app_starts_with_navigation(app_db):
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert app.title[0].value == "Главная"


def test_app_reports_broken_database(tmp_path, monkeypatch):
    broken = tmp_path / "broken.db"
    broken.write_bytes(b"not a database" * 100)
    monkeypatch.setattr(config, "DB_PATH", broken)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    st.cache_resource.clear()

    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    assert "Не удалось открыть базу данных" in app.error[0].value


def test_home_with_empty_inventory_invites_to_add(app_db):
    app = AppTest.from_file(APP, default_timeout=30).run()  # ссылка работает только в навигации
    assert not app.exception
    assert [metric.value for metric in app.metric] == ["0", "0", "0"]
    assert "Начните с продуктов" in text_of(app)


def test_home_shows_expiring_products(app_db):
    from datetime import date

    with_db(app_db, lambda conn: add_item(conn, "Сметана", 1, "шт", date.today()))
    app = AppTest.from_file(APP, default_timeout=30).run()
    assert not app.exception
    assert app.metric[1].value == "1" and app.metric[2].value == "1"
    assert "Сметана" in text_of(app)
    assert "Сегодня последний день" in text_of(app)


def test_inventory_empty_state(app_db):
    assert "Дома пока пусто" in text_of(run("inventory.py"))


def test_inventory_add_form_saves_product(app_db):
    app = run("inventory.py")
    app.selectbox[0].set_value("Лук репчатый")
    app.number_input[0].set_value(3)
    app.button[0].click().run()

    assert not app.exception
    items = with_db(app_db, list_items)
    assert [(item.name, item.quantity, item.unit) for item in items] == [("Лук репчатый", 3, "шт")]


def test_inventory_form_reports_empty_name_and_saves_nothing(app_db):
    app = run("inventory.py")
    app.button[0].click().run()
    assert "название" in app.error[0].value
    assert with_db(app_db, list_items) == []


def test_inventory_form_reports_zero_quantity(app_db):
    app = run("inventory.py")
    app.selectbox[0].set_value("Соль")
    app.number_input[0].set_value(0)
    app.button[0].click().run()
    assert "больше нуля" in app.error[0].value


def test_finished_product_can_be_returned(app_db):
    def prepare(conn):
        mark_finished(conn, add_item(conn, "Рис", 2, "кг"))

    with_db(app_db, prepare)
    app = run("inventory.py")
    next(button for button in app.button if button.label == "Вернуть").click().run()

    assert [item.name for item in with_db(app_db, list_items)] == ["Рис"]


def test_finished_product_can_be_deleted(app_db):
    def prepare(conn):
        mark_finished(conn, add_item(conn, "Рис", 2, "кг"))

    with_db(app_db, prepare)
    app = run("inventory.py")
    next(button for button in app.button if button.label == "Удалить").click().run()

    assert with_db(app_db, list_finished) == []


def test_unknown_product_can_be_linked(app_db):
    with_db(app_db, lambda conn: add_item(conn, "Масло домашнее", 1, "шт"))
    app = run("inventory.py")
    assert "Не найдено в рецептах" in text_of(app)

    next(button for button in app.button if button.label == "Связать").click().run()

    assert not app.exception
    assert "Не найдено в рецептах" not in text_of(app)


def test_settings_equipment_is_saved(app_db):
    app = run("settings.py")
    stove = next(box for box in app.checkbox if box.label.startswith("Плита"))
    stove.check().run()

    from core.equipment_manager import list_equipment

    assert with_db(app_db, list_equipment)[0].is_available is True
    assert next(box for box in run("settings.py").checkbox if box.label.startswith("Плита")).value


def test_settings_add_equipment_and_duplicate(app_db):
    app = run("settings.py")
    app.text_input[0].set_value("Су-вид")
    next(button for button in app.button if button.label == "Добавить").click().run()
    assert any(box.label == "Су-вид" for box in app.checkbox)

    app.text_input[0].set_value("су-вид")
    next(button for button in app.button if button.label == "Добавить").click().run()
    assert "уже есть" in app.error[0].value


def test_only_custom_equipment_has_remove_button(app_db):
    app = run("settings.py")
    assert not [button for button in app.button if button.label.startswith("Убрать")]

    app.text_input[0].set_value("Су-вид")
    next(button for button in app.button if button.label == "Добавить").click().run()
    remove = next(button for button in app.button if button.label.startswith("Убрать"))
    remove.click().run()

    assert not app.exception
    assert not any(box.label == "Су-вид" for box in app.checkbox)


def test_settings_creates_backup(app_db):
    app = run("settings.py")
    next(button for button in app.button if button.label == "Создать копию сейчас").click().run()
    assert not app.exception
    assert len(list_backups(app_db, config.BACKUP_DIR)) == 1


def test_restore_requires_confirmation_and_restores(app_db):
    backup = create_backup(app_db, config.BACKUP_DIR)
    assert backup.exists()
    with_db(app_db, lambda conn: add_item(conn, "После копии", 1, "шт"))

    app = run("settings.py")
    restore = next(button for button in app.button if button.label == "Восстановить")
    assert restore.disabled

    next(box for box in app.checkbox if box.label == "Заменить текущие данные этой копией").check().run()
    next(button for button in app.button if button.label == "Восстановить").click().run()

    assert not app.exception
    assert with_db(app_db, list_items) == []


def test_database_file_can_be_prepared_for_download(app_db):
    app = run("settings.py")
    next(button for button in app.button if button.label == "Подготовить файл базы").click().run()
    assert not app.exception
    data = app.session_state["database_file"]
    assert data.startswith(b"SQLite format 3")
    assert list_backups(app_db, config.BACKUP_DIR) == []


def test_exported_file_opens_as_database(app_db, tmp_path):
    from database.database import export_database, table_counts

    with_db(app_db, lambda conn: add_item(conn, "Рис", 1, "кг"))
    target = tmp_path / "exported.db"
    target.write_bytes(export_database(app_db))
    assert table_counts(target)["inventory"] == 1


def test_seed_is_copied_on_first_start(tmp_path, monkeypatch):
    seed, db_path = tmp_path / "seed.db", tmp_path / "data" / "app.db"
    init_db(seed, tmp_path / "seed_backups")
    with closing(connect(seed)) as conn:
        conn.execute("INSERT INTO recipes (title) VALUES ('Из начальной базы')")
    monkeypatch.setattr(config, "SEED_DB", seed)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    st.cache_resource.clear()

    app = AppTest.from_file(APP, default_timeout=30).run()

    assert not app.exception
    assert app.metric[0].value == "1"


def test_existing_data_is_not_replaced_by_seed(tmp_path, monkeypatch):
    seed, db_path = tmp_path / "seed.db", tmp_path / "app.db"
    init_db(seed, tmp_path / "seed_backups")
    init_db(db_path, tmp_path / "backups")
    with closing(connect(db_path)) as conn:
        add_item(conn, "Моё", 1, "шт")
    monkeypatch.setattr(config, "SEED_DB", seed)
    monkeypatch.setattr(config, "DB_PATH", db_path)
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    st.cache_resource.clear()

    AppTest.from_file(APP, default_timeout=30).run()

    assert [item.name for item in with_db(db_path, list_items)] == ["Моё"]

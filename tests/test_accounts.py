import logging
import sqlite3
from contextlib import closing

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import config
from core import accounts
from core.accounts import AccountError, AccountStore, hash_password, verify_password
from core.inventory_manager import add_item
from core.logging_setup import mask_email, setup_logging
from database.database import connect, init_db

APP = str(config.BASE_DIR / "app.py")
PASSWORD = "correct horse 7"


@pytest.fixture
def store(tmp_path):
    result = AccountStore(f"sqlite:///{tmp_path / 'accounts.db'}")
    result.init_schema()
    return result


def test_password_is_stored_only_as_salted_hash():
    first, second = hash_password(PASSWORD), hash_password(PASSWORD)
    assert PASSWORD not in first
    assert first != second  # соль у каждого хеша своя
    assert first.startswith("scrypt$")
    assert verify_password(PASSWORD, first)
    assert not verify_password("wrong password", first)
    assert not verify_password(PASSWORD, "broken")


def test_register_and_login(store):
    user = store.register("  Ivan@Mail.RU ", PASSWORD)
    assert user.email == "ivan@mail.ru"
    assert store.authenticate("ivan@mail.ru", PASSWORD).id == user.id
    assert not user.is_pro


@pytest.mark.parametrize(
    "email, password, message",
    [
        ("not-an-email", PASSWORD, "адрес почты"),
        ("a@b.ru", "short", "короткий"),
        ("a@b.ru", "x" * 200, "длинный"),
        ("long@mail.ru", "long@mail.ru", "совпадать"),
    ],
)
def test_register_rules(store, email, password, message):
    with pytest.raises(AccountError, match=message):
        store.register(email, password)


def test_email_is_registered_once(store):
    store.register("a@b.ru", PASSWORD)
    with pytest.raises(AccountError, match="уже зарегистрирована"):
        store.register("A@B.ru", PASSWORD)


def test_wrong_login_does_not_reveal_whether_account_exists(store):
    store.register("a@b.ru", PASSWORD)
    with pytest.raises(AccountError) as wrong_password:
        store.authenticate("a@b.ru", "wrong password")
    with pytest.raises(AccountError) as no_account:
        store.authenticate("nobody@b.ru", PASSWORD)
    assert str(wrong_password.value) == str(no_account.value) == "Неверная почта или пароль"


def test_account_locks_after_failed_logins(store):
    store.register("a@b.ru", PASSWORD)
    for _ in range(accounts.MAX_FAILED_LOGINS):
        with pytest.raises(AccountError, match="Неверная"):
            store.authenticate("a@b.ru", "wrong password")
    with pytest.raises(AccountError, match="Слишком много"):
        store.authenticate("a@b.ru", PASSWORD)  # верный пароль тоже не пускает до конца блокировки


def test_successful_login_resets_failed_count(store):
    store.register("a@b.ru", PASSWORD)
    for _ in range(accounts.MAX_FAILED_LOGINS - 1):
        with pytest.raises(AccountError):
            store.authenticate("a@b.ru", "wrong password")
    store.authenticate("a@b.ru", PASSWORD)
    with pytest.raises(AccountError, match="Неверная"):
        store.authenticate("a@b.ru", "wrong password")


def test_change_password(store):
    user = store.register("a@b.ru", PASSWORD)
    with pytest.raises(AccountError, match="Текущий пароль"):
        store.change_password(user.id, "wrong password", "new password 1")
    store.change_password(user.id, PASSWORD, "new password 1")
    assert store.authenticate("a@b.ru", "new password 1").id == user.id


def test_user_data_round_trip(store):
    user = store.register("a@b.ru", PASSWORD)
    assert store.load_data(user.id) is None
    store.save_data(user.id, {"inventory": [{"name": "Мука"}]}, "h1")
    store.save_data(user.id, {"inventory": [{"name": "Соль"}]}, "h2")
    assert store.load_data(user.id) == {"inventory": [{"name": "Соль"}]}


def test_payment_activates_plan_and_creates_receipt(store, tmp_path):
    user = store.register("a@b.ru", PASSWORD)
    payment = store.create_payment(user.id, "pro_month")
    receipt = store.mark_paid(payment, "provider-1")
    assert store.get_user(user.id).is_pro
    with pytest.raises(AccountError, match="уже обработан"):
        store.mark_paid(payment, "provider-1")
    with closing(sqlite3.connect(tmp_path / "accounts.db")) as conn:
        row = conn.execute("SELECT kind, email, total_kopecks FROM receipts WHERE id = ?", (receipt,)).fetchone()
    assert row == ("income", "a@b.ru", accounts.PLANS["pro_month"].price_kopecks)
    assert [status for _, _, _, status, _, _ in store.payments_of(user.id)] == ["paid"]


def test_unknown_plan_is_rejected(store):
    user = store.register("a@b.ru", PASSWORD)
    with pytest.raises(AccountError, match="тарифа"):
        store.create_payment(user.id, "free_forever")


def test_audit_log_has_no_passwords(store, tmp_path):
    store.register("a@b.ru", PASSWORD)
    with pytest.raises(AccountError):
        store.authenticate("a@b.ru", "wrong password")
    with closing(sqlite3.connect(tmp_path / "accounts.db")) as conn:
        events = [row[0] for row in conn.execute("SELECT event FROM audit_log ORDER BY created_at")]
        dump = "\n".join(conn.iterdump())
    assert events[0] == "register" and "login_failed" in events
    assert PASSWORD not in dump and "wrong password" not in dump


def test_logging_masks_email(tmp_path, caplog):
    assert mask_email("ivan@mail.ru") == "i***@mail.ru"
    assert mask_email("broken") == "***"
    logger = setup_logging(tmp_path)
    assert logger.handlers
    assert setup_logging(tmp_path) is logger and len(logger.handlers) == len(setup_logging().handlers)
    logging.getLogger("food_parser").propagate = True
    try:
        with caplog.at_level(logging.INFO, logger="food_parser"):
            store = AccountStore(f"sqlite:///{tmp_path / 'log.db'}")
            store.init_schema()
            store.register("ivan@mail.ru", PASSWORD)
    finally:
        logging.getLogger("food_parser").propagate = False
    assert "i***@mail.ru" in caplog.text
    assert "ivan@mail.ru" not in caplog.text and PASSWORD not in caplog.text


@pytest.fixture
def accounts_app(tmp_path, monkeypatch):
    seed = tmp_path / "seed.db"
    init_db(seed, tmp_path / "seed_backups")
    monkeypatch.setattr(config, "SEED_DB", seed)
    monkeypatch.setattr(config, "USERS_DIR", tmp_path / "users")
    monkeypatch.setattr(config, "DB_PATH", tmp_path / "shared.db")
    monkeypatch.setattr(config, "BACKUP_DIR", tmp_path / "backups")
    st.cache_resource.clear()
    app = AppTest.from_file(APP, default_timeout=30)
    app.secrets["DATABASE_URL"] = f"sqlite:///{tmp_path / 'accounts.db'}"
    return app


def sign_up(app, email):
    app.text_input(key="new_email").input(email)
    app.text_input(key="new_password").input(PASSWORD)
    app.text_input(key="new_repeat").input(PASSWORD)
    app.button[1].click().run()
    assert not app.exception


def test_pages_need_login(accounts_app):
    app = accounts_app.run()
    assert not app.exception
    assert app.title[0].value == "Personal Food Parser"
    assert [tab.label for tab in app.tabs] == ["Вход", "Регистрация"]


def test_users_do_not_see_each_others_data(accounts_app, tmp_path):
    app = accounts_app.run()
    sign_up(app, "first@b.ru")
    assert app.title[0].value == "Главная"
    first_db = app.session_state["user_db_path"]
    with closing(connect(first_db)) as conn:
        add_item(conn, "Мука", 1, "кг")
    app.run()  # после действия записи уходят в хранилище

    st.cache_resource.clear()
    other = AppTest.from_file(APP, default_timeout=30)
    other.secrets["DATABASE_URL"] = accounts_app.secrets["DATABASE_URL"]
    other.run()
    sign_up(other, "second@b.ru")
    second_db = other.session_state["user_db_path"]
    assert second_db != first_db
    with closing(connect(second_db)) as conn:
        assert conn.execute("SELECT count(*) FROM inventory").fetchone()[0] == 0

    store = AccountStore(accounts_app.secrets["DATABASE_URL"])
    first = store.authenticate("first@b.ru", PASSWORD)
    assert [row["name"] for row in store.load_data(first.id)["inventory"]] == ["Мука"]


class FakeCursor:
    def __init__(self, log):
        self.log = log

    def execute(self, sql, params=()):
        self.log.append((sql, params))

    def fetchone(self):
        return None


class FakeConnection:
    def __init__(self, log):
        self.log = log

    def cursor(self):
        return FakeCursor(self.log)

    def commit(self):
        pass

    def rollback(self):
        pass

    def close(self):
        pass


def test_postgres_uses_its_placeholders_and_closes_public_api(monkeypatch):
    store = AccountStore("postgresql://user:p%40ss@db.example.com:6543/postgres")
    log = []
    monkeypatch.setattr(store, "_connect", lambda: FakeConnection(log))
    store.init_schema()
    store.get_user("abc")
    statements = [sql for sql, _ in log]
    assert all("?" not in sql for sql in statements)
    assert statements[-1].endswith("WHERE id = %s")
    secured = {sql.split()[2] for sql in statements if "ROW LEVEL SECURITY" in sql}
    assert secured == set(accounts.TABLES)


def test_unknown_database_url_is_rejected():
    with pytest.raises(ValueError):
        AccountStore("mysql://localhost/db")

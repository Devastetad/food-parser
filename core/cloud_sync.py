"""Автосохранение данных пользователя в приватный репозиторий GitHub.

Диск Streamlit Cloud временный: при перезапуске база возвращается к начальной. Поэтому
записи пользователя хранятся в одном JSON-файле репозитория и подгружаются при старте.
Рецепты туда не входят: они лежат в приложении и не меняются.
"""
import base64
import hashlib
import json
import sqlite3

import requests

from database.database import transaction

API = "https://api.github.com"
DATA_FILE = "food_parser_data.json"
TIMEOUT = 20

# Порядок важен: при записи родительские таблицы идут раньше дочерних
USER_TABLES = [
    "inventory", "cooking_history", "meal_plans", "meal_plan_items", "shopping_checks",
    "user_preferences",
]  # fmt: skip


class SyncError(Exception):
    """GitHub недоступен или отказал. Текст показывается пользователю."""


def snapshot(conn: sqlite3.Connection) -> dict:
    """Все записи пользователя в виде обычных данных."""
    data = {
        table: [dict(row) for row in conn.execute(f"SELECT * FROM {table} ORDER BY 1, 2")]
        for table in USER_TABLES
    }
    data["kitchen_equipment"] = [
        dict(row) for row in conn.execute("SELECT name, is_available FROM kitchen_equipment ORDER BY name")
    ]
    data["ingredient_aliases"] = [
        dict(row)
        for row in conn.execute(
            "SELECT ingredient_id, alias, normalized_alias FROM ingredient_aliases "
            "ORDER BY normalized_alias"
        )
    ]
    return data


def restore(conn: sqlite3.Connection, data: dict) -> None:
    """Заменяет записи пользователя сохранёнными. При ошибке база не меняется."""
    with transaction(conn):
        for table in reversed(USER_TABLES):
            conn.execute(f"DELETE FROM {table}")
        for table in USER_TABLES:
            for row in data.get(table, []):
                _insert(conn, table, row, "INSERT")
        for row in data.get("kitchen_equipment", []):
            conn.execute(
                "INSERT INTO kitchen_equipment (name, is_available) VALUES (:name, :is_available) "
                "ON CONFLICT (name) DO UPDATE SET is_available = excluded.is_available",
                row,
            )
        for row in data.get("ingredient_aliases", []):
            _insert(conn, "ingredient_aliases", row, "INSERT OR REPLACE")


def _insert(conn: sqlite3.Connection, table: str, row: dict, verb: str) -> None:
    columns = ", ".join(row)
    marks = ", ".join(f":{name}" for name in row)
    conn.execute(f"{verb} INTO {table} ({columns}) VALUES ({marks})", row)


def _encode(data: dict) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1)


class CloudSync:
    def __init__(self, token: str, repository: str, session=None):
        self.repository = repository
        self._session = session or requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {token}", "X-GitHub-Api-Version": "2022-11-28"}
        )
        self._sha: str | None = None
        self._saved_hash: str | None = None

    @property
    def _url(self) -> str:
        return f"{API}/repos/{self.repository}/contents/{DATA_FILE}"

    def _request(self, method: str, **kwargs):
        try:
            return self._session.request(method, self._url, timeout=TIMEOUT, **kwargs)
        except requests.RequestException as error:
            raise SyncError(f"Нет связи с GitHub: {type(error).__name__}") from error

    def _read_sha(self) -> str | None:
        response = self._request("GET", headers={"Accept": "application/vnd.github+json"})
        if response.status_code == 404:
            return None
        if response.status_code != 200:
            raise SyncError(f"GitHub ответил {response.status_code}")
        return response.json()["sha"]

    def load(self, conn: sqlite3.Connection) -> bool:
        """Подгружает сохранённое. False: файла ещё нет, это первый запуск."""
        self._sha = self._read_sha()
        if self._sha is None:
            self._saved_hash = _hash(snapshot(conn))
            return False
        response = self._request("GET", headers={"Accept": "application/vnd.github.raw+json"})
        if response.status_code != 200:
            raise SyncError(f"GitHub ответил {response.status_code}")
        try:
            data = json.loads(response.content.decode("utf-8"))
            restore(conn, data)
        except (ValueError, sqlite3.Error) as error:
            raise SyncError(f"Сохранённые данные не прочитались: {error}") from error
        self._saved_hash = _hash(snapshot(conn))
        return True

    def save_if_changed(self, conn: sqlite3.Connection) -> bool:
        """Отправляет данные, если они изменились. True: была запись на GitHub."""
        data = snapshot(conn)
        current = _hash(data)
        if current == self._saved_hash:
            return False
        body = {
            "message": "Обновление данных",
            "content": base64.b64encode(_encode(data).encode("utf-8")).decode("ascii"),
        }
        if self._sha:
            body["sha"] = self._sha
        response = self._request("PUT", json=body)
        if response.status_code in (409, 422):  # версия файла устарела: берём новую и повторяем
            body["sha"] = self._read_sha()
            response = self._request("PUT", json=body)
        if response.status_code not in (200, 201):
            raise SyncError(f"GitHub ответил {response.status_code}")
        self._sha = response.json()["content"]["sha"]
        self._saved_hash = current
        return True


def _hash(data: dict) -> str:
    return hashlib.sha256(_encode(data).encode("utf-8")).hexdigest()

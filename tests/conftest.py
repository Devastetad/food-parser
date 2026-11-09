try:  # первая загрузка из потока AppTest изредка падает на полпути, поэтому заранее
    import pandas  # noqa: F401
except ImportError:  # без pandas упадут только тесты страниц с таблицами
    pass
from contextlib import closing

import pytest

from database.database import connect, init_db


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_db(path, tmp_path / "backups")
    return path


@pytest.fixture
def backup_dir(tmp_path):
    return tmp_path / "backups"


@pytest.fixture
def conn(db_path):
    with closing(connect(db_path)) as connection:
        yield connection

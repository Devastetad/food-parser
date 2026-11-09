"""Заливает базу рецептов в Postgres (Supabase), откуда её берёт сайт.

Запуск: python scripts/upload_recipe_base.py [файл.db]
Без файла собирается из локальной базы так же, как seed.db (только рецепты, без личных данных).
Адрес базы берётся из переменной DATABASE_URL или из .streamlit/secrets.toml и не печатается.
"""
import os
import sys
import tempfile
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")
import config
from core.accounts import AccountStore, explain_store_error
from core.recipe_base import upload
from scripts.build_web import build_seed


def database_url() -> str:
    if os.environ.get("DATABASE_URL"):
        return os.environ["DATABASE_URL"]
    secrets = config.BASE_DIR / ".streamlit" / "secrets.toml"
    if secrets.exists():
        value = tomllib.loads(secrets.read_text(encoding="utf-8")).get("DATABASE_URL")
        if value:
            return value
    raise SystemExit("Нет DATABASE_URL: добавьте его в .streamlit/secrets.toml")


def main() -> None:
    try:
        store = AccountStore(database_url())
        store.init_schema()
    except Exception as error:
        raise SystemExit(f"Хранилище не открылось: {explain_store_error(error)}") from None
    if len(sys.argv) > 1:
        source = Path(sys.argv[1])
        version, counts = upload(store, source)
    else:
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "seed.db"
            build_seed(source)
            version, counts = upload(store, source)
    print("Загружено:", ", ".join(f"{table} {count}" for table, count in counts.items()))
    print("Версия", version[:12], "— сайт подхватит её в течение 10 минут")


if __name__ == "__main__":
    main()

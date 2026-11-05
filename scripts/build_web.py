"""Собирает браузерную версию для GitHub Pages в папку docs.

Запуск из корня проекта: python scripts/build_web.py

Приложение работает в браузере через stlite. В архив попадает код и начальная база, где
есть только рецепты: инвентарь, история и планы пользователя не публикуются.
"""
import shutil
import sqlite3
import sys
import zipfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import config  # noqa: E402

DOCS = ROOT / "docs"
SOURCES = ["app.py", "config.py", "database", "core", "interface", "parser"]
SKIPPED_PARSER_FILES = {"__main__.py", "base_parser.py", "http_client.py", "recipe_extractor.py"}
SKIPPED_DIRS = {"sources", "__pycache__"}

PRIVATE_TABLES = [
    "inventory", "cooking_history", "meal_plans", "meal_plan_items", "shopping_checks",
    "user_preferences", "raw_recipes", "raw_items", "parser_runs",
]  # fmt: skip

STLITE_VERSION = "1.12.1"
INDEX_HTML = f"""<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no">
  <title>Personal Food Parser</title>
  <link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@stlite/browser@{STLITE_VERSION}/build/stlite.css">
  <style>
    :root {{ --page: #FAF8F3; --text: #2B2A26; }}
    body {{ margin: 0; background: var(--page); color: var(--text); }}
  </style>
</head>
<body>
  <div id="root"></div>
  <script type="module">
    import {{ mount }} from "https://cdn.jsdelivr.net/npm/@stlite/browser@{STLITE_VERSION}/build/stlite.js";
    mount({{
      entrypoint: "app.py",
      requirements: ["pymorphy3", "pymorphy3-dicts-ru"],
      archives: [{{ url: "./app.zip", format: "zip" }}],
      idbfsMountpoints: ["/mnt/food_parser"],
      env: {{
        FOOD_PARSER_DATA_DIR: "/mnt/food_parser",
        FOOD_PARSER_JOURNAL: "DELETE",
        FOOD_PARSER_BACKUP_KEEP: "3",
      }},
      streamlitConfig: {{
        "theme.base": "light",
        "theme.primaryColor": "#2F6B4F",
        "theme.backgroundColor": "#FAF8F3",
        "theme.secondaryBackgroundColor": "#F0ECE3",
        "theme.textColor": "#2B2A26",
        "client.toolbarMode": "minimal",
      }},
    }}, document.getElementById("root"));
  </script>
</body>
</html>
"""


def build_seed(target: Path) -> None:
    """Копия базы только с рецептами и справочниками."""
    with closing(sqlite3.connect(config.DB_PATH)) as source, closing(sqlite3.connect(target)) as copy:
        source.backup(copy)
    with closing(sqlite3.connect(target)) as conn:
        conn.execute("PRAGMA journal_mode = DELETE")
        for table in PRIVATE_TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("UPDATE kitchen_equipment SET is_available = 0")
        conn.commit()
        conn.execute("VACUUM")


def source_files():
    for name in SOURCES:
        path = ROOT / name
        if path.is_file():
            yield path
            continue
        for file in sorted(path.rglob("*.py")):
            relative = file.relative_to(path)
            if SKIPPED_DIRS & set(relative.parts):
                continue
            if name == "parser" and file.name in SKIPPED_PARSER_FILES:
                continue
            yield file


def main() -> None:
    shutil.rmtree(DOCS, ignore_errors=True)
    DOCS.mkdir()
    seed = DOCS / "seed.db"
    build_seed(seed)
    with zipfile.ZipFile(DOCS / "app.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for file in source_files():
            archive.write(file, file.relative_to(ROOT).as_posix())
        archive.write(seed, config.SEED_DB.name)
    seed.unlink()
    (DOCS / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (DOCS / ".nojekyll").write_text("", encoding="utf-8")
    print(f"docs/app.zip: {(DOCS / 'app.zip').stat().st_size / 1024 / 1024:.1f} МБ")


if __name__ == "__main__":
    main()

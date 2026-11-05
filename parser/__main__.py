"""Сбор и нормализация рецептов: python -m parser [--source имя] [--limit N] [--normalize-only]."""
import argparse
import sys

from core.recipe_manager import import_raw_recipes
from database.database import init_db
from parser.base_parser import SourceUnavailable, run_source
from parser.sources import SOURCES


def main() -> int:
    arguments = argparse.ArgumentParser(description="Сбор рецептов из источников")
    arguments.add_argument("--source", choices=sorted(SOURCES), default=None)
    arguments.add_argument("--limit", type=int, default=None, help="страниц за запуск")
    arguments.add_argument(
        "--normalize-only", action="store_true", help="только перенести собранное в базу рецептов"
    )
    options = arguments.parse_args()

    init_db()
    exit_code = 0
    names = [options.source] if options.source else sorted(SOURCES)
    for name in [] if options.normalize_only else names:
        try:
            stats = run_source(SOURCES[name](), limit=options.limit)
        except SourceUnavailable as error:
            print(f"{name}: {error}", file=sys.stderr)
            exit_code = 1
            continue
        print(
            f"{name}: страниц с рецептами {stats.parsed}, рецептов {stats.recipes}, "
            f"без рецепта {stats.rejected}, ошибок {stats.errors}, "
            f"пропущено как готовые {stats.skipped}"
        )

    imported = import_raw_recipes()
    print(
        f"В базе рецептов: перенесено {imported.imported}, дублей {imported.duplicates}, "
        f"без ингредиентов {imported.without_ingredients}"
    )
    return exit_code


if __name__ == "__main__":
    sys.exit(main())

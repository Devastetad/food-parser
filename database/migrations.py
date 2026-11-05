"""Версионные миграции схемы.

Версия схемы хранится в PRAGMA user_version. Уже выпущенные миграции не меняем:
любое изменение схемы добавляется новым элементом в конец MIGRATIONS.
"""
import sqlite3

# Названия ингредиентов сравниваются по normalized_*, которые заполняет Python:
# COLLATE NOCASE в SQLite не приводит к одному регистру кириллицу.
V1_INITIAL = """
CREATE TABLE recipes (
    id            INTEGER PRIMARY KEY,
    title         TEXT NOT NULL CHECK (length(trim(title)) > 0),
    description   TEXT,
    category      TEXT,
    cuisine       TEXT,
    cook_time_min INTEGER CHECK (cook_time_min IS NULL OR cook_time_min > 0),
    servings      INTEGER CHECK (servings IS NULL OR servings > 0),
    difficulty    TEXT CHECK (difficulty IS NULL OR difficulty IN ('easy', 'medium', 'hard')),
    image_path    TEXT,
    source        TEXT,
    source_url    TEXT UNIQUE,
    -- КБЖУ на порцию: нужны генератору, у многих рецептов их не будет
    calories      REAL CHECK (calories IS NULL OR calories >= 0),
    protein_g     REAL CHECK (protein_g IS NULL OR protein_g >= 0),
    fat_g         REAL CHECK (fat_g IS NULL OR fat_g >= 0),
    carbs_g       REAL CHECK (carbs_g IS NULL OR carbs_g >= 0),
    created_at    TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_recipes_category ON recipes (category);
CREATE INDEX idx_recipes_cook_time ON recipes (cook_time_min);

CREATE TABLE ingredients (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL CHECK (length(trim(name)) > 0),
    normalized_name TEXT NOT NULL UNIQUE,
    category        TEXT,
    default_unit    TEXT
);

-- Альтернативные названия: «филе курицы», «chicken breast» -> один ингредиент
CREATE TABLE ingredient_aliases (
    id               INTEGER PRIMARY KEY,
    ingredient_id    INTEGER NOT NULL REFERENCES ingredients (id) ON DELETE CASCADE,
    alias            TEXT NOT NULL CHECK (length(trim(alias)) > 0),
    normalized_alias TEXT NOT NULL UNIQUE
);
CREATE INDEX idx_aliases_ingredient ON ingredient_aliases (ingredient_id);

-- Свой id: один ингредиент может встретиться в рецепте дважды (тесто и начинка).
-- quantity пуст для «по вкусу», исходная строка остаётся в raw_text.
CREATE TABLE recipe_ingredients (
    id            INTEGER PRIMARY KEY,
    recipe_id     INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients (id) ON DELETE RESTRICT,
    quantity      REAL CHECK (quantity IS NULL OR quantity > 0),
    unit          TEXT,
    raw_text      TEXT
);
CREATE INDEX idx_recipe_ingredients_recipe ON recipe_ingredients (recipe_id);
CREATE INDEX idx_recipe_ingredients_ingredient ON recipe_ingredients (ingredient_id);

CREATE TABLE cooking_instructions (
    recipe_id    INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    step_number  INTEGER NOT NULL CHECK (step_number > 0),
    description  TEXT NOT NULL CHECK (length(trim(description)) > 0),
    duration_min INTEGER CHECK (duration_min IS NULL OR duration_min > 0),
    PRIMARY KEY (recipe_id, step_number)
);

-- RESTRICT: рецепт с историей удалить нельзя, иначе режим «только новые блюда»
-- перестанет его исключать.
CREATE TABLE cooking_history (
    id        INTEGER PRIMARY KEY,
    recipe_id INTEGER NOT NULL REFERENCES recipes (id) ON DELETE RESTRICT,
    cooked_on TEXT NOT NULL CHECK (date(cooked_on) IS NOT NULL),
    rating    TEXT CHECK (rating IS NULL OR rating IN ('liked', 'ok', 'disliked', 'tired')),
    comment   TEXT,
    servings  INTEGER CHECK (servings IS NULL OR servings > 0)
);
CREATE INDEX idx_history_recipe ON cooking_history (recipe_id);
CREATE INDEX idx_history_cooked_on ON cooking_history (cooked_on);

-- Партии, а не уникальные продукты: у двух упаковок могут быть разные сроки годности.
-- ingredient_id связывает партию с базой; пуст, пока продукт не распознан.
CREATE TABLE inventory (
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL CHECK (length(trim(name)) > 0),
    ingredient_id INTEGER REFERENCES ingredients (id) ON DELETE SET NULL,
    quantity      REAL NOT NULL CHECK (quantity >= 0),
    unit          TEXT NOT NULL CHECK (length(trim(unit)) > 0),
    added_on      TEXT NOT NULL DEFAULT CURRENT_DATE CHECK (date(added_on) IS NOT NULL),
    expires_on    TEXT CHECK (expires_on IS NULL OR date(expires_on) IS NOT NULL),
    finished_on   TEXT CHECK (finished_on IS NULL OR date(finished_on) IS NOT NULL)
);
CREATE INDEX idx_inventory_ingredient ON inventory (ingredient_id);
CREATE INDEX idx_inventory_expires ON inventory (expires_on);

CREATE TABLE kitchen_equipment (
    id           INTEGER PRIMARY KEY,
    name         TEXT NOT NULL UNIQUE CHECK (length(trim(name)) > 0),
    is_available INTEGER NOT NULL DEFAULT 0 CHECK (is_available IN (0, 1))
);

-- Какая техника нужна рецепту: без этой связи генератор не сможет её учитывать
CREATE TABLE recipe_equipment (
    recipe_id    INTEGER NOT NULL REFERENCES recipes (id) ON DELETE CASCADE,
    equipment_id INTEGER NOT NULL REFERENCES kitchen_equipment (id) ON DELETE RESTRICT,
    PRIMARY KEY (recipe_id, equipment_id)
);
CREATE INDEX idx_recipe_equipment_equipment ON recipe_equipment (equipment_id);

INSERT INTO kitchen_equipment (name) VALUES
    ('Плита'), ('Духовка'), ('Микроволновка'), ('Мультиварка'),
    ('Аэрогриль'), ('Блендер'), ('Миксер'), ('Гриль');
"""

# Сырые данные парсера. Этап нормализации читает их отсюда, поэтому его можно
# перезапускать без повторного скачивания. raw_items: одна страница источника,
# raw_recipes: рецепты с неё (на странице бывает несколько вариантов блюда).
V2_PARSER = """
CREATE TABLE raw_items (
    id           INTEGER PRIMARY KEY,
    source       TEXT NOT NULL,
    item_id      TEXT NOT NULL,
    revision     TEXT NOT NULL,
    title        TEXT NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('parsed', 'rejected', 'error')),
    reason       TEXT,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (source, item_id)
);
CREATE INDEX idx_raw_items_status ON raw_items (status);

CREATE TABLE raw_recipes (
    id          INTEGER PRIMARY KEY,
    raw_item_id INTEGER NOT NULL REFERENCES raw_items (id) ON DELETE CASCADE,
    variant     INTEGER NOT NULL CHECK (variant > 0),
    title       TEXT NOT NULL CHECK (length(trim(title)) > 0),
    source_url  TEXT NOT NULL,
    license     TEXT NOT NULL,
    payload     TEXT NOT NULL CHECK (json_valid(payload)),
    UNIQUE (raw_item_id, variant)
);

CREATE TABLE parser_runs (
    id          INTEGER PRIMARY KEY,
    source      TEXT NOT NULL,
    started_at  TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    status      TEXT NOT NULL DEFAULT 'running'
                CHECK (status IN ('running', 'finished', 'failed', 'interrupted')),
    parsed      INTEGER NOT NULL DEFAULT 0,
    rejected    INTEGER NOT NULL DEFAULT 0,
    errors      INTEGER NOT NULL DEFAULT 0,
    skipped     INTEGER NOT NULL DEFAULT 0,
    recipes     INTEGER NOT NULL DEFAULT 0,
    message     TEXT
);
"""

# Поля, которые появились при нормализации. time_is_estimated: время посчитано по шагам,
# а не указано в рецепте. image_url: файл пока не скачан, лицензия картинки не проверена.
V3_NORMALIZED = """
ALTER TABLE recipes ADD COLUMN image_url TEXT;
ALTER TABLE recipes ADD COLUMN license TEXT;
ALTER TABLE recipes ADD COLUMN time_is_estimated INTEGER NOT NULL DEFAULT 0
    CHECK (time_is_estimated IN (0, 1));
ALTER TABLE recipe_ingredients ADD COLUMN note TEXT;
ALTER TABLE recipe_ingredients ADD COLUMN group_name TEXT;
"""

# Предпочтения, планы питания и отметки в списке покупок
V4_PLANNING = """
ALTER TABLE recipes ADD COLUMN nutrition_is_estimated INTEGER NOT NULL DEFAULT 0
    CHECK (nutrition_is_estimated IN (0, 1));

CREATE TABLE user_preferences (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL CHECK (json_valid(value))
);

CREATE TABLE meal_plans (
    id         INTEGER PRIMARY KEY,
    name       TEXT NOT NULL,
    start_date TEXT NOT NULL CHECK (date(start_date) IS NOT NULL),
    days       INTEGER NOT NULL CHECK (days > 0),
    people     INTEGER NOT NULL CHECK (people > 0),
    mode       TEXT NOT NULL CHECK (mode IN ('mixed', 'new')),
    warnings   TEXT NOT NULL DEFAULT '[]' CHECK (json_valid(warnings)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE meal_plan_items (
    id        INTEGER PRIMARY KEY,
    plan_id   INTEGER NOT NULL REFERENCES meal_plans (id) ON DELETE CASCADE,
    day_index INTEGER NOT NULL CHECK (day_index >= 0),
    meal      TEXT NOT NULL CHECK (meal IN ('breakfast', 'lunch', 'dinner', 'snack')),
    recipe_id INTEGER NOT NULL REFERENCES recipes (id) ON DELETE RESTRICT,
    servings  INTEGER NOT NULL CHECK (servings > 0),
    cooked_on TEXT CHECK (cooked_on IS NULL OR date(cooked_on) IS NOT NULL)
);
CREATE INDEX idx_plan_items_plan ON meal_plan_items (plan_id);
CREATE INDEX idx_plan_items_recipe ON meal_plan_items (recipe_id);

CREATE TABLE shopping_checks (
    plan_id       INTEGER NOT NULL REFERENCES meal_plans (id) ON DELETE CASCADE,
    ingredient_id INTEGER NOT NULL REFERENCES ingredients (id) ON DELETE CASCADE,
    PRIMARY KEY (plan_id, ingredient_id)
);
"""

MIGRATIONS = [V1_INITIAL, V2_PARSER, V3_NORMALIZED, V4_PLANNING]


def get_version(conn: sqlite3.Connection) -> int:
    return conn.execute("PRAGMA user_version").fetchone()[0]


def apply_migrations(conn: sqlite3.Connection, migrations: list[str] = MIGRATIONS) -> None:
    """Применяет недостающие миграции. Каждая идёт одной транзакцией вместе с версией."""
    for version, sql in enumerate(migrations, start=1):
        if version <= get_version(conn):
            continue
        try:
            conn.executescript(
                f"BEGIN IMMEDIATE;\n{sql}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
        except sqlite3.Error:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise

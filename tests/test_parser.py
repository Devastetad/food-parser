import bz2
import json
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from database.database import connect
from parser.base_parser import (
    BaseSource,
    NoRecipeFound,
    RawRecipe,
    IngredientLine,
    SourceItem,
    SourceUnavailable,
    run_source,
)
from parser.http_client import FetchError, PoliteClient, RobotsDisallowed
from parser.recipe_extractor import extract_recipes, page_url
from parser.sources import SOURCES
from parser.sources.wikibooks import EXTRACTOR_VERSION, WikibooksSource


def texts(recipe):
    return [line.text for line in recipe.ingredients]


# --- Извлечение рецепта из вики-разметки (форматы взяты с реальных страниц) ---


STANDARD_PAGE = """{{Рецепт
| Категория = Супы <!-- см [[Категория:Виды яств]] -->
| Кухня = Татарская кухня
| Порций = 4
| Время = 1 час
| Сложность = 3
| Энергетическая ценность =
| Изображение = Азу блюдо.jpg
}}
'''Азу''' это традиционное блюдо, состоящее из [[w:мясо|мяса]].

== Состав ==
Количество продуктов приведено из расчёта на одну порцию.

* Говядина 160 грамм
* [[Рецепт:Томат-пюре|томат-пюре]] 20 грамм<ref>Кулинария, 1955</ref>
* перец

== Приготовление <ref>{{Книга|заглавие=Кулинария}}</ref>==
# Обжарить [[мясо]].
# Залить [[Рецепт:Бульон|бульоном]] и тушить.

== Примечания ==
{{примечания}}
"""


def test_standard_page():
    [recipe] = extract_recipes("Рецепт:Азу", STANDARD_PAGE)
    assert recipe.title == "Азу"
    assert texts(recipe) == ["Говядина 160 грамм", "томат-пюре 20 грамм", "перец"]
    assert recipe.steps == ["Обжарить мясо", "Залить бульоном и тушить"]
    assert recipe.ingredient_notes == [
        "Количество продуктов приведено из расчёта на одну порцию"
    ]
    assert recipe.description == "Азу это традиционное блюдо, состоящее из мяса"
    assert recipe.category == "Супы"
    assert recipe.cuisine == "Татарская кухня"
    assert recipe.time_text == "1 час"
    assert recipe.servings_text == "4"
    assert recipe.difficulty_text == "3"
    assert recipe.calories_text is None
    assert recipe.source_url == page_url("Рецепт:Азу")
    assert recipe.image_url.endswith("Special:FilePath/%D0%90%D0%B7%D1%83_%D0%B1%D0%BB%D1%8E%D0%B4%D0%BE.jpg")


def test_page_with_variants_gives_separate_recipes_with_unique_urls():
    page = """
== Соус Томат 1 ==
=== Состав ===
* томаты: 1 кг
=== Приготовление ===
# Протереть томаты
== Соус Томат 2 ==
=== Состав ===
* томаты: 2 кг
=== Приготовление ===
# Варить томаты
=== Состав ===
* томаты: 3 кг
=== Приготовление ===
# Запечь томаты
== Примечания ==
"""
    recipes = extract_recipes("Рецепт:Соус Томат", page)
    assert [recipe.title for recipe in recipes] == [
        "Соус Томат 1",
        "Соус Томат 2",
        "Соус Томат 2 (вариант 3)",
    ]
    assert texts(recipes[2]) == ["томаты: 3 кг"]
    assert len({recipe.source_url for recipe in recipes}) == 3


def test_ingredient_groups_and_nested_steps():
    page = """
== Ингредиенты ==
=== Овощи ===
* 1 баклажан
=== Соус ===
* 30 мл масла
=== Приготовление ===
* Нарезать овощи.
"""
    [recipe] = extract_recipes("Рецепт:Рататуй", page)
    assert [(line.group, line.text) for line in recipe.ingredients] == [
        ("Овощи", "1 баклажан"),
        ("Соус", "30 мл масла"),
    ]
    assert recipe.steps == ["Нарезать овощи"]


def test_alternative_headings():
    page = """
== Необходимые продукты ==
* Мука&nbsp;— 600&nbsp;г;
== Как готовить ==
Муку просеять.
"""
    [recipe] = extract_recipes("Рецепт:Пельмени", page)
    assert texts(recipe) == ["Мука — 600 г"]


@pytest.mark.parametrize("heading", ["Cостав", "Игредиенты", "'''Ингредиенты:'''"])
def test_heading_typos_are_recognized(heading):
    page = f"== {heading} ==\n* соль\n== Приготовление ==\n# Посолить\n"
    assert texts(extract_recipes("Рецепт:Х", page)[0]) == ["соль"]


def test_section_with_list_and_text_without_subheadings():
    page = """
== Посуда ==
Подойдёт толстостенная кастрюля.
== Классический вариант ==
* Рис — 250 грамм
* Баранина — 150 грамм
* Морковь — 150 грамм

; Подготовка
Промыть рис и нарезать мясо некрупными кусками, морковь соломкой.
== См. также ==
"""
    [recipe] = extract_recipes("Рецепт:Плов", page)
    assert len(recipe.ingredients) == 3
    assert recipe.steps == ["Промыть рис и нарезать мясо некрупными кусками, морковь соломкой"]


def test_recipe_heading_after_ingredients_means_steps():
    page = "== Ингредиенты ==\n# Кукуруза\n# Сахар\n== Рецепт ==\n# Разогреть кастрюлю\n"
    [recipe] = extract_recipes("Рецепт:Попкорн", page)
    assert texts(recipe) == ["Кукуруза", "Сахар"]
    assert recipe.steps == ["Разогреть кастрюлю"]


def test_numbered_list_inside_ingredients_section_means_steps():
    page = "== Ингредиенты ==\n*Сыр 250 г\n*Яйцо 2 шт.\n\n#Натереть сыр.\n#Запечь.\n"
    [recipe] = extract_recipes("Рецепт:Рэмкин", page)
    assert texts(recipe) == ["Сыр 250 г", "Яйцо 2 шт"]
    assert recipe.steps == ["Натереть сыр", "Запечь"]


def test_ingredients_written_as_a_sentence():
    page = "== Состав ==\n1 кг перца, 50–70 г кориандра (молотого, свежего), 0,5 кг соли.\n== Приготовление ==\nСмешать.\n"
    [recipe] = extract_recipes("Рецепт:Х", page)
    assert texts(recipe) == ["1 кг перца", "50–70 г кориандра (молотого, свежего)", "0,5 кг соли"]


def test_ingredient_table():
    page = """
== Состав ==
{| class="wikitable"
! Продукт !! Вес
|-
| style="width:2em;" | говядина || — || 57 фунтов
|-
|Дрожжи
|10 г
|}
== Приготовление ==
# Смешать
"""
    [recipe] = extract_recipes("Рецепт:Х", page)
    assert texts(recipe) == ["говядина 57 фунтов", "Дрожжи 10 г"]


def test_markup_noise_is_removed():
    page = """
== Состав ==
[[Файл:Сыр.jpg|thumb|Сыр [[w:Сыр|твёрдый]]]]
* [[Рецепт:Томат-пюре|томат-пюре[[Рецепт:Томат-пюре|: 25 г
* сало<br>(или масло)
== Приготовление ==
<div class="mw-collapsible">
{{Начало цитаты|источник=Книга,
1952}}
# Рыбу промыть[7].
{{Конец цитаты}}
</div>
"""
    [recipe] = extract_recipes("Рецепт:Х", page)
    assert texts(recipe) == ["томат-пюре: 25 г", "сало (или масло)"]
    assert recipe.steps == ["Рыбу промыть"]


@pytest.mark.parametrize(
    "value",
    ["Adjika e-citizen.jpg", "[[Файл:Adjika e-citizen.jpg|200px]]", "Файл:Adjika e-citizen.jpg"],
)
def test_image_value_formats(value):
    page = f"{{{{Рецепт\n| Изображение = {value}\n}}}}\n== Состав ==\n* соль\n== Приготовление ==\n# Шаг\n"
    assert extract_recipes("Рецепт:Х", page)[0].image_url.endswith("/Adjika_e-citizen.jpg")


@pytest.mark.parametrize(
    "page, reason",
    [
        ("'''Борщ''' — суп.\n== Подстраницы ==\n* [[/История]]\n", "Нет разделов"),
        ("== Состав ==\n* соль\n", "Нет раздела приготовления"),
        ("== Приготовление ==\n# Варить\n", "Нет раздела состава"),
        ("== Состав ==\n== Приготовление ==\n", "пусты"),
        ("{{Начало цитаты}}Пулярдку зачистить.{{Конец цитаты}}", "Нет разделов"),
    ],
)
def test_pages_without_recipe_are_rejected_with_reason(page, reason):
    with pytest.raises(NoRecipeFound, match=reason):
        extract_recipes("Рецепт:Х", page)


# --- Цикл сбора: прогресс, защита от повторов, ошибки ---


class FakeSource(BaseSource):
    name = "fake"
    license = "CC0"

    def __init__(self, items, fail_on=None, prepare_error=None, stop_after=None):
        self.items = items
        self.fail_on = fail_on
        self.prepare_error = prepare_error
        self.stop_after = stop_after
        self.extracted = []

    def prepare(self):
        if self.prepare_error:
            raise self.prepare_error

    def iter_items(self):
        for index, item in enumerate(self.items):
            if index == self.stop_after:
                raise KeyboardInterrupt
            yield item

    def extract(self, item):
        self.extracted.append(item.item_id)
        if item.item_id == self.fail_on:
            raise ValueError("сбой разбора")
        if item.content == "нет":
            raise NoRecipeFound("Нет рецепта")
        return [
            RawRecipe(
                title=f"{item.title} {number}",
                source_url=f"https://example.org/{item.item_id}/{number}",
                ingredients=[IngredientLine("соль")],
                steps=[item.content],
            )
            for number in range(1, 3)
        ]


def page(item_id, revision="1", content="Варить"):
    return SourceItem(item_id, revision, f"Блюдо {item_id}", content)


def counts(db_path):
    with closing(connect(db_path)) as conn:
        return (
            conn.execute("SELECT count(*) FROM raw_items").fetchone()[0],
            conn.execute("SELECT count(*) FROM raw_recipes").fetchone()[0],
        )


def last_run(db_path):
    with closing(connect(db_path)) as conn:
        return dict(conn.execute("SELECT * FROM parser_runs ORDER BY id DESC").fetchone())


def test_run_saves_recipes_and_statistics(db_path):
    stats = run_source(FakeSource([page("1"), page("2", content="нет")]), db_path)

    assert (stats.parsed, stats.rejected, stats.recipes) == (1, 1, 2)
    assert counts(db_path) == (2, 2)
    run = last_run(db_path)
    assert (run["status"], run["parsed"], run["rejected"], run["recipes"]) == ("finished", 1, 1, 2)
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT license, payload FROM raw_recipes").fetchone()
        rejected = conn.execute("SELECT reason FROM raw_items WHERE status = 'rejected'").fetchone()
    assert row["license"] == "CC0"
    assert json.loads(row["payload"])["ingredients"] == [{"text": "соль", "group": None}]
    assert rejected["reason"] == "Нет рецепта"


def test_second_run_adds_nothing(db_path):
    items = [page("1"), page("2", content="нет")]
    run_source(FakeSource(items), db_path)
    source = FakeSource(items)

    stats = run_source(source, db_path)

    assert stats.skipped == 2
    assert source.extracted == []
    assert counts(db_path) == (2, 2)


def test_changed_revision_replaces_old_recipes(db_path):
    run_source(FakeSource([page("1", "1", "Варить")]), db_path)

    run_source(FakeSource([page("1", "2", "Жарить")]), db_path)

    assert counts(db_path) == (1, 2)
    with closing(connect(db_path)) as conn:
        payloads = [json.loads(row["payload"]) for row in conn.execute("SELECT payload FROM raw_recipes")]
    assert all(payload["steps"] == ["Жарить"] for payload in payloads)


def test_page_that_lost_its_recipe_keeps_no_stale_rows(db_path):
    run_source(FakeSource([page("1", "1")]), db_path)
    run_source(FakeSource([page("1", "2", "нет")]), db_path)
    assert counts(db_path) == (1, 0)


def test_error_on_one_page_does_not_stop_run_and_is_retried(db_path):
    items = [page("1"), page("2"), page("3")]

    stats = run_source(FakeSource(items, fail_on="2"), db_path)

    assert (stats.parsed, stats.errors) == (2, 1)
    with closing(connect(db_path)) as conn:
        reason = conn.execute("SELECT reason FROM raw_items WHERE status = 'error'").fetchone()[0]
    assert "сбой разбора" in reason

    source = FakeSource(items)
    stats = run_source(source, db_path)
    assert source.extracted == ["2"]
    assert (stats.parsed, stats.skipped) == (1, 2)


def test_interrupted_run_resumes_from_where_it_stopped(db_path):
    items = [page("1"), page("2"), page("3")]
    with pytest.raises(KeyboardInterrupt):
        run_source(FakeSource(items, stop_after=2), db_path)
    assert last_run(db_path)["status"] == "interrupted"
    assert last_run(db_path)["parsed"] == 2

    source = FakeSource(items)
    run_source(source, db_path)

    assert source.extracted == ["3"]
    assert counts(db_path) == (3, 6)


def test_run_left_running_after_crash_is_closed_by_next_run(db_path):
    with closing(connect(db_path)) as conn:
        conn.execute("INSERT INTO parser_runs (source) VALUES ('fake')")
    run_source(FakeSource([]), db_path)
    with closing(connect(db_path)) as conn:
        statuses = [row[0] for row in conn.execute("SELECT status FROM parser_runs ORDER BY id")]
    assert statuses == ["interrupted", "finished"]


def test_limit_counts_only_new_pages(db_path):
    items = [page(str(number)) for number in range(1, 6)]
    run_source(FakeSource(items), db_path, limit=2)
    assert counts(db_path)[0] == 2
    stats = run_source(FakeSource(items), db_path, limit=2)
    assert (stats.parsed, stats.skipped) == (2, 2)
    assert counts(db_path)[0] == 4


def test_unavailable_source_is_recorded_as_failed(db_path):
    source = FakeSource([], prepare_error=SourceUnavailable("Сайт недоступен"))
    with pytest.raises(SourceUnavailable):
        run_source(source, db_path)
    run = last_run(db_path)
    assert (run["status"], run["message"]) == ("failed", "Сайт недоступен")


def test_failure_saves_finished_pages(db_path):
    class BrokenDump(FakeSource):
        def iter_items(self):
            yield page("1")
            raise EOFError("Файл обрезан")

    with pytest.raises(EOFError):
        run_source(BrokenDump([]), db_path)
    assert counts(db_path) == (1, 2)
    assert last_run(db_path)["status"] == "failed"


# --- HTTP-клиент на настоящем локальном сервере ---


class Site:
    """Локальный сайт, поведение которого задаёт тест."""

    def __init__(self):
        self.robots = "User-agent: *\nDisallow: /private/\n"
        self.files = {}
        self.etag = '"v1"'
        self.failures = []  # статусы или (статус, Retry-After) для очередных запросов
        self.requests = []
        self.cut_after = None  # оборвать ответ после стольких байт
        self.ignore_range = False


@pytest.fixture
def site():
    state = Site()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_HEAD(self):
            self.respond(body=False)

        def do_GET(self):
            self.respond(body=True)

        def respond(self, body):
            state.requests.append((self.command, self.path, self.headers.get("Range")))
            if self.path == "/robots.txt":
                if state.robots is None:
                    self.send_error(404)
                elif isinstance(state.robots, int):
                    self.send_error(state.robots)
                else:
                    self.send(200, state.robots.encode(), body)
                return
            if state.failures:
                failure = state.failures.pop(0)
                status, retry = failure if isinstance(failure, tuple) else (failure, None)
                self.send_response(status)
                if retry:
                    self.send_header("Retry-After", str(retry))
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if self.path not in state.files:
                self.send_error(404)
                return
            self.send(200, state.files[self.path], body)

        def send(self, status, data, body):
            start = 0
            header = self.headers.get("Range")
            if header and not state.ignore_range and body:
                start = int(header.split("=")[1].rstrip("-"))
                status = 206
            payload = data[start:]
            self.send_response(status)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("ETag", state.etag)
            self.end_headers()
            if body:
                if state.cut_after is not None and self.path != "/robots.txt":
                    self.wfile.write(payload[: state.cut_after])
                    self.wfile.flush()
                    state.cut_after = None
                    self.close_connection = True
                else:
                    self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    state.url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True)
    thread.start()
    yield state
    server.shutdown()
    server.server_close()


@pytest.fixture
def client():
    sleeps = []
    polite = PoliteClient(min_interval=0, max_attempts=4, sleep=sleeps.append)
    polite.sleeps = sleeps
    return polite


def test_get_returns_page(site, client):
    site.files["/page"] = b"ok"
    assert client.get(f"{site.url}/page").text == "ok"


def test_user_agent_is_sent(site):
    seen = []
    original = PoliteClient._send

    def spy(self, method, url, **kwargs):
        seen.append(self._session.headers["User-Agent"])
        return original(self, method, url, **kwargs)

    site.files["/page"] = b"ok"
    polite = PoliteClient(user_agent="TestAgent/1", min_interval=0)
    polite._send = spy.__get__(polite)
    polite.get(f"{site.url}/page")
    assert set(seen) == {"TestAgent/1"}


def test_robots_blocks_disallowed_url_without_requesting_it(site, client):
    site.files["/private/x"] = b"secret"
    with pytest.raises(RobotsDisallowed):
        client.get(f"{site.url}/private/x")
    assert not any(path == "/private/x" for _, path, _ in site.requests)


def test_robots_is_read_once_per_site(site, client):
    site.files["/a"] = site.files["/b"] = b"ok"
    client.get(f"{site.url}/a")
    client.get(f"{site.url}/b")
    assert sum(path == "/robots.txt" for _, path, _ in site.requests) == 1


def test_missing_robots_means_no_restrictions(site, client):
    site.robots = None
    site.files["/page"] = b"ok"
    assert client.get(f"{site.url}/page").text == "ok"


def test_failing_robots_stops_crawling(site, client):
    site.robots = 503
    site.files["/page"] = b"ok"
    with pytest.raises(FetchError):
        client.get(f"{site.url}/page")
    assert not any(path == "/page" for _, path, _ in site.requests)


def test_429_waits_for_retry_after(site, client):
    site.files["/page"] = b"ok"
    site.failures = [(429, 7)]
    assert client.get(f"{site.url}/page").text == "ok"
    assert 7 in client.sleeps


def test_server_errors_use_growing_backoff(site, client):
    site.files["/page"] = b"ok"
    site.failures = [503, 502, 500]
    assert client.get(f"{site.url}/page").text == "ok"
    assert client.sleeps == [1, 2, 4]


def test_gives_up_after_max_attempts(site, client):
    site.files["/page"] = b"ok"
    site.failures = [503] * 10
    with pytest.raises(FetchError, match="503"):
        client.get(f"{site.url}/page")
    assert len([1 for _, path, _ in site.requests if path == "/page"]) == client.max_attempts


def test_missing_page_is_not_retried(site, client):
    with pytest.raises(FetchError, match="404"):
        client.get(f"{site.url}/none")
    assert client.sleeps == []


def test_unreachable_site_is_reported(client):
    client.max_attempts = 1
    with pytest.raises(FetchError):
        client.get("http://127.0.0.1:9/page")


def test_requests_to_one_site_are_spaced(site):
    sleeps = []
    polite = PoliteClient(min_interval=30, sleep=sleeps.append)
    site.files["/a"] = b"ok"
    polite.get(f"{site.url}/a")
    polite.get(f"{site.url}/a")
    assert len(sleeps) >= 2  # robots -> страница -> страница
    assert all(25 < delay <= 30 for delay in sleeps)


def test_crawl_delay_from_robots_is_respected(site):
    site.robots = "User-agent: *\nCrawl-delay: 50\n"
    site.files["/a"] = b"ok"
    sleeps = []
    polite = PoliteClient(min_interval=1, sleep=sleeps.append)
    polite.get(f"{site.url}/a")
    polite.get(f"{site.url}/a")
    assert max(sleeps) > 45


def test_download_saves_file_and_skips_unchanged(site, client, tmp_path):
    site.files["/dump"] = b"x" * 1000
    dest = tmp_path / "dump.bz2"

    assert client.download(f"{site.url}/dump", dest) is True
    assert dest.read_bytes() == b"x" * 1000
    assert client.download(f"{site.url}/dump", dest) is False


def test_download_replaces_file_when_etag_changes(site, client, tmp_path):
    site.files["/dump"] = b"old"
    dest = tmp_path / "dump.bz2"
    client.download(f"{site.url}/dump", dest)

    site.files["/dump"] = b"newer"
    site.etag = '"v2"'

    assert client.download(f"{site.url}/dump", dest) is True
    assert dest.read_bytes() == b"newer"


def test_interrupted_download_resumes_with_range(site, client, tmp_path):
    data = bytes(range(256)) * 1200
    site.files["/dump"] = data
    site.cut_after = 200_000
    dest = tmp_path / "dump.bz2"

    with pytest.raises(FetchError):
        client.download(f"{site.url}/dump", dest)
    assert not dest.exists()

    client.download(f"{site.url}/dump", dest)

    assert dest.read_bytes() == data
    ranges = [header for _, path, header in site.requests if path == "/dump" and header]
    assert ranges == [f"bytes={3 * 64 * 1024}-"]


def test_failed_download_keeps_previous_file(site, client, tmp_path):
    site.files["/dump"] = b"old"
    dest = tmp_path / "dump.bz2"
    client.download(f"{site.url}/dump", dest)

    site.files["/dump"] = b"newer version"
    site.etag = '"v2"'
    site.cut_after = 3
    with pytest.raises(FetchError):
        client.download(f"{site.url}/dump", dest)

    assert dest.read_bytes() == b"old"


def test_resume_from_server_without_range_support_restarts(site, client, tmp_path):
    data = b"abcdefghij" * 30_000
    site.files["/dump"] = data
    site.cut_after = 200_000
    dest = tmp_path / "dump.bz2"
    with pytest.raises(FetchError):
        client.download(f"{site.url}/dump", dest)

    site.ignore_range = True
    client.download(f"{site.url}/dump", dest)

    assert dest.read_bytes() == data


def test_part_of_old_version_is_not_glued_to_new_one(site, client, tmp_path):
    site.files["/dump"] = b"A" * 1000
    site.cut_after = 400
    dest = tmp_path / "dump.bz2"
    with pytest.raises(FetchError):
        client.download(f"{site.url}/dump", dest)

    site.files["/dump"] = b"B" * 1000
    site.etag = '"v2"'
    client.download(f"{site.url}/dump", dest)

    assert dest.read_bytes() == b"B" * 1000
    assert list(tmp_path.glob("*.part")) == []


# --- Источник Викиучебник ---


def dump_xml(pages):
    body = "".join(
        f"<page><title>{title}</title><ns>{namespace}</ns><id>{page_id}</id>{extra}"
        f"<revision><id>{revision}</id><text>{text}</text></revision></page>"
        for title, namespace, page_id, revision, text, extra in pages
    )
    return (
        '<mediawiki xmlns="http://www.mediawiki.org/xml/export-0.11/">' + body + "</mediawiki>"
    ).encode()


RECIPE_XML = "== Состав ==\n* соль\n== Приготовление ==\n# Посолить\n"
SAMPLE_DUMP = dump_xml(
    [
        ("Рецепт:Суп", 104, 10, 500, RECIPE_XML, ""),
        ("Рецепт:Старый", 104, 11, 501, "", '<redirect title="Рецепт:Суп"/>'),
        ("Кулинария", 0, 12, 502, RECIPE_XML, ""),
        ("Рецепт:Пустой", 104, 13, 503, "Только текст", ""),
    ]
)


@pytest.fixture
def wikibooks(site, client, tmp_path):
    site.files["/dump.bz2"] = bz2.compress(SAMPLE_DUMP)
    return WikibooksSource(client, tmp_path / "sources" / "dump.bz2", f"{site.url}/dump.bz2")


def test_source_is_registered():
    assert SOURCES["wikibooks_ru"] is WikibooksSource


def test_dump_gives_only_recipe_pages_without_redirects(wikibooks):
    wikibooks.prepare()
    items = list(wikibooks.iter_items())
    assert [item.item_id for item in items] == ["10", "13"]
    assert items[0].title == "Рецепт:Суп"
    assert items[0].revision == f"500:v{EXTRACTOR_VERSION}"


def test_full_collection_from_dump(wikibooks, db_path):
    stats = run_source(wikibooks, db_path)

    assert (stats.parsed, stats.rejected, stats.recipes) == (1, 1, 1)
    with closing(connect(db_path)) as conn:
        row = conn.execute("SELECT title, source_url, license FROM raw_recipes").fetchone()
    assert row["title"] == "Суп"
    assert row["license"] == "CC BY-SA 4.0"
    assert row["source_url"].startswith("https://ru.wikibooks.org/wiki/")


def test_rerun_after_dump_update_processes_only_changed_pages(wikibooks, site, db_path):
    run_source(wikibooks, db_path)
    site.files["/dump.bz2"] = bz2.compress(
        dump_xml(
            [
                ("Рецепт:Суп", 104, 10, 600, RECIPE_XML, ""),
                ("Рецепт:Пустой", 104, 13, 503, "Только текст", ""),
            ]
        )
    )
    site.etag = '"v2"'

    stats = run_source(wikibooks, db_path)

    assert (stats.parsed, stats.skipped) == (1, 1)


def test_works_offline_on_downloaded_dump(wikibooks, db_path):
    wikibooks.prepare()
    wikibooks.client.max_attempts = 1
    wikibooks.dump_url = "http://127.0.0.1:9/dump.bz2"
    assert run_source(wikibooks, db_path).parsed == 1


def test_unavailable_without_local_dump(client, tmp_path, db_path):
    client.max_attempts = 1
    source = WikibooksSource(client, tmp_path / "none.bz2", "http://127.0.0.1:9/dump.bz2")
    with pytest.raises(SourceUnavailable):
        run_source(source, db_path)


def test_corrupt_dump_fails_run_instead_of_finishing_silently(wikibooks, db_path):
    wikibooks.prepare()
    data = wikibooks.dump_path.read_bytes()
    wikibooks.dump_path.write_bytes(data[: len(data) // 2])
    wikibooks.prepare = lambda: None

    with pytest.raises((EOFError, OSError)):
        run_source(wikibooks, db_path)
    assert last_run(db_path)["status"] == "failed"


# --- Английский Викиучебник ---


EN_PAGE = """{{recipesummary|category=Cake recipes|servings=10|time=60 minutes|difficulty=3|image=[[Image:Cake.jpg|300px]]
}}
{{recipe}} | [[Cookbook:Dessert|Dessert]]

A soft cake.

==Ingredients==
* 110 [[Cookbook:Gram|g]] [[Cookbook:Butter|butter]]
* 2 eggs

==Procedure==
# Mix everything.
# Bake.

==Notes, tips, and variations==
* Add nuts.
"""


def test_english_markup_goes_through_same_extractor():
    from parser.sources.wikibooks_en import to_extractor_markup

    [recipe] = extract_recipes("Cookbook:Cake", to_extractor_markup(EN_PAGE))
    assert recipe.title == "Cake"
    assert texts(recipe) == ["110 g butter", "2 eggs"]
    assert recipe.steps == ["Mix everything", "Bake"]
    assert recipe.servings_text == "10"
    assert recipe.time_text == "60 minutes"
    assert recipe.difficulty_text == "3"
    assert recipe.image_url.endswith("/Cake.jpg")


@pytest.mark.parametrize(
    "heading", ["Directions", "Method", "Instructions", "Preparation", "Procedure"]
)
def test_english_step_headings(heading):
    from parser.sources.wikibooks_en import to_extractor_markup

    page = f"== Ingredients ==\n* salt\n== {heading} ==\n# Add salt\n"
    assert extract_recipes("Cookbook:X", to_extractor_markup(page))[0].steps == ["Add salt"]


def translated_file(tmp_path, recipes):
    path = tmp_path / "translated.jsonl"
    page = {"id": "7", "revision": "9", "title": "Cookbook:Cake", "recipes": recipes}
    path.write_text(json.dumps(page, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def test_translated_source_saves_recipes(tmp_path, db_path):
    from parser.sources.wikibooks_en import WikibooksEnSource

    recipe = {
        "title": "Пирог", "source_url": "https://en.wikibooks.org/wiki/Cookbook:Cake",
        "ingredients": [{"text": "2 яйца", "group": None}], "steps": ["Смешать"],
        "ingredient_notes": [], "description": None, "category": None, "cuisine": None,
        "time_text": None, "servings_text": None, "difficulty_text": None,
        "calories_text": None, "image_url": None,
    }  # fmt: skip
    source = WikibooksEnSource(translated_file(tmp_path, [recipe]))

    stats = run_source(source, db_path)

    assert (stats.parsed, stats.recipes) == (1, 1)
    assert run_source(source, db_path).skipped == 1


def test_page_without_translated_recipes_is_rejected(tmp_path, db_path):
    from parser.sources.wikibooks_en import WikibooksEnSource

    assert run_source(WikibooksEnSource(translated_file(tmp_path, [])), db_path).rejected == 1


def test_missing_translation_file_is_reported(tmp_path, db_path):
    from parser.sources.wikibooks_en import WikibooksEnSource

    with pytest.raises(SourceUnavailable, match="перевод"):
        run_source(WikibooksEnSource(tmp_path / "none.jsonl"), db_path)


def test_translated_recipe_is_marked_in_database(tmp_path, db_path):
    from core.recipe_manager import import_raw_recipes
    from parser.sources.wikibooks_en import WikibooksEnSource

    recipe = {
        "title": "Пирог", "source_url": "https://en.wikibooks.org/wiki/Cookbook:Cake",
        "ingredients": [{"text": "2 яйца", "group": None}], "steps": ["Смешать"],
        "ingredient_notes": [], "description": None, "category": None, "cuisine": None,
        "time_text": None, "servings_text": None, "difficulty_text": None,
        "calories_text": None, "image_url": None,
    }  # fmt: skip
    run_source(WikibooksEnSource(translated_file(tmp_path, [recipe])), db_path)
    import_raw_recipes(db_path)
    with closing(connect(db_path)) as conn:
        assert "машинный перевод" in conn.execute("SELECT source FROM recipes").fetchone()[0]


@pytest.mark.parametrize(
    "title, page, expected",
    [
        ("Пружина", "Cookbook:Spring Rolls", "Пружина (Spring Rolls)"),
        ("Хлеб и", "Cookbook:Saj Bread I", "Хлеб (Saj Bread I)"),
        ("Луковый суп", "Cookbook:Onion Soup", "Луковый суп"),
    ],
)
def test_doubtful_translated_title_keeps_original(title, page, expected):
    from parser.sources.wikibooks_en import clear_title

    assert clear_title(title, page) == expected

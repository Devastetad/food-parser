"""Скачивает статьи Recipes Wiki (recipes.fandom.com, CC BY-SA) через API в jsonl.

Запуск: python scripts/fetch_recipes_wiki.py
Можно прервать и запустить снова: продолжит с места остановки.
"""
import json
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config

API = "https://recipes.fandom.com/api.php"
OUT = config.SOURCES_DIR / "recipes_wiki.jsonl"
STATE = config.SOURCES_DIR / "recipes_wiki.continue"
BATCH = 50


def main() -> None:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    session = requests.Session()
    session.headers["User-Agent"] = config.USER_AGENT
    params = {
        "action": "query", "generator": "allpages", "gapnamespace": 0, "gaplimit": BATCH,
        "gapfilterredir": "nonredirects", "prop": "revisions", "rvprop": "content|ids",
        "rvslots": "main", "format": "json", "formatversion": 2,
    }  # fmt: skip
    if STATE.exists():
        params["gapcontinue"] = STATE.read_text(encoding="utf-8")
    total = sum(1 for _ in OUT.open(encoding="utf-8")) if OUT.exists() else 0
    while True:
        for attempt in range(config.REQUEST_MAX_ATTEMPTS):
            try:
                response = session.get(API, params=params, timeout=60)
                response.raise_for_status()
                data = response.json()
                break
            except (requests.RequestException, ValueError) as error:
                print("повтор после ошибки", type(error).__name__, flush=True)
                time.sleep(10 * (attempt + 1))
        else:
            raise SystemExit("API не отвечает")
        with OUT.open("a", encoding="utf-8") as file:
            for page in data.get("query", {}).get("pages", []):
                revisions = page.get("revisions")
                if not revisions:
                    continue
                revision = revisions[0]
                record = {
                    "id": page["pageid"], "revision": revision["revid"], "title": page["title"],
                    "text": revision["slots"]["main"].get("content", ""),
                }  # fmt: skip
                file.write(json.dumps(record, ensure_ascii=False) + "\n")
                total += 1
        following = data.get("continue", {}).get("gapcontinue")
        if not following:
            break
        params["gapcontinue"] = following
        STATE.write_text(following, encoding="utf-8")
        print(total, flush=True)
        time.sleep(config.REQUEST_MIN_INTERVAL)
    STATE.unlink(missing_ok=True)
    print("готово", total, flush=True)


if __name__ == "__main__":
    main()

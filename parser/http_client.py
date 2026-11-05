"""HTTP-клиент для всех источников: robots.txt, паузы, повторы, докачка файлов."""
import hashlib
import time
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

import requests

import config

RETRY_STATUSES = {429, 500, 502, 503, 504}
CHUNK_SIZE = 64 * 1024  # при обрыве теряется только незаписанный кусок


class FetchError(Exception):
    """Запрос не удался и после повторов."""


class RobotsDisallowed(FetchError):
    """Адрес закрыт для роботов в robots.txt сайта."""


class PoliteClient:
    def __init__(
        self,
        user_agent: str = config.USER_AGENT,
        min_interval: float = config.REQUEST_MIN_INTERVAL,
        max_attempts: int = config.REQUEST_MAX_ATTEMPTS,
        sleep=time.sleep,
    ):
        self.user_agent = user_agent
        self.min_interval = min_interval
        self.max_attempts = max_attempts
        self._sleep = sleep
        self._session = requests.Session()
        self._session.headers["User-Agent"] = user_agent
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser] = {}

    def get(self, url: str) -> requests.Response:
        self._check_robots(url)
        return self._send("GET", url)

    def download(self, url: str, dest: Path) -> bool:
        """Скачивает файл с докачкой. Возвращает False, если локальный файл уже свежий."""
        self._check_robots(url)
        head = self._send("HEAD", url)
        etag = head.headers.get("ETag", "")
        size = int(head.headers.get("Content-Length", 0))

        etag_file = dest.with_name(dest.name + ".etag")
        if etag and dest.exists() and etag_file.exists():
            if etag_file.read_text(encoding="utf-8") == etag:
                return False

        dest.parent.mkdir(parents=True, exist_ok=True)
        # В имени части хеш ETag: докачка не склеит куски разных версий файла
        version = hashlib.sha1(etag.encode()).hexdigest()[:12]
        part = dest.with_name(f"{dest.name}.{version}.part")
        for stale in dest.parent.glob(f"{dest.name}.*.part"):
            if stale != part:
                stale.unlink()

        done = part.stat().st_size if part.exists() else 0
        if not size or done < size:
            self._fetch_to_part(url, part, done)
        if size and part.stat().st_size != size:
            raise FetchError(f"Файл скачан не полностью: {url}")

        part.replace(dest)
        etag_file.write_text(etag, encoding="utf-8")
        return True

    def _fetch_to_part(self, url: str, part: Path, done: int) -> None:
        headers = {"Range": f"bytes={done}-"} if done else {}
        try:
            with self._send("GET", url, headers=headers, stream=True) as response:
                # 200 вместо 206: сервер отдаёт файл с начала
                mode = "ab" if response.status_code == 206 else "wb"
                with part.open(mode) as file:
                    for chunk in response.iter_content(CHUNK_SIZE):
                        file.write(chunk)
        except requests.RequestException as error:
            raise FetchError(f"Обрыв при скачивании {url}: {error}") from error

    def _send(self, method: str, url: str, **kwargs) -> requests.Response:
        """Запрос с паузами и повторами при перегрузке и сбоях сети."""
        host = urlsplit(url).netloc
        reason = ""
        for attempt in range(self.max_attempts):
            self._throttle(host)
            try:
                response = self._session.request(method, url, timeout=(10, 60), **kwargs)
            except requests.RequestException as error:
                reason = str(error)
                delay = 2**attempt
            else:
                if response.status_code not in RETRY_STATUSES:
                    if response.status_code >= 400:
                        raise FetchError(f"{url}: HTTP {response.status_code}")
                    return response
                reason = f"HTTP {response.status_code}"
                delay = _retry_after(response) or 2**attempt
                response.close()
            if attempt + 1 < self.max_attempts:
                self._sleep(delay)
        raise FetchError(f"{url}: {reason}, попыток: {self.max_attempts}")

    def _throttle(self, host: str) -> None:
        interval = self.min_interval
        robots = self._robots.get(host)
        if robots is not None:
            interval = max(interval, robots.crawl_delay(self.user_agent) or 0)
        last = self._last_request.get(host)
        if last is not None:
            wait = interval - (time.monotonic() - last)
            if wait > 0:
                self._sleep(wait)
        self._last_request[host] = time.monotonic()

    def _check_robots(self, url: str) -> None:
        parts = urlsplit(url)
        if parts.netloc not in self._robots:
            self._robots[parts.netloc] = self._load_robots(f"{parts.scheme}://{parts.netloc}")
        if not self._robots[parts.netloc].can_fetch(self.user_agent, url):
            raise RobotsDisallowed(f"Запрещено правилами robots.txt: {url}")

    def _load_robots(self, origin: str) -> RobotFileParser:
        robots = RobotFileParser()
        try:
            response = self._send("GET", f"{origin}/robots.txt")
        except FetchError as error:
            # Нет файла (4xx) значит нет ограничений. Сбой сервера: правила неизвестны, не идём.
            if " HTTP 4" not in str(error):
                raise
            robots.parse([])
        else:
            robots.parse(response.text.splitlines())
        return robots


def _retry_after(response: requests.Response) -> int | None:
    value = response.headers.get("Retry-After", "")
    return int(value) if value.isdigit() else None

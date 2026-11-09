"""Аккаунты: регистрация, вход, данные пользователя, платежи и чеки.

Хранилище — Postgres (Supabase) на сайте или файл SQLite для проверки на компьютере.
Пароль хранится только хешем scrypt с солью. Данные пользователя (запасы, планы, история)
лежат одним JSON на пользователя, общие рецепты сюда не попадают.
Реальной оплаты нет: таблицы платежей и чеков готовы к подключению платёжного сервиса.
"""
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import ssl
import uuid
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import unquote, urlparse

from core.logging_setup import get_logger, mask_email

log = get_logger("accounts")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EMAIL_MAX = 254
PASSWORD_MIN = 8
PASSWORD_MAX = 128  # длиннее не нужно, а огромная строка нагрузила бы хеширование
MAX_FAILED_LOGINS = 5
LOCK_TIME = timedelta(minutes=15)

# scrypt: 16 МБ памяти на проверку, подбор на видеокартах дорог
SCRYPT_N, SCRYPT_R, SCRYPT_P = 2**14, 8, 1
SCRYPT_LEN = 32


@dataclass(frozen=True)
class Plan:
    title: str
    price_kopecks: int
    days: int


# Цена условная: менять здесь, когда появится настоящая оплата
PLANS = {"pro_month": Plan("Pro на месяц", 19900, 30)}

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_login_at TEXT,
        failed_logins INTEGER NOT NULL DEFAULT 0,
        locked_until TEXT,
        plan TEXT NOT NULL DEFAULT 'free',
        plan_until TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS user_data (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        data TEXT NOT NULL,
        data_hash TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS email_codes (
        user_id TEXT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        sent_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id TEXT PRIMARY KEY,
        user_id TEXT,
        event TEXT NOT NULL,
        detail TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS audit_log_user ON audit_log (user_id, created_at)",
    # Платежи и чеки не удаляются вместе с пользователем: их хранят по закону о кассах
    """CREATE TABLE IF NOT EXISTS payments (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id),
        plan TEXT NOT NULL,
        amount_kopecks INTEGER NOT NULL CHECK (amount_kopecks > 0),
        currency TEXT NOT NULL DEFAULT 'RUB',
        status TEXT NOT NULL CHECK (status IN ('pending', 'paid', 'canceled', 'refunded')),
        provider TEXT,
        provider_payment_id TEXT UNIQUE,
        created_at TEXT NOT NULL,
        paid_at TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS receipts (
        id TEXT PRIMARY KEY,
        payment_id TEXT NOT NULL REFERENCES payments(id),
        kind TEXT NOT NULL CHECK (kind IN ('income', 'refund')),
        email TEXT NOT NULL,
        items TEXT NOT NULL,
        total_kopecks INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'failed')),
        provider_receipt_id TEXT,
        created_at TEXT NOT NULL
    )""",
]


TABLES = ["users", "user_data", "audit_log", "payments", "receipts", "email_codes"]

CODE_LIFETIME = timedelta(minutes=15)
CODE_ATTEMPTS = 5
CODE_RESEND_PAUSE = timedelta(seconds=60)


# Supabase Root 2021 CA, действует до 2031 года. Взят с сайта Supabase и сверен с их сервером
SUPABASE_CA = Path(__file__).resolve().parent.parent / "certs" / "supabase-root-2021.crt"
WRONG_SCHEME = "DATABASE_URL должен начинаться с postgresql:// или sqlite:///"
BROKEN_URL = "DATABASE_URL обрезан"


class AccountError(Exception):
    """Текст показывается пользователю как есть."""


@dataclass(frozen=True)
class User:
    id: str
    email: str
    plan: str
    plan_until: str | None
    verified: bool = False

    @property
    def is_pro(self) -> bool:
        return self.plan != "free" and bool(self.plan_until) and self.plan_until > _now()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=SCRYPT_N, r=SCRYPT_R, p=SCRYPT_P, dklen=SCRYPT_LEN
    )
    return f"scrypt${SCRYPT_N}${SCRYPT_R}${SCRYPT_P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
            dklen=len(bytes.fromhex(digest)),
        )  # fmt: skip
    except ValueError:
        return False
    return hmac.compare_digest(actual.hex(), digest)


# Проверка несуществующей почты занимает столько же времени: по задержке не понять, есть ли аккаунт
_DUMMY_HASH = hash_password(secrets.token_hex(8))


def _code_hash(user_id: str, code: str) -> str:
    return hashlib.sha256(f"{user_id}:{code}".encode("utf-8")).hexdigest()


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if len(email) > EMAIL_MAX or not EMAIL_RE.match(email):
        raise AccountError("Проверьте адрес почты: в нём должны быть @ и домен, например name@mail.ru")
    return email


def check_password_rules(password: str, email: str) -> None:
    if len(password) < PASSWORD_MIN:
        raise AccountError(f"Пароль слишком короткий: нужно не меньше {PASSWORD_MIN} символов")
    if len(password) > PASSWORD_MAX:
        raise AccountError(f"Пароль слишком длинный: не больше {PASSWORD_MAX} символов")
    if password.strip().lower() == email:
        raise AccountError("Пароль не должен совпадать с адресом почты")


def _ssl_context() -> ssl.SSLContext:
    """Системные корневые сертификаты плюс корневой Supabase: им подписан их сервер базы.

    Проверка сертификата и имени сервера не отключается, иначе соединение можно подменить.
    """
    context = ssl.create_default_context()
    context.load_verify_locations(SUPABASE_CA)
    return context


class AccountStore:
    """Хранилище аккаунтов. url: postgresql://... или sqlite:///путь."""

    def __init__(self, url: str):
        url = url.strip().strip("\"'<>").strip()  # частые следы копирования
        parsed = urlparse(url)
        scheme = parsed.scheme.split("+")[0]  # postgresql+psycopg2:// из вкладки ORM
        if parsed.scheme == "sqlite":
            self._path = url.removeprefix("sqlite:///")
            self._mark = "?"
            self._pg = None
        elif scheme in ("postgres", "postgresql"):
            try:
                parsed.port
            except ValueError:
                raise ValueError(BROKEN_URL) from None
            if not (parsed.hostname and parsed.username and parsed.password):
                raise ValueError(BROKEN_URL)
            self._pg = parsed
            self._mark = "%s"
        else:
            # Показывается всем посетителям, поэтому только приставка адреса, без начала значения
            found = f"начинается с «{parsed.scheme[:12]}://»" if "://" in url else "это не адрес: нет «://»"
            raise ValueError(f"{WRONG_SCHEME}. Сейчас {'значение пустое' if not url else found}")

    def _connect(self):
        if self._pg is None:
            conn = sqlite3.connect(self._path)
            conn.execute("PRAGMA foreign_keys = ON")
            return conn
        import pg8000.dbapi  # только на сайте: на компьютере драйвер может быть не установлен

        url = self._pg
        return pg8000.dbapi.connect(
            user=unquote(url.username or ""), password=unquote(url.password or ""),
            host=url.hostname, port=url.port or 5432, database=(url.path or "/postgres")[1:],
            ssl_context=_ssl_context(), timeout=20,
        )  # fmt: skip

    def _run(self, sql: str, params=(), fetch: str | None = None):
        """Одна операция в своей транзакции. sql пишется с ?, для Postgres меняется на %s."""
        sql = sql.replace("?", self._mark)
        with closing(self._connect()) as conn:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, params)
                result = cursor.fetchone() if fetch == "one" else cursor.fetchall() if fetch == "all" else None
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            return result

    def init_schema(self) -> None:
        for statement in SCHEMA:
            self._run(statement)
        try:  # колонка появилась после первых аккаунтов
            self._run("ALTER TABLE users ADD COLUMN email_verified INTEGER NOT NULL DEFAULT 0")
        except Exception as error:
            if "duplicate" not in str(error).lower() and "already exists" not in str(error).lower():
                raise
        if self._pg is not None:
            # Supabase открывает таблицы схемы public через свой API по публичному ключу.
            # Защита строк без правил закрывает этот доступ, приложение входит владельцем таблиц
            for table in TABLES:
                self._run(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")

    def audit(self, user_id: str | None, event: str, detail: str = "") -> None:
        self._run(
            "INSERT INTO audit_log (id, user_id, event, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (uuid.uuid4().hex, user_id, event, detail, _now()),
        )

    def _user(self, row) -> User:
        return User(row[0], row[1], row[2], row[3], bool(row[4]))

    def get_user(self, user_id: str) -> User | None:
        row = self._run(
            "SELECT id, email, plan, plan_until, email_verified FROM users WHERE id = ?", (user_id,), "one"
        )
        return self._user(row) if row else None

    def register(self, email: str, password: str) -> User:
        email = normalize_email(email)
        check_password_rules(password, email)
        if self._run("SELECT 1 FROM users WHERE email = ?", (email,), "one"):
            raise AccountError("Эта почта уже зарегистрирована. Войдите или укажите другую")
        user_id = uuid.uuid4().hex
        try:
            self._run(
                "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                (user_id, email, hash_password(password), _now()),
            )
        except Exception as error:  # одновременная регистрация той же почты
            if "unique" in str(error).lower():
                raise AccountError("Эта почта уже зарегистрирована. Войдите или укажите другую") from error
            raise
        self.audit(user_id, "register")
        log.info("Регистрация %s", mask_email(email))
        return User(user_id, email, "free", None, False)

    def authenticate(self, email: str, password: str) -> User:
        wrong = AccountError("Неверная почта или пароль")
        try:
            email = normalize_email(email)
        except AccountError:
            raise wrong from None
        row = self._run(
            "SELECT id, password_hash, failed_logins, locked_until FROM users WHERE email = ?", (email,), "one"
        )
        if row is None:
            verify_password(password[:PASSWORD_MAX], _DUMMY_HASH)
            log.info("Вход: нет аккаунта %s", mask_email(email))
            raise wrong
        user_id, stored, failed, locked_until = row
        if locked_until and locked_until > _now():
            self.audit(user_id, "login_locked")
            raise AccountError("Слишком много неудачных попыток. Попробуйте через 15 минут")
        if not verify_password(password[:PASSWORD_MAX], stored):
            failed += 1
            lock = (datetime.now(timezone.utc) + LOCK_TIME).isoformat(timespec="seconds")
            locked = failed >= MAX_FAILED_LOGINS
            self._run(
                "UPDATE users SET failed_logins = ?, locked_until = ? WHERE id = ?",
                (0 if locked else failed, lock if locked else None, user_id),
            )
            self.audit(user_id, "login_failed", "locked" if locked else "")
            log.warning("Неудачный вход %s%s", mask_email(email), ", аккаунт заблокирован" if locked else "")
            raise wrong
        self._run(
            "UPDATE users SET failed_logins = 0, locked_until = NULL, last_login_at = ? WHERE id = ?",
            (_now(), user_id),
        )
        self.audit(user_id, "login")
        log.info("Вход %s", mask_email(email))
        return self.get_user(user_id)

    # --- Подтверждение почты ---

    def issue_code(self, user_id: str) -> str:
        """Новый код из 6 цифр. Хранится только хеш, повторно отправить можно через минуту."""
        row = self._run("SELECT sent_at FROM email_codes WHERE user_id = ?", (user_id,), "one")
        now = datetime.now(timezone.utc)
        if row and datetime.fromisoformat(row[0]) + CODE_RESEND_PAUSE > now:
            raise AccountError("Код уже отправлен. Новый можно запросить через минуту")
        code = f"{secrets.randbelow(10**6):06d}"
        self._run(
            "INSERT INTO email_codes (user_id, code_hash, expires_at, attempts, sent_at) VALUES (?, ?, ?, 0, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET code_hash = excluded.code_hash, "
            "expires_at = excluded.expires_at, attempts = 0, sent_at = excluded.sent_at",
            (user_id, _code_hash(user_id, code), (now + CODE_LIFETIME).isoformat(timespec="seconds"),
             now.isoformat(timespec="seconds")),
        )  # fmt: skip
        self.audit(user_id, "code_sent")
        return code

    def confirm_email(self, user_id: str, code: str) -> User:
        row = self._run(
            "SELECT code_hash, expires_at, attempts FROM email_codes WHERE user_id = ?", (user_id,), "one"
        )
        if row is None or row[1] < _now():
            raise AccountError("Код устарел. Запросите новый")
        code_hash, _, attempts = row
        if attempts >= CODE_ATTEMPTS:
            raise AccountError("Слишком много неверных попыток. Запросите новый код")
        if not hmac.compare_digest(code_hash, _code_hash(user_id, code.strip())):
            self._run("UPDATE email_codes SET attempts = attempts + 1 WHERE user_id = ?", (user_id,))
            self.audit(user_id, "code_failed")
            raise AccountError("Неверный код. Проверьте письмо и введите 6 цифр")
        self._run("UPDATE users SET email_verified = 1 WHERE id = ?", (user_id,))
        self._run("DELETE FROM email_codes WHERE user_id = ?", (user_id,))
        self.audit(user_id, "email_confirmed")
        log.info("Почта подтверждена у пользователя %s", user_id)
        return self.get_user(user_id)

    def change_password(self, user_id: str, old: str, new: str) -> None:
        row = self._run("SELECT email, password_hash FROM users WHERE id = ?", (user_id,), "one")
        if row is None or not verify_password(old[:PASSWORD_MAX], row[1]):
            raise AccountError("Текущий пароль указан неверно")
        check_password_rules(new, row[0])
        self._run("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(new), user_id))
        self.audit(user_id, "password_changed")
        log.info("Смена пароля %s", mask_email(row[0]))

    def load_data(self, user_id: str) -> dict | None:
        row = self._run("SELECT data FROM user_data WHERE user_id = ?", (user_id,), "one")
        return json.loads(row[0]) if row else None

    def save_data(self, user_id: str, data: dict, data_hash: str) -> None:
        self._run(
            "INSERT INTO user_data (user_id, data, data_hash, updated_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (user_id) DO UPDATE SET data = excluded.data, data_hash = excluded.data_hash, "
            "updated_at = excluded.updated_at",
            (user_id, json.dumps(data, ensure_ascii=False), data_hash, _now()),
        )

    # --- Платежи и чеки ---

    def create_payment(self, user_id: str, plan_code: str, provider: str = "manual") -> str:
        plan = PLANS.get(plan_code)
        if plan is None:
            raise AccountError("Такого тарифа нет")
        payment_id = uuid.uuid4().hex
        self._run(
            "INSERT INTO payments (id, user_id, plan, amount_kopecks, status, provider, created_at) "
            "VALUES (?, ?, ?, ?, 'pending', ?, ?)",
            (payment_id, user_id, plan_code, plan.price_kopecks, provider, _now()),
        )
        self.audit(user_id, "payment_created", payment_id)
        return payment_id

    def mark_paid(self, payment_id: str, provider_payment_id: str) -> str:
        """Оплата подтверждена: тариф продлевается, создаётся чек прихода. Возвращает id чека."""
        row = self._run(
            "SELECT p.user_id, p.plan, p.amount_kopecks, p.status, u.email, u.plan_until "
            "FROM payments p JOIN users u ON u.id = p.user_id WHERE p.id = ?",
            (payment_id,), "one",
        )  # fmt: skip
        if row is None:
            raise AccountError("Платёж не найден")
        user_id, plan_code, amount, status, email, plan_until = row
        if status != "pending":
            raise AccountError("Платёж уже обработан")
        plan = PLANS[plan_code]
        start = max(datetime.now(timezone.utc), datetime.fromisoformat(plan_until)) if plan_until else datetime.now(timezone.utc)
        until = (start + timedelta(days=plan.days)).isoformat(timespec="seconds")
        self._run(
            "UPDATE payments SET status = 'paid', provider_payment_id = ?, paid_at = ? WHERE id = ?",
            (provider_payment_id, _now(), payment_id),
        )
        self._run("UPDATE users SET plan = ?, plan_until = ? WHERE id = ?", (plan_code, until, user_id))
        receipt_id = uuid.uuid4().hex
        items = [{"name": plan.title, "quantity": 1, "price_kopecks": amount, "vat": "none"}]
        self._run(
            "INSERT INTO receipts (id, payment_id, kind, email, items, total_kopecks, created_at) "
            "VALUES (?, ?, 'income', ?, ?, ?, ?)",
            (receipt_id, payment_id, email, json.dumps(items, ensure_ascii=False), amount, _now()),
        )
        self.audit(user_id, "payment_paid", payment_id)
        log.info("Оплата %s: тариф %s до %s", payment_id, plan_code, until)
        return receipt_id

    def payments_of(self, user_id: str) -> list[tuple]:
        return self._run(
            "SELECT id, plan, amount_kopecks, status, created_at, paid_at FROM payments "
            "WHERE user_id = ? ORDER BY created_at DESC",
            (user_id,), "all",
        )  # fmt: skip


def explain_store_error(error: Exception) -> str:
    """Понятная причина, почему хранилище не открылось. Секреты в текст не попадают."""
    text = str(error)
    lowered = text.lower()
    if "certificate" in lowered:  # ошибка сертификата тоже ValueError, поэтому проверяется первой
        return "Сертификат сервера базы не прошёл проверку. Нужна строка Session pooler из Supabase"
    if isinstance(error, ValueError) and "ipv6" in lowered:
        return (
            "Строка DATABASE_URL не разбирается. Проверьте, что [YOUR-PASSWORD] заменён паролем "
            "без квадратных скобок, а в пароле нет символов @ : / # ?"
        )
    if isinstance(error, ValueError) and text == BROKEN_URL:
        return (
            "Строка DATABASE_URL обрывается: в пароле, скорее всего, есть символ # ? / или пробел. "
            "Сбросьте пароль базы в Supabase, сгенерируйте новый из букв и цифр и вставьте его в строку"
        )
    if isinstance(error, ValueError):
        return (
            "DATABASE_URL должен начинаться с postgresql:// (строка Session pooler из Supabase). "
            "Проверьте, что значение в кавычках и перед ним нет лишних символов. "
            + text.removeprefix(WRONG_SCHEME).removeprefix(". ")
        ).strip()
    if "28p01" in lowered or "password authentication failed" in lowered:
        return "Supabase не принял пароль базы. Вставьте в строку пароль базы данных проекта или сбросьте его"
    if "tenant or user not found" in lowered:
        return "Supabase не нашёл пользователя. В строке Session pooler имя должно быть вида postgres.<id проекта>"
    if "getaddrinfo" in lowered or "name or service not known" in lowered or "nodename" in lowered:
        return "Сервер базы не найден. Проверьте адрес после @: он должен оканчиваться на pooler.supabase.com"
    if "timed out" in lowered or "unreachable" in lowered or "timeout" in lowered:
        return "Сервер базы не отвечает. Нужна строка Session pooler, Direct с Streamlit Cloud не работает"
    return f"Ошибка подключения к базе: {type(error).__name__}"

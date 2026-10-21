#!/usr/bin/env python3
"""
Видеохостинг фильмов в Telegram: бот-вход, Mini App, VIP-подписка за Telegram Stars.
Весь проект запускается одним файлом: python main.py

Бот: /start с текстом и кнопкой Mini App. Видео, отправленное админом боту, становится фильмом.
Mini App: каталог, просмотр в HLS с выбором качества, отзывы, VIP, админ-панель.

Роли: owner (OWNER_ID) > admin > editor > operator > user.
  owner    — всё, получает звёзды, возвраты, баланс бота.
  admin    — фильмы любые, выдаёт редакторов, VIP, видит пользователей.
  editor   — загружает фильмы, управляет только своими.
  operator — только тикеты поддержки: отвечает и передаёт тикет админам.
  user     — смотрит; VIP-фильмы открыты по подписке.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import io
import json
import logging
import mimetypes
import os
import pysubs2
import re
import shutil
import signal
import sqlite3
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.filters import CommandStart
from aiogram.types import (
    BotCommand,
    ChatMemberUpdated,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    PreCheckoutQuery,
    TelegramObject,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
STATIC_DIR = BASE_DIR / "static"
DEFAULT_OWNER_ID = 7745681987
DEFAULT_BACKGROUND = "https://i.pinimg.com/1200x/93/31/9d/93319d1600bd69fcb7db28fef7b4a7bb.jpg"
DONATE_OPTIONS = (50, 100, 250, 500)
LADDER = (240, 720)                                   # качества HLS: 240p и 720p (низкий исходник масштабируется вверх)
BITRATE_KBPS = {240: 400, 720: 2500}
AUDIO_KBPS = {240: 64}

# ---- жанры ----
# Жанров два: «Аниме» и «Мультфильмы». Админ выбирает жанр при загрузке каждого видео
# и каждой серии. Показывается на карточке в каталоге и рядом с названием на странице фильма.
GENRES = ("anime", "cartoon")
GENRE_TITLES = {"anime": "Аниме", "cartoon": "Мультфильмы"}
DEFAULT_GENRE = "anime"

# ---- VIP ----
VIP_FOREVER = "9999-12-31 23:59:59"       # метка «навсегда»: дата далеко в будущем
REVIEW_LIMIT = 1                          # отзывов на фильм без VIP
REVIEW_LIMIT_VIP = 2                      # отзывов на фильм с VIP
GRADIENT_RE = re.compile(r"^(linear|radial|conic)-gradient\((?:[^;{}<>\\\"']){0,300}\)$")
COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$")
BOT_UPLOAD_LIMIT = 20 * 1024 * 1024             # лимит Bot API на скачивание файла (cloud API)
UPLOAD_LIMIT = 2 * 1024 ** 3                    # максимум файла при загрузке через Mini App: 2 ГБ
CHUNK_SIZE = 2 * 1024 * 1024                    # части по 2 МБ: на медленной отдаче часть успевает
                                                # долететь раньше, чем прокси закроет долгий запрос (60 с)
VIP_MONTH_DAYS, VIP_YEAR_DAYS = 30, 365
VIEW_MIN_SEC = 300                            # просмотр засчитывается после 5 минут, а не в самом конце

# ---- тикеты поддержки ----
TICKET_KINDS = ("bug", "idea", "anime", "cartoon")
TICKET_TITLES = {"bug": "Баг", "idea": "Идея", "anime": "Аниме", "cartoon": "Мультфильм"}
TICKET_OPERATOR, TICKET_STAFF = "operator", "staff"   # у кого сейчас тикет: у операторов или у админов
TICKET_ANSWER = ("Ваш тикет успешно отправлен, ожидайте рассмотрения. "
                 "Обычно это занимает около 5-10 минут.")
TICKET_CLOSED, TICKET_TTL = "closed", 600   # закрытый тикет удаляется из базы через 10 минут
TICKET_IMAGE_MAX = 10 * 1024 * 1024         # 10 МБ на картинку к тикету хватает
# картинку к тикету присылают в base64: внутри Telegram WebView multipart-загрузка
# доходит не всегда, а base64 уходит тем же JSON-запросом, что и текст сообщения
IMAGE_MAGIC = ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"),
               (b"GIF87a", ".gif"), (b"GIF89a", ".gif"), (b"BM", ".bmp"))
TOUCH_EVERY = 60                            # как часто обновлять last_seen: запись в базу на каждый запрос дорога
FFMPEG_NICE = 10                            # обработка видео уступает сайт и просмотр
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
YOUTUBE_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})")

log = logging.getLogger("film-hub")


# ----------------------------------------------------------------------------
# Мелкие помощники (жанры, «навсегда», градиенты)
# ----------------------------------------------------------------------------
def clean_genre(value: Any, default: str | None = DEFAULT_GENRE) -> str | None:
    """Приводит значение к одному из двух жанров. Пустое или неизвестное — default."""
    raw = str(value or "").strip().lower()
    if not raw:
        return default
    return raw if raw in GENRES else default


def is_forever(value: str | None) -> bool:
    """VIP выдан навсегда (метка — дата 9999 года)."""
    return bool(value) and str(value).startswith("9999")


def clean_gradient(value: Any) -> tuple[bool, str | None]:
    """Проверяет оформление ника VIP: (подошло, значение). Разрешены цвет #rrggbb и CSS-градиент.
    Пустая строка — сброс оформления (подошло, None)."""
    raw = re.sub(r"\s+", " ", str(value or "").strip())[:300]
    if not raw:
        return True, None
    if COLOR_RE.match(raw) or GRADIENT_RE.match(raw):
        return True, raw
    return False, None


# ============================================================================
# Конфигурация (только переменные окружения)
# ============================================================================
@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    initial_admins: frozenset[int]
    mode: str
    public_url: str | None
    port: int
    webhook_path: str
    webhook_secret: str
    db_path: str
    background_url: str
    vip_month: int
    vip_year: int

    @property
    def webapp_url(self) -> str | None:
        return f"{self.public_url}/app" if self.public_url else None

    @property
    def media_dir(self) -> Path:
        return Path(self.db_path).parent / "media"


def _parse_ids(raw: str, name: str) -> set[int]:
    ids: set[int] = set()
    for part in re.split(r"[\s,;]+", raw.strip()):
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(f"{name}: некорректный Telegram ID «{part}»")
        ids.add(int(part))
    return ids


def _price(env: Any, name: str, default: int) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    if not raw.isdigit() or int(raw) <= 0:
        raise SystemExit(f"{name} должен быть целым числом больше 0")
    return int(raw)


def load_config(env: Any = os.environ) -> Config:
    token = (env.get("BOT_TOKEN") or "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан: добавьте его в переменные окружения")

    owner_ids = _parse_ids((env.get("OWNER_ID") or "").strip() or str(DEFAULT_OWNER_ID), "OWNER_ID")
    if len(owner_ids) != 1:
        raise SystemExit("OWNER_ID должен содержать ровно один Telegram ID")

    mode = (env.get("BOT_MODE") or "webhook").strip().lower()
    if mode not in ("webhook", "polling"):
        raise SystemExit('BOT_MODE должен быть "webhook" или "polling"')

    public_url = (env.get("PUBLIC_URL") or "").strip().rstrip("/") or None
    if mode == "webhook":
        if not public_url:
            raise SystemExit("PUBLIC_URL обязателен в режиме webhook")
        if not public_url.startswith("https://"):
            raise SystemExit("PUBLIC_URL должен начинаться с https://")

    webhook_path = (env.get("WEBHOOK_PATH") or "/webhook").strip()
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path

    secret = (env.get("WEBHOOK_SECRET") or "").strip() or hashlib.sha256(
        f"webhook:{token}".encode()
    ).hexdigest()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise SystemExit("WEBHOOK_SECRET: допустимы только латиница, цифры, _ и -")

    return Config(
        bot_token=token,
        owner_id=next(iter(owner_ids)),
        initial_admins=frozenset(_parse_ids(env.get("ADMIN_IDS") or "", "ADMIN_IDS")),
        mode=mode,
        public_url=public_url,
        port=int(env.get("PORT") or 3000),
        webhook_path=webhook_path,
        webhook_secret=secret,
        db_path=(env.get("DB_PATH") or "").strip() or str(BASE_DIR / "data" / "film.db"),
        background_url=(env.get("BACKGROUND_URL") or "").strip() or DEFAULT_BACKGROUND,
        vip_month=_price(env, "VIP_MONTH_PRICE", 199),
        vip_year=_price(env, "VIP_YEAR_PRICE", 900),
    )


# ============================================================================
# Роли и права
# ============================================================================
OWNER, ADMIN, EDITOR, OPERATOR, USER = "owner", "admin", "editor", "operator", "user"
RANK = {USER: 0, OPERATOR: 1, EDITOR: 2, ADMIN: 3, OWNER: 4}
ROLE_TITLES = {OWNER: "Владелец", ADMIN: "Админ", EDITOR: "Редактор",
               OPERATOR: "Оператор", USER: "Пользователь"}


def at_least(role: str, minimum: str) -> bool:
    return RANK.get(role, 0) >= RANK[minimum]


def can_change_role(actor: str, target: str, new: str) -> bool:
    """Назначать и снимать можно только роли строго ниже своей. Владелец неприкосновенен."""
    if target == OWNER or new == OWNER:
        return False
    return RANK[actor] > RANK[target] and RANK[actor] > RANK[new]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


# ============================================================================
# База данных (SQLite, стандартный модуль)
# ============================================================================
TABLES = """
CREATE TABLE IF NOT EXISTS films (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    title         TEXT    NOT NULL,
    kind          TEXT    NOT NULL CHECK (kind IN ('link', 'video', 'series')),
    genre         TEXT    NOT NULL DEFAULT 'anime',
    url           TEXT,
    video_file_id TEXT,
    photo_file_id TEXT,
    owner_id      INTEGER,
    description   TEXT    NOT NULL DEFAULT '',
    release_date  TEXT    NOT NULL DEFAULT '',
    creator       TEXT    NOT NULL DEFAULT '',
    poster        TEXT,
    status        TEXT    NOT NULL DEFAULT 'ready',
    error         TEXT,
    duration      INTEGER NOT NULL DEFAULT 0,
    width         INTEGER NOT NULL DEFAULT 0,
    height        INTEGER NOT NULL DEFAULT 0,
    is_vip        INTEGER NOT NULL DEFAULT 0,
    is_active     INTEGER NOT NULL DEFAULT 1,
    views         INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    role        TEXT    NOT NULL DEFAULT 'user',
    vip_until   TEXT,
    vip_forever INTEGER NOT NULL DEFAULT 0,
    stars       INTEGER NOT NULL DEFAULT 0,
    name_gradient TEXT,
    added_by    INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- уникальности (film_id, user_id) нет: с VIP на фильм можно оставить два отзыва
CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS sales (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL,
    amount     INTEGER NOT NULL CHECK (amount > 0),
    charge_id  TEXT    UNIQUE,
    refunded   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS watch (
    user_id    INTEGER NOT NULL,
    film_id    INTEGER NOT NULL,
    position   REAL    NOT NULL DEFAULT 0,
    duration   REAL    NOT NULL DEFAULT 0,
    updated_at TEXT    NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, film_id)
);

CREATE TABLE IF NOT EXISTS ads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    start_sec  INTEGER NOT NULL DEFAULT 0,
    link       TEXT    NOT NULL DEFAULT '',
    ext        TEXT    NOT NULL DEFAULT '.mp4',
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- избранное: что пользователь отложил посмотреть позже
CREATE TABLE IF NOT EXISTS favorites (
    user_id    INTEGER NOT NULL,
    film_id    INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, film_id)
);

-- тикеты поддержки: assignee — у кого тикет сейчас (операторы или админы)
CREATE TABLE IF NOT EXISTS tickets (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL,
    kind       TEXT    NOT NULL CHECK (kind IN ('bug', 'idea', 'anime', 'cartoon')),
    text       TEXT    NOT NULL,
    assignee   TEXT    NOT NULL DEFAULT 'operator' CHECK (assignee IN ('operator', 'staff')),
    status     TEXT    NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'closed')),
    closed_at  TEXT,
    created_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- переписка внутри тикета; удаляется вместе с тикетом (ON DELETE CASCADE)
CREATE TABLE IF NOT EXISTS ticket_messages (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ticket_id   INTEGER NOT NULL REFERENCES tickets (id) ON DELETE CASCADE,
    author_id   INTEGER NOT NULL,
    author_role TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    image       TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_films_active ON films (is_active, id);
CREATE INDEX IF NOT EXISTS idx_films_owner ON films (owner_id);
CREATE INDEX IF NOT EXISTS idx_reviews_film ON reviews (film_id);
CREATE INDEX IF NOT EXISTS idx_sales_refund ON sales (refunded, id);
CREATE INDEX IF NOT EXISTS idx_tickets_assignee ON tickets (assignee, id);
CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets (user_id, id);
CREATE INDEX IF NOT EXISTS idx_ticket_messages ON ticket_messages (ticket_id, id);
CREATE INDEX IF NOT EXISTS idx_watch_user ON watch (user_id);
CREATE INDEX IF NOT EXISTS idx_favorites_user ON favorites (user_id);
CREATE INDEX IF NOT EXISTS idx_tickets_closed ON tickets (status, closed_at);
"""

# колонки, которых могло не быть в ранних версиях
FILM_COLUMNS = {
    "series_id": "INTEGER",
    "season": "INTEGER NOT NULL DEFAULT 0",
    "episode": "INTEGER NOT NULL DEFAULT 0",
    "owner_id": "INTEGER",
    "description": "TEXT NOT NULL DEFAULT ''",
    "release_date": "TEXT NOT NULL DEFAULT ''",
    "creator": "TEXT NOT NULL DEFAULT ''",
    "country": "TEXT NOT NULL DEFAULT ''",
    "poster": "TEXT",
    "status": "TEXT NOT NULL DEFAULT 'ready'",
    "error": "TEXT",
    "duration": "INTEGER NOT NULL DEFAULT 0",
    "width": "INTEGER NOT NULL DEFAULT 0",
    "height": "INTEGER NOT NULL DEFAULT 0",
    "is_vip": "INTEGER NOT NULL DEFAULT 0",
    "is_active": "INTEGER NOT NULL DEFAULT 1",
    "views": "INTEGER NOT NULL DEFAULT 0",
    "photo_file_id": "TEXT",
    "video_file_id": "TEXT",
    "url": "TEXT",
    "genre": "TEXT NOT NULL DEFAULT 'anime'",
}

# колонки watch, которых могло не быть раньше
WATCH_COLUMNS = {
    "counted": "INTEGER NOT NULL DEFAULT 0",   # просмотр уже засчитан (один раз на фильм)
}

# колонки tickets/ticket_messages, которых могло не быть раньше
TICKET_COLUMNS = {"status": "TEXT NOT NULL DEFAULT 'open'", "closed_at": "TEXT"}
MESSAGE_COLUMNS = {"image": "TEXT"}

# колонки users, которых могло не быть раньше
USER_COLUMNS = {
    "banned": "INTEGER NOT NULL DEFAULT 0",
    "tour_seen": "INTEGER NOT NULL DEFAULT 0",   # подсказки по кнопкам показаны — больше не показывать
    "vip_forever": "INTEGER NOT NULL DEFAULT 0",
    "stars": "INTEGER NOT NULL DEFAULT 0",
    "name_gradient": "TEXT",
}


def sniff_image(data: bytes) -> str | None:
    """Расширение картинки по её первым байтам. None — это не картинка: тип узнаём по
    содержимому, а не по имени файла, поэтому подсунуть вместо картинки файл нельзя."""
    for magic, ext in IMAGE_MAGIC:
        if data.startswith(magic):
            return ext
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    if data[4:8] == b"ftyp":                      # HEIC и AVIF прямо с телефона
        return ".heic" if data[8:12] in (b"heic", b"heix", b"mif1", b"msf1") else ".avif"
    head = data[:300].lstrip().lower()
    if head.startswith(b"<svg") or (head.startswith(b"<?xml") and b"<svg" in data[:4096].lower()):
        return ".svg"
    return None


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def close(self) -> None:
        self.conn.close()

    # ---- миграции: данные не удаляются, несовместимые таблицы переименовываются ----
    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _table_sql(self, table: str) -> str:
        row = self.conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        return row["sql"] if row and row["sql"] else ""

    def _migrate(self) -> None:
        stamp = int(time.time())
        # 1. старая таблица payments (подписки/прежние платежи) — сохраняем под другим именем
        legacy = self._columns("payments")
        if legacy and "kind" not in legacy:
            self.conn.execute(f"ALTER TABLE payments RENAME TO payments_legacy_{stamp}")
        # 2. старая films с CHECK на video_file_id не даёт загружать фильмы через Mini App — пересоздаём
        old_films_sql = self._table_sql("films")
        flat_films_sql = re.sub(r"\s+", "", old_films_sql)   # пробелы в CHECK могут отличаться
        if flat_films_sql and ("video_file_idISNOTNULL" in flat_films_sql
                               or "kindIN('link','video')" in flat_films_sql):
            old_cols = self._columns("films")
            self.conn.execute("DROP INDEX IF EXISTS idx_films_active")
            self.conn.execute(f"ALTER TABLE films RENAME TO films_old_{stamp}")
            self.conn.executescript(TABLES)
            new_cols = self._columns("films")
            common = [c for c in old_cols & new_cols]
            cols = ", ".join(common)
            self.conn.execute(f"INSERT INTO films ({cols}) SELECT {cols} FROM films_old_{stamp}")
            if "price" in old_cols:  # платные по одной цене становятся VIP-фильмами
                self.conn.execute(
                    f"UPDATE films SET is_vip = 1 WHERE id IN (SELECT id FROM films_old_{stamp} WHERE price > 0)"
                )
            log.warning("таблица films пересоздана, старые данные в films_old_%s", stamp)

        # 3. старая таблица отзывов с UNIQUE (film_id, user_id) не даёт оставить второй отзыв — пересоздаём
        old_reviews_sql = self._table_sql("reviews")
        if old_reviews_sql and "UNIQUE" in old_reviews_sql.upper():
            self.conn.execute("DROP INDEX IF EXISTS idx_reviews_film")
            self.conn.execute(f"ALTER TABLE reviews RENAME TO reviews_old_{stamp}")
            self.conn.executescript(TABLES)
            self.conn.execute(
                "INSERT INTO reviews (id, film_id, user_id, rating, text, created_at) "
                f"SELECT id, film_id, user_id, rating, text, created_at FROM reviews_old_{stamp}"
            )
            log.warning("таблица reviews пересоздана, старые данные в reviews_old_%s", stamp)

        self.conn.executescript(TABLES)
        for column, ddl in FILM_COLUMNS.items():
            if column not in self._columns("films"):
                self.conn.execute(f"ALTER TABLE films ADD COLUMN {column} {ddl}")
        if "vip_until" not in self._columns("users"):
            self.conn.execute("ALTER TABLE users ADD COLUMN vip_until TEXT")
        for column, ddl in USER_COLUMNS.items():
            if column not in self._columns("users"):
                self.conn.execute(f"ALTER TABLE users ADD COLUMN {column} {ddl}")
        for column, ddl in WATCH_COLUMNS.items():
            if column not in self._columns("watch"):
                self.conn.execute(f"ALTER TABLE watch ADD COLUMN {column} {ddl}")
        for table, columns in (("tickets", TICKET_COLUMNS), ("ticket_messages", MESSAGE_COLUMNS)):
            have = self._columns(table)
            for column, ddl in columns.items():
                if column not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
        # у тех, у кого VIP помечен «навсегда», дата уже стоит far-future — синхронизируем флаг
        self.conn.execute("UPDATE users SET vip_forever = 1 WHERE vip_until LIKE '9999%'")
        # жанров осталось два: старые значения («Фильмы», «Сериалы») приводим к «Аниме»,
        # нужный жанр админ выбирает в редакторе фильма
        self.conn.execute("UPDATE films SET genre = 'anime' WHERE genre IS NULL OR genre NOT IN ('anime','cartoon')")
        # сезоны убраны: старые серии сводим в один плоский список
        self.conn.execute("UPDATE films SET season = 1 WHERE season <> 1")
        self.conn.executescript(INDEXES)

    # ---- пользователи ----
    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def touch_user(self, tg_id: int, username: str | None, first_name: str | None) -> None:
        """Регистрирует пользователя и обновляет last_seen не чаще раза в минуту.

        Плеер во время просмотра шлёт запрос каждые несколько секунд, а каждая запись
        в SQLite — это транзакция и обращение к диску. На хостинге с 1 ГБ RAM
        запись на каждый запрос и создавала лишнюю нагрузку.
        """
        row = self._one("SELECT username, first_name, last_seen FROM users WHERE tg_id = ?", (tg_id,))
        if row is None:
            self.conn.execute("INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)",
                              (tg_id, username, first_name))
            return
        last = parse_dt(row["last_seen"])
        fresh = bool(last and (utcnow() - last).total_seconds() < TOUCH_EVERY)
        same = (not username or row["username"] == username) and (not first_name or row["first_name"] == first_name)
        if fresh and same:
            return
        self.conn.execute(
            """UPDATE users SET username = COALESCE(?, username),
                 first_name = COALESCE(?, first_name), last_seen = datetime('now')
               WHERE tg_id = ?""",
            (username, first_name, tg_id),
        )

    # ---- бан ----
    def set_banned(self, tg_id: int, banned: bool) -> None:
        self.touch_user(tg_id, None, None)
        self.conn.execute("UPDATE users SET banned = ? WHERE tg_id = ?", (1 if banned else 0, tg_id))

    def set_tour_seen(self, tg_id: int) -> None:
        """Подсказки по интерфейсу показаны: запоминаем, чтобы не показывать их снова."""
        self.conn.execute("UPDATE users SET tour_seen = 1 WHERE tg_id = ?", (tg_id,))

    def get_user(self, tg_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    def get_role(self, tg_id: int) -> str:
        row = self.get_user(tg_id)
        return row["role"] if row and row["role"] in RANK else USER

    def set_role(self, tg_id: int, role: str, added_by: int | None) -> None:
        self.conn.execute(
            """INSERT INTO users (tg_id, role, added_by) VALUES (?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role, added_by = excluded.added_by""",
            (tg_id, role, added_by),
        )

    def search_users(self, q: str, limit: int = 30) -> list[dict]:
        if q:
            return self._all(
                """SELECT * FROM users WHERE CAST(tg_id AS TEXT) LIKE ?
                   OR first_name LIKE ? OR username LIKE ? ORDER BY last_seen DESC LIMIT ?""",
                (f"%{q}%", f"%{q}%", f"%{q}%", limit),
            )
        return self._all("SELECT * FROM users ORDER BY last_seen DESC LIMIT ?", (limit,))

    def grant_vip(self, tg_id: int, days: int) -> datetime:
        """Продлевает VIP на days дней; отрицательное значение сокращает срок, но не ниже текущего момента."""
        self.touch_user(tg_id, None, None)
        now = utcnow()
        row = self.get_user(tg_id) or {}
        forever = is_forever(row.get("vip_until")) or bool(row.get("vip_forever"))
        if forever:
            # бессрочный VIP не продлевается и не режется отрезками: снять его можно только revoke_vip()
            return parse_dt(VIP_FOREVER) or now
        current = parse_dt(row.get("vip_until")) or now
        new_until = max(current, now) + timedelta(days=days)
        new_until = max(new_until, now)
        new_until = min(new_until, datetime(9998, 12, 30, tzinfo=timezone.utc))  # страховка от переполнения даты
        self.conn.execute(
            "UPDATE users SET vip_until = ?, vip_forever = 0 WHERE tg_id = ?", (fmt_dt(new_until), tg_id)
        )
        return new_until

    def grant_vip_forever(self, tg_id: int) -> None:
        """VIP без срока: в базе метка 9999 года, в интерфейсе — переливающийся символ ∞."""
        self.touch_user(tg_id, None, None)
        self.conn.execute(
            "UPDATE users SET vip_until = ?, vip_forever = 1 WHERE tg_id = ?", (VIP_FOREVER, tg_id)
        )

    def revoke_vip(self, tg_id: int) -> None:
        """Полностью снимает VIP (и бессрочный тоже)."""
        self.touch_user(tg_id, None, None)
        self.conn.execute("UPDATE users SET vip_until = NULL, vip_forever = 0 WHERE tg_id = ?", (tg_id,))

    # ---- звёзды на внутреннем балансе ----
    def star_balance(self, tg_id: int) -> int:
        row = self.get_user(tg_id) or {}
        return max(0, int(row.get("stars") or 0))

    def add_stars(self, tg_id: int, amount: int) -> int:
        """Начисляет (или списывает при минусе) звёзды. Возвращает новый баланс, он не уходит в минус."""
        self.touch_user(tg_id, None, None)
        balance = max(0, self.star_balance(tg_id) + int(amount))
        self.conn.execute("UPDATE users SET stars = ? WHERE tg_id = ?", (balance, tg_id))
        return balance

    def spend_stars(self, tg_id: int, amount: int) -> bool:
        """Списывает звёзды, если их хватает."""
        if amount <= 0 or self.star_balance(tg_id) < amount:
            return False
        self.conn.execute(
            "UPDATE users SET stars = stars - ? WHERE tg_id = ? AND stars >= ?", (amount, tg_id, amount)
        )
        return self.conn.execute("SELECT changes() AS n").fetchone()["n"] > 0

    def set_name_gradient(self, tg_id: int, gradient: str | None) -> None:
        self.touch_user(tg_id, None, None)
        self.conn.execute("UPDATE users SET name_gradient = ? WHERE tg_id = ?", (gradient, tg_id))

    def display_name(self, tg_id: int) -> str:
        u = self.get_user(tg_id) or {}
        return u.get("first_name") or (f"@{u['username']}" if u.get("username") else "Пользователь")

    # ---- фильмы ----
    def create_film(self, **fields: Any) -> int:
        keys = list(fields)
        sql = f"INSERT INTO films ({', '.join(keys)}) VALUES ({', '.join('?' for _ in keys)})"
        return int(self.conn.execute(sql, tuple(fields[k] for k in keys)).lastrowid)

    def get_film(self, film_id: int) -> dict | None:
        return self._one("SELECT * FROM films WHERE id = ?", (film_id,))

    def update_film(self, film_id: int, **fields: Any) -> None:
        allowed = {"title", "description", "release_date", "creator", "is_vip", "is_active",
                   "url", "status", "error", "duration", "width", "height", "poster", "country", "genre"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        sets = ", ".join(f"{k} = ?" for k, _ in items)
        self.conn.execute(f"UPDATE films SET {sets} WHERE id = ?", (*[v for _, v in items], film_id))

    def set_status(self, film_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE films SET status = ?, error = ? WHERE id = ?", (status, error, film_id))

    def delete_film(self, film_id: int) -> None:
        self.conn.execute("DELETE FROM reviews WHERE film_id = ?", (film_id,))
        self.conn.execute("DELETE FROM watch WHERE film_id = ?", (film_id,))
        self.conn.execute("DELETE FROM films WHERE id = ?", (film_id,))

    # ---- обслуживание базы ----
    def film_ids(self) -> set[int]:
        return {r["id"] for r in self._all("SELECT id FROM films")}

    def prune_watch(self, keep_per_user: int) -> int:
        """Удаляет строки просмотров удалённых фильмов и обрезает историю до keep_per_user на пользователя."""
        cur = self.conn.execute("DELETE FROM watch WHERE film_id NOT IN (SELECT id FROM films)")
        removed = cur.rowcount
        cur = self.conn.execute(
            """DELETE FROM watch WHERE rowid IN (
                 SELECT rowid FROM (
                   SELECT rowid, ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY updated_at DESC) AS rn FROM watch
                 ) WHERE rn > ?)""", (keep_per_user,))
        return removed + cur.rowcount

    def vacuum(self) -> None:
        self.conn.execute("VACUUM")

    def list_films(self, *, public: bool = True, owner_id: int | None = None,
                   limit: int = 500) -> list[dict]:
        where, params = ["series_id IS NULL"], []
        if public:
            where.append("is_active = 1 AND status = 'ready'")
        if owner_id is not None:
            where.append("owner_id = ?")
            params.append(owner_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return self._all(f"SELECT * FROM films{clause} ORDER BY id DESC LIMIT ?", (*params, limit))

    def episodes(self, series_id: int) -> list[dict]:
        return self._all("SELECT * FROM films WHERE series_id = ? ORDER BY episode, id", (series_id,))

    def episode_exists(self, series_id: int, episode: int) -> bool:
        return self._one("SELECT id FROM films WHERE series_id = ? AND episode = ?",
                         (series_id, episode)) is not None

    def list_video_films(self) -> list[dict]:
        return self._all("SELECT * FROM films WHERE kind = 'video' ORDER BY id")

    def add_view(self, film_id: int) -> None:
        self.conn.execute("UPDATE films SET views = views + 1 WHERE id = ?", (film_id,))

    def register_view(self, user_id: int, film_id: int, position: float, duration: float) -> bool:
        """Засчитывает просмотр, когда зритель посмотрел 5 минут (VIEW_MIN_SEC).

        Досматривать до конца не нужно: в конце титры и субтитры, их смотрят не все.
        Если сам ролик короче пяти минут, достаточно досмотреть почти до конца.
        Один пользователь даёт фильму один просмотр — повторный запуск счётчик не крутит.
        """
        row = self._one("SELECT counted FROM watch WHERE user_id = ? AND film_id = ?", (user_id, film_id))
        if row and row["counted"]:
            return False
        need = VIEW_MIN_SEC if duration <= 0 else min(VIEW_MIN_SEC, max(20.0, duration * 0.9))
        if position < need:
            return False
        self.conn.execute(
            """INSERT INTO watch (user_id, film_id, position, duration, counted) VALUES (?, ?, ?, ?, 1)
               ON CONFLICT(user_id, film_id) DO UPDATE SET counted = 1""",
            (user_id, film_id, max(0.0, position), max(0.0, duration)),
        )
        self.add_view(film_id)
        return True

    # ---- отзывы ----
    def count_user_reviews(self, film_id: int, user_id: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM reviews WHERE film_id = ? AND user_id = ?",
                         (film_id, user_id))["n"]

    def add_review(self, film_id: int, user_id: int, rating: int, text: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO reviews (film_id, user_id, rating, text) VALUES (?, ?, ?, ?)",
            (film_id, user_id, rating, text),
        )
        return int(cur.lastrowid)

    def update_review(self, review_id: int, rating: int, text: str) -> None:
        self.conn.execute(
            "UPDATE reviews SET rating = ?, text = ?, created_at = datetime('now') WHERE id = ?",
            (rating, text, review_id),
        )

    def reviews(self, film_id: int, owner_id: int, limit: int = 300) -> list[dict]:
        """Отзывы фильма: сначала отзывы с VIP (их видно сразу), потом обычные.
        Внутри каждой группы — новые сверху. Порядок совпадает с золотой припиской VIP."""
        return self._all(
            """SELECT r.*, u.role AS u_role, u.vip_until AS u_vip_until, u.vip_forever AS u_vip_forever
               FROM reviews r LEFT JOIN users u ON u.tg_id = r.user_id
               WHERE r.film_id = ?
               ORDER BY CASE WHEN u.tg_id = ? OR u.role IN ('admin', 'owner')
                                  OR COALESCE(u.vip_forever, 0) = 1
                                  OR (u.vip_until IS NOT NULL AND u.vip_until > ?)
                             THEN 0 ELSE 1 END,
                        r.id DESC
               LIMIT ?""",
            (film_id, owner_id, fmt_dt(utcnow()), limit),
        )

    def rating(self, film_id: int) -> tuple[float, int]:
        row = self._one("SELECT AVG(rating) AS avg, COUNT(*) AS n FROM reviews WHERE film_id = ?", (film_id,))
        return (round(row["avg"] or 0, 1), row["n"]) if row else (0.0, 0)

    def get_review(self, review_id: int) -> dict | None:
        return self._one("SELECT * FROM reviews WHERE id = ?", (review_id,))

    def delete_review(self, review_id: int) -> None:
        self.conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))

    # ---- избранное ----
    def favorites(self, user_id: int) -> list[int]:
        return [r["film_id"] for r in self._all(
            "SELECT film_id FROM favorites WHERE user_id = ? ORDER BY created_at DESC, film_id DESC",
            (user_id,))]

    def toggle_favorite(self, user_id: int, film_id: int) -> bool:
        """Кладёт в избранное или убирает из него. Возвращает True, если теперь в избранном."""
        row = self._one("SELECT film_id FROM favorites WHERE user_id = ? AND film_id = ?", (user_id, film_id))
        if row:
            self.conn.execute("DELETE FROM favorites WHERE user_id = ? AND film_id = ?", (user_id, film_id))
            return False
        self.conn.execute("INSERT INTO favorites (user_id, film_id) VALUES (?, ?)", (user_id, film_id))
        return True

    # ---- тикеты поддержки ----
    def add_ticket(self, user_id: int, kind: str, text: str) -> int:
        self.touch_user(user_id, None, None)
        cur = self.conn.execute("INSERT INTO tickets (user_id, kind, text) VALUES (?, ?, ?)",
                                (user_id, kind, text))
        return int(cur.lastrowid)

    def get_ticket(self, ticket_id: int) -> dict | None:
        return self._one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    def tickets(self, user_id: int | None = None, assignee: str | None = None,
                limit: int = 200) -> list[dict]:
        """Список тикетов: свои (user_id), у операторов или у админов (assignee), либо все."""
        where, params = [], []
        if user_id is not None:
            where.append("user_id = ?")
            params.append(user_id)
        if assignee is not None:
            where.append("assignee = ?")
            params.append(assignee)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        return self._all(f"SELECT * FROM tickets{clause} ORDER BY id DESC LIMIT ?", (*params, limit))

    def ticket_queue(self) -> dict[int, int]:
        """Номер в очереди: порядковый номер среди тикетов, которые ждут у операторов (старые первыми)."""
        rows = self._all("SELECT id FROM tickets WHERE assignee = 'operator' ORDER BY id")
        return {r["id"]: i + 1 for i, r in enumerate(rows)}

    def ticket_messages(self, ticket_id: int) -> list[dict]:
        return self._all("SELECT * FROM ticket_messages WHERE ticket_id = ? ORDER BY id", (ticket_id,))

    def add_ticket_message(self, ticket_id: int, author_id: int, role: str, text: str,
                           image: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO ticket_messages (ticket_id, author_id, author_role, text, image) VALUES (?, ?, ?, ?, ?)",
            (ticket_id, author_id, role, text, image),
        )
        return int(cur.lastrowid)

    def close_ticket(self, ticket_id: int) -> None:
        self.conn.execute("UPDATE tickets SET status = 'closed', closed_at = datetime('now') WHERE id = ?",
                          (ticket_id,))

    def drop_expired_tickets(self, ttl: int = TICKET_TTL) -> list[int]:
        """Закрытые тикеты удаляются из базы через ttl секунд — вместе с перепиской и картинками."""
        rows = self._all(
            "SELECT id FROM tickets WHERE status = 'closed' AND closed_at IS NOT NULL "
            "AND closed_at <= datetime('now', ?)", (f"-{int(ttl)} seconds",))
        for row in rows:
            self.delete_ticket(row["id"])
        return [r["id"] for r in rows]

    def get_ticket_message(self, message_id: int) -> dict | None:
        return self._one("SELECT * FROM ticket_messages WHERE id = ?", (message_id,))

    def set_ticket_assignee(self, ticket_id: int, assignee: str) -> None:
        self.conn.execute("UPDATE tickets SET assignee = ? WHERE id = ?", (assignee, ticket_id))

    def delete_ticket(self, ticket_id: int) -> None:
        """Удаляет тикет вместе с перепиской из базы (внешний ключ с ON DELETE CASCADE)."""
        self.conn.execute("DELETE FROM ticket_messages WHERE ticket_id = ?", (ticket_id,))
        self.conn.execute("DELETE FROM tickets WHERE id = ?", (ticket_id,))

    def delete_user_tickets(self, user_id: int) -> int:
        """Пользователь ушёл из бота — его тикеты удаляются сразу и полностью."""
        rows = self._all("SELECT id FROM tickets WHERE user_id = ?", (user_id,))
        for row in rows:
            self.delete_ticket(row["id"])
        return len(rows)

    def staff_ids(self, minimum: str = ADMIN) -> list[int]:
        """Кому передавать тикет: владелец плюс все, у кого роль не ниже minimum."""
        rows = self._all("SELECT tg_id, role FROM users")
        return [r["tg_id"] for r in rows if at_least(r["role"], minimum)]

    # ---- просмотры, прогресс, настройки ----
    def touch_watch(self, user_id: int, film_id: int) -> None:
        self.conn.execute(
            """INSERT INTO watch (user_id, film_id) VALUES (?, ?)
               ON CONFLICT(user_id, film_id) DO UPDATE SET updated_at = datetime('now')""",
            (user_id, film_id),
        )

    def save_progress(self, user_id: int, film_id: int, position: float, duration: float) -> None:
        """Досмотренный до конца фильм не запоминает позицию, чтобы не предлагать продолжить."""
        done = duration > 0 and position >= duration - 5
        pos = 0.0 if done else max(0.0, position)
        self.conn.execute(
            """INSERT INTO watch (user_id, film_id, position, duration) VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id, film_id) DO UPDATE SET position = excluded.position,
                 duration = excluded.duration, updated_at = datetime('now')""",
            (user_id, film_id, pos, max(0.0, duration)),
        )

    def get_watch(self, user_id: int, film_id: int) -> dict | None:
        return self._one("SELECT * FROM watch WHERE user_id = ? AND film_id = ?", (user_id, film_id))

    def history(self, user_id: int, limit: int = 60) -> list[dict]:
        return self._all(
            """SELECT f.*, w.position AS w_position, w.duration AS w_duration, w.updated_at AS w_updated
               FROM watch w JOIN films f ON f.id = w.film_id
               WHERE w.user_id = ? AND f.is_active = 1
               ORDER BY w.updated_at DESC LIMIT ?""",
            (user_id, limit),
        )

    def add_ad(self, start_sec: int, link: str, ext: str) -> int:
        cur = self.conn.execute("INSERT INTO ads (start_sec, link, ext) VALUES (?, ?, ?)", (start_sec, link, ext))
        return int(cur.lastrowid)

    def list_ads(self) -> list[dict]:
        return self._all("SELECT * FROM ads ORDER BY start_sec, id")

    def get_ad(self, ad_id: int) -> dict | None:
        return self._one("SELECT * FROM ads WHERE id = ?", (ad_id,))

    def delete_ad(self, ad_id: int) -> None:
        self.conn.execute("DELETE FROM ads WHERE id = ?", (ad_id,))

    # ---- продажи ----
    def record_sale(self, user_id: int, kind: str, amount: int, charge_id: str) -> bool:
        """Записывает оплату. Повторный charge_id не зачисляется (идемпотентность)."""
        self.touch_user(user_id, None, None)
        try:
            self.conn.execute(
                "INSERT INTO sales (user_id, kind, amount, charge_id) VALUES (?, ?, ?, ?)",
                (user_id, kind, amount, charge_id),
            )
            return True
        except sqlite3.IntegrityError:
            return False

    def get_sale(self, sale_id: int) -> dict | None:
        return self._one("SELECT * FROM sales WHERE id = ?", (sale_id,))

    def mark_refunded(self, sale_id: int) -> None:
        self.conn.execute("UPDATE sales SET refunded = 1 WHERE id = ?", (sale_id,))

    def list_sales(self, limit: int = 20, offset: int = 0) -> list[dict]:
        return self._all("SELECT * FROM sales ORDER BY id DESC LIMIT ? OFFSET ?", (limit, offset))

    def count_sales(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM sales")["n"]

    def revenue(self) -> int:
        return self._one("SELECT COALESCE(SUM(amount), 0) AS n FROM sales WHERE refunded = 0")["n"]

    def counts(self) -> dict:
        films = self._one("SELECT COUNT(*) AS n, COALESCE(SUM(views), 0) AS v FROM films")
        return {
            "films": films["n"],
            "views": films["v"],
            "users": self._one("SELECT COUNT(*) AS n FROM users")["n"],
            "vip": self._one("SELECT COUNT(*) AS n FROM users WHERE vip_until > ?", (fmt_dt(utcnow()),))["n"],
        }


# ============================================================================
# Медиа: ffmpeg, HLS-качества, очередь обработки, токены доступа
# ============================================================================
def ffmpeg_bin() -> str:
    try:
        import imageio_ffmpeg  # поставляет готовый ffmpeg, см. requirements.txt

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return shutil.which("ffmpeg") or "ffmpeg"


async def _run(args: list[str]) -> bytes:
    proc = await asyncio.create_subprocess_exec(
        ffmpeg_bin(), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        # обработка не должна мешать сайту и просмотру: у ffmpeg приоритет ниже обычного
        os.setpriority(os.PRIO_PROCESS, proc.pid, FFMPEG_NICE)
    except OSError:
        pass
    _, err = await proc.communicate()
    return err


ISO3 = {"rus": "ru", "eng": "en", "ukr": "uk", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
        "spa": "es", "ita": "it", "tur": "tr", "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh"}


def sub_name(lang: str) -> str:
    code = re.sub(r"[^a-z0-9-]", "", lang.lower())[:12]
    return ISO3.get(code, code) or "und"


def target_ladder() -> list[int]:
    """Какие качества нужны для фильма: всегда весь набор 240p/720p.

    Низкий исходник масштабируется вверх — фильм появляется в каталоге только тогда,
    когда готовы сразу оба качества.
    """
    return list(LADDER)


def mark_lost_sources(db: "Database", media_dir: Path) -> list[int]:
    """Фильмы без исходника (контейнер обновили посреди загрузки) помечает failed с понятной
    причиной: повторять обработку не из чего, админ увидит причину и загрузит видео заново."""
    marked = []
    for film in db.list_video_films():
        if film["status"] == "failed" or film["video_file_id"]:
            continue
        folder = media_dir / str(film["id"])
        if (folder / "source.mp4").exists():
            continue
        if film["status"] == "ready" and (folder / "master.m3u8").exists():
            continue  # видео уже собрано, исходник не нужен
        db.set_status(film["id"], "failed", "Источник видео не найден — загрузите видео заново")
        marked.append(film["id"])
    return marked


def hls_ready(folder: Path) -> bool:
    """Готовы ли оба качества (240p, 720p) и все плейлисты на месте."""
    master = folder / "master.m3u8"
    if not master.exists():
        return False
    names = {int(n[:-1]) for n in re.findall(r'NAME="(\d+p)"', master.read_text(encoding="utf-8", errors="replace"))}
    return set(LADDER) <= names and all((folder / f"{h}p" / "index.m3u8").exists() for h in names)


def convert_subtitle(src: Path, dest: Path) -> None:
    """Любой текстовый субтитр (SRT, ASS, SSA, VTT) -> WebVTT. Кодировка UTF-8 или CP1251."""
    last: Exception | None = None
    for enc in ("utf-8", "cp1251"):
        try:
            subs = pysubs2.load(str(src), encoding=enc)
            break
        except (UnicodeDecodeError, pysubs2.exceptions.Pysubs2Error) as err:
            last = err
    else:
        raise ValueError(f"Не удалось прочитать субтитры: {last}")
    if not len(subs):
        raise ValueError("В файле нет субтитров")
    subs.save(str(dest), encoding="utf-8", format_="vtt")


def list_subtitles(folder: Path) -> list[str]:
    sub_dir = folder / "subs"
    if not sub_dir.is_dir():
        return []
    return sorted(p.stem for p in sub_dir.glob("*.vtt"))


async def probe(src: Path) -> tuple[int, int, int]:
    """Возвращает (ширина, высота, длительность в секундах) через вывод ffmpeg."""
    err = (await _run(["-hide_banner", "-i", str(src)])).decode(errors="replace")
    size = re.search(r"Video:.*?, (\d{2,5})x(\d{2,5})", err)
    dur = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", err)
    if not size:
        return 0, 0, 0
    seconds = 0
    if dur:
        seconds = int(int(dur.group(1)) * 3600 + int(dur.group(2)) * 60 + float(dur.group(3)))
    return int(size.group(1)), int(size.group(2)), seconds


class Transcoder:
    """Одна фоновая очередь: скачивает файл из Telegram (если нужно) и строит HLS-качества.

    Оба качества (240p и 720p) собираются ОДНИМ процессом ffmpeg: исходник декодируется
    один раз, а в памяти висит один процесс, а не несколько — на хостинге с двумя ядрами
    и гигабайтом RAM это и быстрее, и без упора в лимит памяти. Фильм открывается только
    когда готовы оба качества: тогда один раз пишется master-плейлист со всеми дорожками.
    """

    def __init__(self, bot: Bot, db: Database, media_dir: Path) -> None:
        self.bot = bot
        self.db = db
        self.media_dir = media_dir
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._worker())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()

    def enqueue(self, film_id: int) -> None:
        self.db.set_status(film_id, "processing", None)
        self.queue.put_nowait(film_id)

    async def _worker(self) -> None:
        while True:
            film_id = await self.queue.get()
            try:
                await self.process(film_id)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                if self.db.get_film(film_id) is None:
                    shutil.rmtree(self.media_dir / str(film_id), ignore_errors=True)
                    log.info("фильм %s удалён во время обработки", film_id)
                else:
                    log.exception("обработка фильма %s упала", film_id)
                    self.db.set_status(film_id, "failed", str(err)[:300])
            finally:
                self.queue.task_done()

    async def process(self, film_id: int) -> None:
        film = self.db.get_film(film_id)
        if not film or film["kind"] != "video":
            return
        folder = self.media_dir / str(film_id)
        folder.mkdir(parents=True, exist_ok=True)
        src = folder / "source.mp4"

        if not src.exists() or src.stat().st_size == 0:
            if not film["video_file_id"]:
                raise RuntimeError("Нет исходного видео")
            try:
                tg_file = await self.bot.get_file(film["video_file_id"])
            except TelegramBadRequest as err:
                if "too big" in str(err).lower():
                    raise RuntimeError(
                        "Telegram не отдаёт боту файлы больше 20 МБ. Загрузите видео до 2 ГБ через Админ-панель в Mini App."
                    ) from err
                raise
            await self.bot.download_file(tg_file.file_path, destination=src)

        width, height, duration = await probe(src)
        if not height:
            raise RuntimeError("Файл не распознан как видео")

        # всегда весь набор качеств: фильм выходит на главную только когда готовы оба
        ladder = target_ladder()

        for old in folder.iterdir():
            if old.is_dir() and re.fullmatch(r"\d+p", old.name) and old.name not in {f"{h}p" for h in ladder}:
                shutil.rmtree(old, ignore_errors=True)
        build = folder / ".build"
        shutil.rmtree(build, ignore_errors=True)
        build.mkdir()

        def write_master(variants: list[tuple[int, int, int]]) -> None:
            lines = ["#EXTM3U", "#EXT-X-VERSION:3"]
            for h, w, kbps in sorted(variants):
                lines.append(f'#EXT-X-STREAM-INF:BANDWIDTH={(kbps + 128) * 1000},RESOLUTION={w}x{h},NAME="{h}p"')
                lines.append(f"{h}p/index.m3u8")
            tmp = folder / "master.m3u8.tmp"
            tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
            os.replace(tmp, folder / "master.m3u8")  # атомарная подмена, не ломает тех, кто уже смотрит

        def variant_args(h: int) -> tuple[list[str], int, int]:
            """Аргументы ffmpeg для одного качества: свой масштаб, битрейт и пресет."""
            w = max(2, round(width * h / height / 2) * 2)
            out = build / f"{h}p"
            out.mkdir(exist_ok=True)
            kbps = BITRATE_KBPS[h]
            akbps = AUDIO_KBPS.get(h, 128)
            preset = "veryfast" if h >= 720 else "ultrafast"   # 720p держим качественным для полного экрана
            return [
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", f"scale={w}:{h}",
                "-c:v", "libx264", "-preset", preset, "-profile:v", "high", "-threads", "0",
                "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
                "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", f"{akbps}k", "-ac", "2",
                "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(out / "seg_%04d.ts"),
                str(out / "index.m3u8"),
            ], w, kbps

        # все качества одним процессом: один декод исходника, одна копия ffmpeg в памяти
        args = ["-hide_banner", "-loglevel", "error", "-y", "-i", str(src)]
        done_variants: list[tuple[int, int, int]] = []
        for h in ladder:
            extra, w, kbps = variant_args(h)
            args += extra
            done_variants.append((h, w, kbps))
        err = await _run(args)
        missing = [h for h, _, _ in done_variants if not (build / f"{h}p" / "index.m3u8").exists()]
        if missing:
            raise RuntimeError("Не удалось собрать " + "/".join(f"{h}p" for h in missing) +
                               f": {err.decode(errors='replace')[-200:]}")
        if self.db.get_film(film_id) is None:  # фильм удалили, пока кодировали
            shutil.rmtree(build, ignore_errors=True)
            return

        for h, _, _ in sorted(done_variants):
            dest = folder / f"{h}p"
            shutil.rmtree(dest, ignore_errors=True)
            shutil.move(str(build / f"{h}p"), dest)
        write_master(done_variants)          # один плейлист сразу со всеми качествами
        shutil.rmtree(build, ignore_errors=True)
        self.db.update_film(film_id, status="ready", error=None,
                            duration=duration, width=width, height=height)
        log.info("фильм %s готов во всех качествах: %s", film_id, [v[0] for v in done_variants])


STORAGE_EVERY = 6 * 3600          # как часто чистить, секунды
UPLOAD_STALE = 24 * 3600          # незавершённая загрузка без записей дольше суток
AVATAR_TTL = 7 * 24 * 3600        # аватарки кэшируются на неделю
HISTORY_KEEP = 200                # записей истории просмотров на пользователя


def clean_storage(db: Database, media_dir: Path) -> dict:
    """Чистит кэш и базу. Возвращает счётчики для лога."""
    report = {"orphan_films": 0, "tmp": 0, "avatars": 0, "watch": 0}
    now = time.time()
    ids = db.film_ids()
    if media_dir.is_dir():
        for d in media_dir.iterdir():
            if d.is_dir() and d.name.isdigit() and int(d.name) not in ids:
                shutil.rmtree(d, ignore_errors=True)
                report["orphan_films"] += 1
    tmp = media_dir / "tmp"
    if tmp.is_dir():
        for f in tmp.iterdir():
            if now - f.stat().st_mtime > UPLOAD_STALE:
                shutil.rmtree(f, ignore_errors=True) if f.is_dir() else f.unlink(missing_ok=True)
                report["tmp"] += 1
    avatars = media_dir.parent / "avatars"
    if avatars.is_dir():
        for f in avatars.iterdir():
            ttl = AVATAR_TTL if f.suffix == ".jpg" else AVATAR_TTL // 7  # отметки «нет фото» живут сутки
            if now - f.stat().st_mtime > ttl:
                f.unlink(missing_ok=True)
                report["avatars"] += 1
    report["watch"] = db.prune_watch(HISTORY_KEEP)
    db.conn.commit()
    if any(report.values()):
        db.vacuum()  # после удалений возвращаем место на диске
    return report


async def tickets_loop(db: Database, media_dir: Path, every: int = 60) -> None:
    """Закрытый тикет живёт ещё 10 минут, потом удаляется из базы вместе с перепиской и картинками."""
    tickets_dir = media_dir.parent / "tickets"
    while True:
        await asyncio.sleep(every)
        try:
            for ticket_id in await asyncio.to_thread(db.drop_expired_tickets):
                shutil.rmtree(tickets_dir / str(ticket_id), ignore_errors=True)
                log.info("закрытый тикет %s удалён из базы", ticket_id)
        except Exception:
            log.exception("очистка тикетов не удалась")


async def storage_loop(db: Database, media_dir: Path) -> None:
    await asyncio.sleep(120)
    while True:
        try:
            report = await asyncio.to_thread(clean_storage, db, media_dir)
            log.info("очистка: %s", report)
        except Exception:
            log.exception("очистка не удалась")
        await asyncio.sleep(STORAGE_EVERY)


def _secret(cfg: Config) -> bytes:
    return hashlib.sha256(b"media:" + cfg.bot_token.encode()).digest()


def make_media_token(secret: bytes, film_id: int, ttl: int = 6 * 3600) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(secret, f"{film_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def check_media_token(secret: bytes, film_id: int, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(secret, f"{film_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


def embed_url(url: str) -> str:
    m = YOUTUBE_RE.search(url)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    return url


# ============================================================================
# Авторизация Mini App (подпись initData)
# ============================================================================
def verify_init_data(init_data: str, token: str) -> dict | None:
    """Проверяем только HMAC-подпись Telegram, без срока годности auth_date.

    Приложение Telegram на ПК (Windows/macOS) неделями показывает закэшированную
    страницу Mini App по исходной ссылке: подпись там настоящая, но auth_date старый.
    Из-за проверки «свежести» вход на ПК и падал с «Откройте приложение из Telegram».
    Подписанные данные сами по себе доказывают, что человек открывал приложение из
    Telegram, — этого достаточно, проверять давность не нужно."""
    if not init_data:
        return None
    from urllib.parse import parse_qsl

    pairs = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = pairs.pop("hash", None)
    if not their_hash:
        return None
    check = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    if not hmac.compare_digest(hmac.new(secret, check.encode(), hashlib.sha256).hexdigest(), their_hash):
        return None
    try:
        user = json.loads(pairs.get("user", "{}"))
    except json.JSONDecodeError:
        return None
    return user if isinstance(user, dict) and "id" in user else None


# ============================================================================
# Тексты и клавиатуры бота (без эмодзи)
# ============================================================================
START_TEXT = (
    "Здравствуйте! Вы попали к нам удачно. "
    "Здесь много аниме и мультфильмов, в том числе озвученных с помощью ИИ. "
    "Мы стараемся для вас. Если хотите нас поддержать, купите VIP."
)


def webapp_kb(url: str, label: str = "Открыть", staff_url: str | None = None) -> InlineKeyboardMarkup:
    rows = [[InlineKeyboardButton(text=label, web_app=WebAppInfo(url=url))]]
    if staff_url:
        rows.append([InlineKeyboardButton(text="Админ-панель", web_app=WebAppInfo(url=staff_url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


PAY_LABELS = {
    "vip_month": "VIP на месяц",
    "vip_year": "VIP на год",
    "donate": "Поддержка проекта",
}


def parse_payload(payload: str) -> tuple[str, int]:
    """Payload инвойса: 'vip_month', 'vip_year', 'donate:<n>'."""
    kind, _, arg = payload.partition(":")
    return kind, int(arg) if arg.isdigit() else 0


def expected_amount(cfg: Config, kind: str, arg: int) -> int | None:
    return {
        "vip_month": cfg.vip_month,
        "vip_year": cfg.vip_year,
    }.get(kind) or (arg if kind == "donate" and arg in DONATE_OPTIONS else None)


# ============================================================================
# Middleware: роль и регистрация пользователя
# ============================================================================
class RoleMiddleware(BaseMiddleware):
    def __init__(self, db: Database, owner_id: int) -> None:
        self.db = db
        self.owner_id = owner_id

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        role = USER
        if user and not user.is_bot:
            self.db.touch_user(user.id, user.username, user.first_name)
            role = OWNER if user.id == self.owner_id else self.db.get_role(user.id)
        data["role"] = role
        return await handler(event, data)


def build_router(cfg: Config, db: Database, transcoder: Transcoder) -> Router:
    router = Router(name="main")

    @router.message(CommandStart())
    async def on_start(message: Message, role: str) -> None:
        url = cfg.webapp_url
        if not url:
            await message.answer(START_TEXT)
            return
        staff_url = f"{url}#admin" if at_least(role, EDITOR) else None
        await message.answer(START_TEXT, reply_markup=webapp_kb(url, staff_url=staff_url))

    @router.message(F.video)
    async def on_video_from_staff(message: Message, role: str) -> None:
        """Видео, которое админ отправил боту, становится фильмом (до 20 МБ — лимит Bot API, больше — через Mini App до 2 ГБ)."""
        if not at_least(role, EDITOR):
            return
        video = message.video
        url = f"{cfg.webapp_url}#admin/upload" if cfg.webapp_url else None
        if (video.file_size or 0) > BOT_UPLOAD_LIMIT:
            text = ("Файл больше 20 МБ, бот не может его скачать. "
                    "Загрузите видео до 2 ГБ в Админ-панели Mini App.")
            await message.answer(text, reply_markup=webapp_kb(url) if url else None)
            return
        title = (message.caption or video.file_name or "Без названия").rsplit(".", 1)[0][:100]
        film_id = db.create_film(
            title=title, kind="video", video_file_id=video.file_id,
            owner_id=message.from_user.id, status="processing",
        )
        transcoder.enqueue(film_id)
        await message.answer(
            f"Видео #{film_id} принято и обрабатывается сразу в двух качествах: 240p и 720p. "
            "Заполните описание и обложку в Админ-панели.",
            reply_markup=webapp_kb(f"{cfg.webapp_url}#admin/film/{film_id}") if cfg.webapp_url else None,
        )

    @router.my_chat_member()
    async def on_left_bot(event: ChatMemberUpdated) -> None:
        """Пользователь заблокировал бота или закрыл его совсем: его тикеты удаляются сразу,
        чтобы у операторов не висела переписка с тем, кому уже не ответить."""
        if event.chat.type != "private":
            return
        if event.new_chat_member.status in ("kicked", "left"):
            removed = db.delete_user_tickets(event.from_user.id)
            if removed:
                log.info("пользователь %s ушёл из бота, удалено тикетов: %s", event.from_user.id, removed)

    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot, role: str) -> None:
        kind, arg = parse_payload(query.invoice_payload)
        expected = expected_amount(cfg, kind, arg)
        ok, error = True, None
        if expected is None or expected != query.total_amount:
            ok, error = False, "Сумма оплаты не совпадает с ценой"
        await bot.answer_pre_checkout_query(query.id, ok=ok, error_message=error)

    @router.message(F.successful_payment)
    async def on_paid(message: Message, bot: Bot) -> None:
        sp = message.successful_payment
        uid = message.from_user.id
        kind, arg = parse_payload(sp.invoice_payload)
        if not db.record_sale(uid, kind, sp.total_amount, sp.telegram_payment_charge_id):
            return  # уже обработано

        apply_sale(cfg, db, uid, kind, sp.total_amount)
        label = PAY_LABELS.get(kind, kind)
        await message.answer(f"Оплата прошла: {label}, {sp.total_amount} звёзд. Спасибо!",
                             reply_markup=webapp_kb(cfg.webapp_url) if cfg.webapp_url else None)
        try:
            await bot.send_message(
                cfg.owner_id,
                f"Новая оплата: {sp.total_amount} звёзд\n{label}\n"
                f"Покупатель: {db.display_name(uid)} ({uid})",
            )
        except TelegramAPIError as err:
            log.warning("владелец не уведомлён: %s", err)

    return router


def apply_sale(cfg: Config, db: Database, uid: int, kind: str, amount: int) -> None:
    """Эффект оплаты: продление VIP. Донат — только в статистику. Роли не продаются."""
    if kind == "vip_month":
        db.grant_vip(uid, VIP_MONTH_DAYS)
    elif kind == "vip_year":
        db.grant_vip(uid, VIP_YEAR_DAYS)


# ============================================================================
# HTTP: Mini App, API
# ============================================================================
class ChunkUploads:
    """Загрузка видео частями: каждая часть — отдельный короткий запрос, пишется в свой участок файла."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.items: dict[str, dict] = {}

    def create(self, owner: int, size: int, name: str) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        uid = uuid.uuid4().hex
        path = self.root / uid
        with open(path, "wb") as fh:
            fh.truncate(size)
        total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        self.items[uid] = {"owner": owner, "size": size, "path": path, "name": name,
                           "total": total, "received": set()}
        return uid

    def get(self, uid: str, owner: int) -> dict | None:
        item = self.items.get(uid)
        return item if item and item["owner"] == owner else None

    def discard(self, uid: str) -> None:
        item = self.items.pop(uid, None)
        if item:
            item["path"].unlink(missing_ok=True)


class Api:
    """Контекст запроса: пользователь, роль, VIP."""

    def __init__(self, cfg: Config, db: Database, bot: Bot, transcoder: Transcoder) -> None:
        self.cfg = cfg
        self.db = db
        self.bot = bot
        self.transcoder = transcoder
        self.secret = _secret(cfg)

    def user_of(self, request: web.Request) -> dict | None:
        if "_user" in request:      # разбирать init data и ходить в базу дважды на запрос незачем
            return request["_user"]
        user = self._parse_user(request)
        request["_user"] = user
        return user

    def _parse_user(self, request: web.Request) -> dict | None:
        # init data приходит в заголовке, но тег <img> заголовки слать не умеет —
        # картинки тикетов несут подпись в параметре ?init=
        init = request.headers.get("X-Init-Data", "") or request.query.get("init", "")
        user = verify_init_data(init, self.cfg.bot_token)
        if not user:
            return None
        uid = int(user["id"])
        self.db.touch_user(uid, user.get("username"), user.get("first_name"))
        role = OWNER if uid == self.cfg.owner_id else self.db.get_role(uid)
        row = self.db.get_user(uid) or {}
        return {"id": uid, "name": user.get("first_name") or user.get("username") or "Пользователь",
                "role": role, "vip_until": row.get("vip_until"),
                "vip_forever": bool(row.get("vip_forever")) or is_forever(row.get("vip_until")),
                "stars": int(row.get("stars") or 0), "banned": bool(row.get("banned")),
                "name_gradient": row.get("name_gradient"),
                "tour_seen": bool(row.get("tour_seen"))}

    def vip_active(self, user: dict | None) -> bool:
        """Есть ли у пользователя доступ к VIP-контенту (включая служебный доступ персонала:
        владелец и админы/редакторы считаются VIP всегда, даже без оплаченной подписки)."""
        if not user:
            return False
        if at_least(user["role"], ADMIN):
            return True
        if user.get("vip_forever"):
            return True
        until = parse_dt(user.get("vip_until"))
        return bool(until and until > utcnow())

    def review_limit(self, user: dict | None) -> int:
        """Сколько отзывов на один фильм можно оставить: с VIP — два, без VIP — один."""
        return REVIEW_LIMIT_VIP if self.vip_active(user) else REVIEW_LIMIT

    def person(self, tg_id: int) -> dict:
        """Автор отзыва: имя, роль, VIP и градиент ника (градиент только у активного VIP)."""
        row = self.db.get_user(tg_id) or {}
        role = OWNER if tg_id == self.cfg.owner_id else (row.get("role") or USER)
        if role not in RANK:
            role = USER
        pseudo = {"id": tg_id, "role": role, "vip_until": row.get("vip_until"),
                  "vip_forever": bool(row.get("vip_forever")) or is_forever(row.get("vip_until"))}
        vip = self.vip_active(pseudo)
        return {
            "name": row.get("first_name") or (f"@{row['username']}" if row.get("username") else "Пользователь"),
            "vip": vip,
            "role": role,
            "gradient": (row.get("name_gradient") if vip else None),
        }

    def locked(self, user: dict | None, film: dict) -> bool:
        return bool(film["is_vip"]) and not self.vip_active(user)

    def film_json(self, user: dict | None, film: dict, detail: bool = False,
                  favs: set[int] | None = None) -> dict:
        is_series = film["kind"] == "series"
        rating, reviews_n = self.db.rating(film["id"])
        locked = self.locked(user, film)
        # у серии может не быть своей обложки — тогда берём обложку сериала
        parent = self.db.get_film(film["series_id"]) if film["series_id"] else None
        has_poster = bool(film["poster"] or film["photo_file_id"])
        if not has_poster and parent:
            has_poster = bool(parent["poster"] or parent["photo_file_id"])
        data = {
            "id": film["id"],
            "title": film["title"],
            "description": film["description"],
            "releaseDate": film["release_date"],
            "creator": film["creator"],
            "kind": film["kind"],
            "genre": clean_genre(film.get("genre")),
            "genreTitle": GENRE_TITLES[clean_genre(film.get("genre"))],
            "isSeries": is_series,
            "isVip": bool(film["is_vip"]),
            "locked": locked,
            "views": film["views"],
            "duration": film["duration"],
            "width": film["width"],
            "height": film["height"],
            "status": film["status"],
            "hasPoster": has_poster,
            "rating": rating,
            "reviewCount": reviews_n,
            "country": film["country"],
            "isMine": bool(user and film.get("owner_id") == user["id"]),
            "seriesId": film["series_id"],
            "episode": film["episode"],
            "favorite": film["id"] in favs if favs is not None else False,
        }
        if parent:
            data["seriesTitle"] = parent["title"]
        if film["kind"] == "series" and detail:
            data["episodes"] = [
                {"id": e["id"], "episode": e["episode"], "title": e["title"],
                 "status": e["status"], "error": e["error"], "duration": e["duration"],
                 "locked": self.locked(user, e), "hasPoster": bool(e["poster"] or film["poster"] or film["photo_file_id"])}
                for e in self.db.episodes(film["id"])
            ]
        if film["kind"] == "link" and not locked:
            data["url"] = film["url"]
        if detail:
            # отзывы приходят уже отсортированными: сначала VIP, потом обычные
            data["reviews"] = [self.review_json(user, r) for r in self.db.reviews(film["id"], self.cfg.owner_id)]
        data["reviewLimit"] = self.review_limit(user)
        data["canReview"] = bool(user) and not locked
        return data

    def review_json(self, user: dict | None, r: dict) -> dict:
        """Отзыв с данными автора: градиент ника и золотая приписка VIP."""
        who = self.person(r["user_id"])
        return {
            "id": r["id"], "userId": r["user_id"], "name": who["name"], "rating": r["rating"],
            "text": r["text"], "date": r["created_at"],
            "vip": who["vip"], "gradient": who["gradient"],
            "canDelete": bool(user and (r["user_id"] == user["id"] or at_least(user["role"], ADMIN))),
        }

    def can_manage(self, user: dict, film: dict) -> bool:
        if at_least(user["role"], ADMIN):
            return True
        return user["role"] == EDITOR and film.get("owner_id") == user["id"]


def _json(data: Any, status: int = 200) -> web.Response:
    return web.json_response(data, status=status, headers={"Cache-Control": "no-store"})


def _err(code: str, status: int) -> web.Response:
    return _json({"error": code}, status)


async def _read_multipart(request: web.Request, tmp_dir: Path) -> tuple[dict[str, str], dict[str, tuple[Path, str]]]:
    """Потоковая запись файлов на диск (память не растёт), каждый файл не больше UPLOAD_LIMIT."""
    if not request.content_type.startswith("multipart/"):
        raise web.HTTPBadRequest(text="expected multipart/form-data")
    if request.content_length and request.content_length > UPLOAD_LIMIT + 16 * 1024 * 1024:
        raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_LIMIT, actual_size=request.content_length)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    fields: dict[str, str] = {}
    files: dict[str, tuple[Path, str]] = {}
    try:
        reader = await request.multipart()
        async for part in reader:
            if part.filename:
                target = tmp_dir / f"{uuid.uuid4().hex}"
                size = 0
                with open(target, "wb") as fh:
                    while chunk := await part.read_chunk(1 << 20):
                        size += len(chunk)
                        if size > UPLOAD_LIMIT:
                            fh.close()
                            target.unlink(missing_ok=True)
                            raise web.HTTPRequestEntityTooLarge(max_size=UPLOAD_LIMIT, actual_size=size)
                        fh.write(chunk)
                files[part.name] = (target, part.filename)
            else:
                fields[part.name] = (await part.text()).strip()
    except BaseException:
        for path, _ in files.values():
            path.unlink(missing_ok=True)
        raise
    return fields, files


SUB_LABELS = {
    "ru": "Русские", "en": "English", "uk": "Українська", "de": "Deutsch", "fr": "Français",
    "es": "Español", "it": "Italiano", "tr": "Türkçe", "ja": "日本語", "ko": "한국어", "zh": "中文", "und": "Субтитры",
}


async def _subtitle_upload(api: "Api", request: web.Request, film: dict, folder: Path) -> web.Response:
    fields, files = await _read_multipart(request, folder.parent / "tmp")
    try:
        if "file" not in files:
            return _err("subtitle_required", 400)
        lang = sub_name(fields.get("lang", "ru"))
        src, _ = files["file"]
        (folder / "subs").mkdir(parents=True, exist_ok=True)
        try:
            convert_subtitle(src, folder / "subs" / f"{lang}.vtt")
        except ValueError:
            return _err("bad_subtitle", 400)
        return _json({"ok": True, "subtitles": list_subtitles(folder)})
    finally:
        for path, _ in files.values():
            path.unlink(missing_ok=True)


def _poster_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"


def build_web(cfg: Config, db: Database, bot: Bot, dp: Dispatcher,
              transcoder: Transcoder, bot_username: str | None) -> web.Application:
    api = Api(cfg, db, bot, transcoder)
    media_root = cfg.media_dir
    uploads = ChunkUploads(media_root / "tmp")
    tickets_dir = cfg.media_dir.parent / "tickets"      # картинки из тикетов

    # Забаненному пользователю сайт закрыт полностью: открыты только он сам и поддержка,
    # чтобы он мог написать апелляцию. Бан хранится в базе, перезаход в бота его не снимает.
    ban_free = re.compile(r"^/api/(me|config|avatar/|tickets)")

    @web.middleware
    async def ban_guard(request: web.Request, handler):
        if request.path.startswith("/api/") and not ban_free.match(request.path):
            user = api.user_of(request)
            if user and user.get("banned"):
                return _err("banned", 403)
        return await handler(request)

    app = web.Application(client_max_size=UPLOAD_LIMIT + 16 * 1024 * 1024, middlewares=[ban_guard])

    def staff_user(request: web.Request, minimum: str) -> tuple[dict | None, web.Response | None]:
        user = api.user_of(request)
        if not user:
            return None, _err("auth", 401)
        if not at_least(user["role"], minimum):
            return None, _err("forbidden", 403)
        return user, None

    # ---- публичное ----
    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def page(_: web.Request) -> web.FileResponse:
        return web.FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    async def static_hls(_: web.Request) -> web.FileResponse:
        return web.FileResponse(STATIC_DIR / "hls.min.js", headers={"Cache-Control": "public, max-age=86400"})

    async def config(_: web.Request) -> web.Response:
        return _json({
            "botUsername": bot_username,
            "background": cfg.background_url,
            "prices": {"vipMonth": cfg.vip_month, "vipYear": cfg.vip_year},
            "donate": list(DONATE_OPTIONS),
            # жанры: подписи для карточек каталога и страницы фильма
            "genres": [{"id": g, "title": GENRE_TITLES[g]} for g in GENRES],
            "reviewLimit": REVIEW_LIMIT,
            "reviewLimitVip": REVIEW_LIMIT_VIP,
        })

    async def me(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _json({"guest": True})
        return _json({
            "guest": False, "id": user["id"], "name": user["name"], "role": user["role"],
            "roleTitle": ROLE_TITLES[user["role"]], "vipUntil": user["vip_until"],
            "vipActive": api.vip_active(user),
            "vipForever": user["vip_forever"],
            "stars": user["stars"],
            "nameGradient": user["name_gradient"] if api.vip_active(user) else None,
            "reviewLimit": api.review_limit(user),
            "banned": bool(user.get("banned")),
            "canUpload": at_least(user["role"], EDITOR),
            "canManage": at_least(user["role"], ADMIN),
            "isOwner": user["role"] == OWNER,
            "tourSeen": bool(user.get("tour_seen")),
        })

    async def films(request: web.Request) -> web.Response:
        user = api.user_of(request)
        favs = set(db.favorites(user["id"])) if user else set()
        return _json([api.film_json(user, f, favs=favs) for f in db.list_films()])

    async def my_style(request: web.Request) -> web.Response:
        """Оформление ника: gradient разрешён только активному VIP."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if not api.vip_active(user):
            return _err("vip_required", 403)
        ok, gradient = clean_gradient(body.get("gradient"))
        if not ok:
            return _err("bad_gradient", 400)
        db.set_name_gradient(user["id"], gradient)
        return _json({"ok": True, "nameGradient": gradient})

    async def pay_stars(request: web.Request) -> web.Response:
        """Покупка VIP за звёзды с внутреннего баланса (баланс пополняет владелец/админ)."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        try:
            body = await request.json()
            kind = str(body.get("kind", ""))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        price = {"vip_month": cfg.vip_month, "vip_year": cfg.vip_year}.get(kind)
        if not price:
            return _err("bad_request", 400)
        if user["vip_forever"]:   # бессрочный VIP уже есть, продлевать нечего
            return _err("already_forever", 400)
        if not db.spend_stars(user["id"], price):
            return _err("not_enough_stars", 400)
        if kind == "vip_month":
            db.grant_vip(user["id"], VIP_MONTH_DAYS)
        else:
            db.grant_vip(user["id"], VIP_YEAR_DAYS)
        row = db.get_user(user["id"]) or {}
        return _json({"ok": True, "stars": int(row.get("stars") or 0),
                      "vipUntil": row.get("vip_until"),
                      "vipForever": bool(row.get("vip_forever")) or is_forever(row.get("vip_until"))})

    async def film_detail(request: web.Request) -> web.Response:
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        user = api.user_of(request)
        favs = set(db.favorites(user["id"])) if user else set()
        return _json(api.film_json(user, film, detail=True, favs=favs))

    def _film_or_none(request: web.Request) -> dict | None:
        try:
            film = db.get_film(int(request.match_info["film_id"]))
        except ValueError:
            return None
        return film if film and film["is_active"] else None

    async def poster(request: web.Request) -> web.Response:
        film = _film_or_none(request)
        if not film:
            return web.Response(status=404)
        if not film["poster"] and film["series_id"]:
            film = db.get_film(film["series_id"]) or film
        if film["poster"] and Path(film["poster"]).exists():
            return web.FileResponse(film["poster"], headers={"Cache-Control": "public, max-age=3600"})
        if film["photo_file_id"]:
            try:
                tg_file = await bot.get_file(film["photo_file_id"])
                target = media_root / str(film["id"]) / f"poster{Path(tg_file.file_path).suffix or '.jpg'}"
                target.parent.mkdir(parents=True, exist_ok=True)
                await bot.download_file(tg_file.file_path, destination=target)
                db.update_film(film["id"], poster=str(target))
                return web.FileResponse(target, headers={"Cache-Control": "public, max-age=3600"})
            except TelegramAPIError:
                return web.Response(status=404)
        return web.Response(status=404)

    async def play(request: web.Request) -> web.Response:
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        if film["kind"] == "series":
            return _err("not_playable", 400)
        if film["kind"] == "video" and film["status"] != "ready":
            return _err("processing", 202)
        user = api.user_of(request)
        # без Telegram (просто ссылка в браузере) смотреть можно всё, кроме VIP:
        # проверить подписку без аккаунта нечем
        if user:
            if api.locked(user, film):
                return _json({"error": "vip_required"}, 403)
        elif film["is_vip"]:
            return _json({"error": "vip_required"}, 403)
        if user:
            db.touch_watch(user["id"], film["id"])
        if film["kind"] == "link":
            return _json({"kind": "link", "src": embed_url(film["url"]), "url": film["url"]})
        watch = (db.get_watch(user["id"], film["id"]) if user else None) or {}
        pos = float(watch.get("position") or 0)
        dur = float(watch.get("duration") or film["duration"] or 0)
        resume_at = pos if 5 < pos < dur - 15 else 0
        # реклама не показывается тем, у кого есть доступ к VIP-контенту: ни реальным платным
        # подписчикам, ни персоналу (владелец, админы, редакторы) — у всех них vip_active() = True
        ads = []
        if (not user or not api.vip_active(user)) and dur >= 30:
            ads = [{"id": a["id"], "src": f"/api/ad/{a['id']}/video", "link": a["link"], "at": a["start_sec"]}
                   for a in db.list_ads()]
        token = make_media_token(api.secret, film["id"])
        subs = [{"lang": n, "label": SUB_LABELS.get(n.rstrip("0123456789"), n.upper()),
                 "src": f"/api/media/{film['id']}/{token}/subs/{n}.vtt"}
                for n in list_subtitles(media_root / str(film["id"]))]
        return _json({"kind": "hls", "src": f"/api/media/{film['id']}/{token}/master.m3u8", "subtitles": subs,
                      "width": film["width"], "height": film["height"], "resumeAt": resume_at, "ads": ads})

    async def media(request: web.Request) -> web.StreamResponse:
        try:
            film_id = int(request.match_info["film_id"])
        except ValueError:
            return web.Response(status=404)
        if not check_media_token(api.secret, film_id, request.match_info["token"]):
            return web.Response(status=403)
        base = (media_root / str(film_id)).resolve()
        target = (base / request.match_info["path"]).resolve()
        if base not in target.parents or not target.is_file():
            return web.Response(status=404)
        ctype = {".m3u8": "application/vnd.apple.mpegurl", ".ts": "video/mp2t", ".vtt": "text/vtt; charset=utf-8"}.get(
            target.suffix, "application/octet-stream")
        return web.FileResponse(target, headers={"Content-Type": ctype, "Cache-Control": "private, max-age=3600"})

    async def add_review(request: web.Request) -> web.Response:
        """Отзыв на фильм, серию или сериал. Лимит: один отзыв, с VIP — два."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        if api.locked(user, film):
            return _err("vip_required", 403)
        try:
            body = await request.json()
            rating = int(body.get("rating", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        text = str(body.get("text", "")).strip()[:1000]
        if not 1 <= rating <= 5:
            return _err("bad_rating", 400)

        edit_id = body.get("id")
        if edit_id is not None:  # правка уже оставленного отзыва
            try:
                review = db.get_review(int(edit_id))
            except (TypeError, ValueError):
                return _err("bad_request", 400)
            if not review or review["user_id"] != user["id"]:
                return _err("forbidden", 403)
            db.update_review(review["id"], rating, text)
            return _json({"ok": True, "id": review["id"]})

        limit = api.review_limit(user)
        if db.count_user_reviews(film["id"], user["id"]) >= limit:
            return _err("review_limit", 400)
        review_id = db.add_review(film["id"], user["id"], rating, text)
        return _json({"ok": True, "id": review_id, "reviewLimit": limit})

    async def delete_review(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        review = db.get_review(int(request.match_info["id"]))
        if not review:
            return _err("not_found", 404)
        if review["user_id"] != user["id"] and not at_least(user["role"], ADMIN):
            return _err("forbidden", 403)
        db.delete_review(review["id"])
        return _json({"ok": True})

    async def invoice(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        try:
            body = await request.json()
            kind = str(body.get("kind", ""))
            amount = int(body.get("amount", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if kind == "donate":
            payload_arg = amount
        elif kind in ("vip_month", "vip_year"):
            payload_arg = 0
        else:
            return _err("bad_request", 400)
        expected = expected_amount(cfg, kind, payload_arg)
        if expected is None:
            return _err("bad_amount", 400)
        payload = f"{kind}:{payload_arg}" if kind == "donate" else kind
        link = await bot.create_invoice_link(
            title=PAY_LABELS[kind][:32],
            description=f"{PAY_LABELS[kind]}. Оплата звёздами Telegram",
            payload=payload,
            currency="XTR",
            prices=[LabeledPrice(label=PAY_LABELS[kind][:32], amount=expected)],
        )
        return _json({"invoice": link, "amount": expected})

    # ---- админ ----
    async def admin_films(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        owner = None if at_least(user["role"], ADMIN) else user["id"]
        rows = db.list_films(public=False, owner_id=owner)
        return _json([{**api.film_json(user, f, detail=True), "error": f["error"], "isActive": bool(f["is_active"]),
                       "subtitles": list_subtitles(media_root / str(f["id"])) if f["kind"] == "video" else []}
                      for f in rows])

    async def upload_init(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        try:
            body = await request.json()
            size = int(body.get("size", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if size <= 0:
            return _err("bad_request", 400)
        if size > UPLOAD_LIMIT:
            return _err("too_large", 413)
        uid = uploads.create(user["id"], size, str(body.get("name", ""))[:200])
        total = (size + CHUNK_SIZE - 1) // CHUNK_SIZE
        return _json({"id": uid, "chunkSize": CHUNK_SIZE, "total": total})

    async def upload_chunk(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        item = uploads.get(request.match_info["uid"], user["id"])
        if not item:
            return _err("not_found", 404)
        try:
            index = int(request.match_info["index"])
        except ValueError:
            return _err("bad_request", 400)
        if not 0 <= index < item["total"]:
            return _err("bad_chunk", 400)
        start = index * CHUNK_SIZE
        expected = min(CHUNK_SIZE, item["size"] - start)
        got = 0
        # обрыв соединения (слабая сеть, прокси закрыл долгий запрос) — не ошибка сервера:
        # часть просто не засчитывается, и клиент повторяет её целиком
        try:
            with open(item["path"], "r+b") as fh:
                fh.seek(start)
                async for part in request.content.iter_chunked(1 << 20):
                    got += len(part)
                    if got > expected:
                        return _err("bad_chunk", 400)
                    fh.write(part)
        except (ConnectionError, asyncio.TimeoutError, asyncio.IncompleteReadError):
            return _err("interrupted", 499)
        if got != expected:
            return _err("bad_chunk", 400)
        item["received"].add(index)
        return _json({"ok": True, "received": len(item["received"]), "total": item["total"]})

    async def admin_create(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        tmp = media_root / "tmp"
        fields, files = await _read_multipart(request, tmp)
        try:
            title = fields.get("title", "").strip()[:120]
            kind = fields.get("kind", "video")
            if not title:
                return _err("title_required", 400)
            genre = clean_genre(fields.get("genre"), DEFAULT_GENRE)
            if kind == "link" and not URL_RE.match(fields.get("url", "")):
                return _err("bad_url", 400)
            upload_item = None
            if kind == "video":
                upload_item = uploads.get(fields.get("uploadId", ""), user["id"])
                if not upload_item:
                    return _err("video_required", 400)
                if len(upload_item["received"]) != upload_item["total"]:
                    return _err("upload_incomplete", 400)
            if kind != "video":
                return _err("bad_kind", 400)
            series = None
            episode = 0
            series_raw = fields.get("seriesId", "")
            if series_raw:
                series = _managed(user, int(series_raw)) if series_raw.isdigit() else None
                if not series or series["kind"] != "series":
                    return _err("not_found", 404)
                if kind != "video":
                    return _err("bad_kind", 400)
                # сезонов нет: серии — один плоский список с номерами
                try:
                    episode = int(fields.get("episode", "1"))
                except ValueError:
                    return _err("bad_request", 400)
                if not (1 <= episode <= 999):
                    return _err("bad_request", 400)
                if db.episode_exists(series["id"], episode):
                    return _err("episode_exists", 400)
            film_id = db.create_film(
                title=title,
                kind=kind,
                url=fields.get("url") or None,
                owner_id=user["id"],
                description=fields.get("description", "")[:4000],
                release_date=fields.get("releaseDate", "")[:10],
                creator=fields.get("creator", "")[:120],
                country=fields.get("country", "")[:80],
                is_vip=1 if fields.get("isVip") in ("1", "true", "on") else 0,
                status="ready" if kind == "link" else "processing",
                series_id=series["id"] if series else None,
                season=1,
                episode=episode,
                genre=genre,
            )
            if series:  # серия наследует VIP, автора, страну и дату сериала; жанр у серии свой
                db.update_film(film_id, is_vip=series["is_vip"], creator=series["creator"],
                               country=series["country"], release_date=series["release_date"])
            folder = media_root / str(film_id)
            try:
                folder.mkdir(parents=True, exist_ok=True)
                if upload_item:
                    shutil.move(str(upload_item["path"]), folder / "source.mp4")
                    uploads.items.pop(fields["uploadId"], None)
                if "poster" in files:
                    dest = folder / f"poster{_poster_ext(files['poster'][1])}"
                    shutil.move(str(files["poster"][0]), dest)
                    db.update_film(film_id, poster=str(dest))
                if kind == "video":
                    transcoder.enqueue(film_id)
            except Exception:
                # запись без видео оставаться не должна: фильм убираем, parts-загрузку
                # не трогаем — повторное нажатие «Добавить» досоздаст фильм из тех же частей
                log.exception("создание фильма %s оборвалось", film_id)
                db.delete_film(film_id)
                shutil.rmtree(folder, ignore_errors=True)
                return _err("server_error", 500)
            return _json(api.film_json(user, db.get_film(film_id)), 201)
        finally:
            for path, _ in files.values():
                if path.exists():
                    path.unlink()

    def _managed(user: dict, film_id: int) -> dict | None:
        film = db.get_film(film_id)
        if film and api.can_manage(user, film):
            return film
        return None

    async def admin_update(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        film = _managed(user, int(request.match_info["id"]))
        if not film:
            return _err("not_found", 404)
        body = await request.json()
        fields: dict[str, Any] = {}
        if "title" in body:
            fields["title"] = str(body["title"]).strip()[:120] or film["title"]
        if "description" in body:
            fields["description"] = str(body["description"])[:4000]
        if "releaseDate" in body:
            fields["release_date"] = str(body["releaseDate"])[:10]
        if "creator" in body:
            fields["creator"] = str(body["creator"])[:120]
        if "country" in body:
            fields["country"] = str(body["country"])[:80]
        if "genre" in body:
            fields["genre"] = clean_genre(body.get("genre"), clean_genre(film.get("genre"), DEFAULT_GENRE))
        if "url" in body and film["kind"] == "link":
            if not URL_RE.match(str(body["url"])):
                return _err("bad_url", 400)
            fields["url"] = str(body["url"])
        if "isVip" in body:
            fields["is_vip"] = 1 if body["isVip"] else 0
        if "isActive" in body:
            fields["is_active"] = 1 if body["isActive"] else 0
        db.update_film(film["id"], **fields)
        if film["kind"] == "series" and "is_vip" in fields:
            # VIP-флаг сериала переходит на все его серии; жанр у каждой серии свой
            db.conn.execute("UPDATE films SET is_vip = ? WHERE series_id = ?", (fields["is_vip"], film["id"]))
        return _json(api.film_json(user, db.get_film(film["id"])))

    async def admin_poster(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        film = _managed(user, int(request.match_info["id"]))
        if not film:
            return _err("not_found", 404)
        fields, files = await _read_multipart(request, media_root / "tmp")
        try:
            if "poster" not in files:
                return _err("poster_required", 400)
            folder = media_root / str(film["id"])
            folder.mkdir(parents=True, exist_ok=True)
            dest = folder / f"poster{_poster_ext(files['poster'][1])}"
            shutil.move(str(files["poster"][0]), dest)
            db.update_film(film["id"], poster=str(dest))
            return _json({"ok": True})
        finally:
            for path, _ in files.values():
                if path.exists():
                    path.unlink()

    async def admin_subtitle_add(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        film = _managed(user, int(request.match_info["id"]))
        if not film or film["kind"] != "video":
            return _err("not_found", 404)
        return await _subtitle_upload(api, request, film, media_root / str(film["id"]))

    async def admin_subtitle_delete(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        film = _managed(user, int(request.match_info["id"]))
        if not film:
            return _err("not_found", 404)
        name = sub_name(request.match_info["lang"])
        (media_root / str(film["id"]) / "subs" / f"{name}.vtt").unlink(missing_ok=True)
        return _json({"ok": True, "subtitles": list_subtitles(media_root / str(film["id"]))})

    async def admin_delete(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        film = _managed(user, int(request.match_info["id"]))
        if not film:
            return _err("not_found", 404)
        for ep in db.episodes(film["id"]) if film["kind"] == "series" else []:
            db.delete_film(ep["id"])
            shutil.rmtree(media_root / str(ep["id"]), ignore_errors=True)
        db.delete_film(film["id"])
        shutil.rmtree(media_root / str(film["id"]), ignore_errors=True)
        return _json({"ok": True})

    async def admin_series_create(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        try:
            body = await request.json()
        except json.JSONDecodeError:
            return _err("bad_request", 400)
        title = str(body.get("title", "")).strip()[:120]
        if not title:
            return _err("title_required", 400)
        film_id = db.create_film(
            title=title, kind="series", owner_id=user["id"], status="ready",
            description=str(body.get("description", ""))[:4000],
            release_date=str(body.get("releaseDate", ""))[:10],
            creator=str(body.get("creator", ""))[:120],
            country=str(body.get("country", ""))[:80],
            is_vip=1 if body.get("isVip") else 0,
            genre=clean_genre(body.get("genre"), DEFAULT_GENRE),
        )
        return _json(api.film_json(user, db.get_film(film_id), detail=True), 201)

    async def admin_reprocess(request: web.Request) -> web.Response:
        user, bad = staff_user(request, EDITOR)
        if bad:
            return bad
        film = _managed(user, int(request.match_info["id"]))
        if not film or film["kind"] != "video":
            return _err("not_found", 404)
        transcoder.enqueue(film["id"])
        return _json({"ok": True})

    async def admin_users(request: web.Request) -> web.Response:
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        rows = db.search_users(request.query.get("q", "").strip()[:60])
        return _json([{
            "id": r["tg_id"], "name": r["first_name"] or r["username"] or str(r["tg_id"]),
            "role": r["role"] if r["tg_id"] != cfg.owner_id else OWNER,
            "vipUntil": r["vip_until"],
            "vipForever": bool(r["vip_forever"]) or is_forever(r["vip_until"]),
            "vipActive": bool((parse_dt(r["vip_until"]) or datetime.min.replace(tzinfo=timezone.utc)) > utcnow()),
            "stars": int(r["stars"] or 0),
            "banned": bool(r["banned"]),
        } for r in rows])

    async def admin_role(request: web.Request) -> web.Response:
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        body = await request.json()
        try:
            target_id = int(body.get("id"))
        except (TypeError, ValueError):
            return _err("bad_id", 400)
        new_role = str(body.get("role", ""))
        if new_role not in (USER, OPERATOR, EDITOR, ADMIN):
            return _err("bad_role", 400)
        if target_id == cfg.owner_id:
            return _err("owner", 400)
        current = db.get_role(target_id)
        if not can_change_role(user["role"], current, new_role):
            return _err("forbidden", 403)
        db.set_role(target_id, new_role, added_by=user["id"])
        try:
            await bot.send_message(target_id, f"Ваша роль в боте: {ROLE_TITLES[new_role]}.")
        except TelegramAPIError:
            pass
        return _json({"ok": True})

    async def admin_vip(request: web.Request) -> web.Response:
        """Выдача VIP: на 1 день, на срок в днях, навсегда (∞) и полный отзыв."""
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        try:
            body = await request.json()
            target_id = int(body.get("id"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        forever = bool(body.get("forever"))
        revoke = bool(body.get("revoke"))
        try:
            days = int(body.get("days") or 0)
        except (TypeError, ValueError):
            return _err("bad_request", 400)

        if revoke:
            db.revoke_vip(target_id)
            text = "VIP отозван."
            until_raw: str | None = None
        elif forever or days == -1:
            db.grant_vip_forever(target_id)
            text = "Вам выдан VIP навсегда."
            until_raw = VIP_FOREVER
        else:
            if days <= 0 or days > 3650:
                return _err("bad_days", 400)
            until = db.grant_vip(target_id, days)
            text = f"Вам выдан VIP на {days} дн."
            until_raw = fmt_dt(until)
        try:
            await bot.send_message(target_id, text)
        except TelegramAPIError:
            pass
        row = db.get_user(target_id) or {}
        return _json({"ok": True, "vipUntil": row.get("vip_until"),
                      "vipForever": bool(row.get("vip_forever")) or is_forever(row.get("vip_until"))})

    async def admin_stars(request: web.Request) -> web.Response:
        """Выдача звёзд на внутренний баланс себе или любому пользователю по ID.
        Звёздами с баланса можно купить VIP в магазине без списания Telegram Stars."""
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        try:
            body = await request.json()
            target_id = int(body.get("id"))
            amount = int(body.get("amount"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if amount == 0 or abs(amount) > 1_000_000:
            return _err("bad_amount", 400)
        balance = db.add_stars(target_id, amount)   # минус списывает, баланс не уходит ниже нуля
        if amount > 0 and target_id != user["id"]:
            try:
                await bot.send_message(target_id, f"Вам начислено {amount} звёзд. Баланс: {balance} ★.")
            except TelegramAPIError:
                pass
        return _json({"ok": True, "id": target_id, "stars": balance})

    async def admin_sales(request: web.Request) -> web.Response:
        user, bad = staff_user(request, OWNER)
        if bad:
            return bad
        page = max(0, int(request.query.get("page", "0") or 0))
        rows = db.list_sales(limit=20, offset=page * 20)
        return _json({
            "total": db.count_sales(),
            "sales": [{
                "id": s["id"], "user": db.display_name(s["user_id"]), "userId": s["user_id"],
                "kind": s["kind"], "label": PAY_LABELS.get(s["kind"], s["kind"]),
                "amount": s["amount"], "refunded": bool(s["refunded"]), "date": s["created_at"],
                "refundable": bool(s["charge_id"]) and not s["refunded"],
            } for s in rows],
        })

    async def admin_refund(request: web.Request) -> web.Response:
        user, bad = staff_user(request, OWNER)
        if bad:
            return bad
        sale = db.get_sale(int(request.match_info["id"]))
        if not sale or sale["refunded"] or not sale["charge_id"]:
            return _err("not_refundable", 400)
        try:
            await bot.refund_star_payment(user_id=sale["user_id"], telegram_payment_charge_id=sale["charge_id"])
        except TelegramAPIError as err:
            log.warning("возврат отклонён Telegram: %s", err)
            return _err("telegram_refused", 400)
        db.mark_refunded(sale["id"])
        if sale["kind"] in ("vip_month", "vip_year"):
            db.grant_vip(sale["user_id"], -(VIP_MONTH_DAYS if sale["kind"] == "vip_month" else VIP_YEAR_DAYS))
        try:
            await bot.send_message(sale["user_id"], f"Оплата {sale['amount']} звёзд возвращена.")
        except TelegramAPIError:
            pass
        return _json({"ok": True})

    async def admin_stats(request: web.Request) -> web.Response:
        user, bad = staff_user(request, OWNER)
        if bad:
            return bad
        try:
            balance = (await bot.get_my_star_balance()).amount
        except TelegramAPIError:
            balance = None
        return _json({**db.counts(), "revenue": db.revenue(), "botBalance": balance})

    async def progress(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        try:
            body = await request.json()
            pos = float(body.get("position", 0))
            dur = float(body.get("duration", 0))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if not (0 <= pos < 1e7 and 0 <= dur < 1e7):
            return _err("bad_request", 400)
        db.save_progress(user["id"], film["id"], pos, dur)
        return _json({"ok": True})

    async def complete_view(request: web.Request) -> web.Response:
        """Засчитывает просмотр, когда зритель посмотрел 5 минут (а не досмотрел до конца:
        в конце титры, их смотрят не все). Фронтенд шлёт позицию, решает сервер.
        Один пользователь даёт фильму один просмотр."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        try:
            body = await request.json()
            pos = float(body.get("position", 0))
            dur = float(body.get("duration", 0)) or float(film["duration"] or 0)
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if not (0 <= pos < 1e7 and 0 <= dur < 1e7):
            return _err("bad_request", 400)
        counted = db.register_view(user["id"], film["id"], pos, dur)
        return _json({"ok": True, "counted": counted, "views": db.get_film(film["id"])["views"]})

    async def history(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        out = []
        for row in db.history(user["id"]):
            item = api.film_json(user, row)
            item["position"] = row["w_position"]
            item["watchDuration"] = row["w_duration"]
            item["progress"] = round(min(1.0, row["w_position"] / row["w_duration"]), 3) if row["w_duration"] else 0
            out.append(item)
        return _json(out)

    avatars_dir = cfg.media_dir.parent / "avatars"

    async def avatar(request: web.Request) -> web.Response:
        try:
            uid = int(request.match_info["uid"])
        except ValueError:
            return web.Response(status=404)
        path = avatars_dir / f"{uid}.jpg"
        missing = avatars_dir / f"{uid}.none"
        day = 86400
        if path.exists() and time.time() - path.stat().st_mtime < day:
            return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
        if missing.exists() and time.time() - missing.stat().st_mtime < day / 4:
            return web.Response(status=404)
        try:
            photos = await bot.get_user_profile_photos(uid, limit=1)
            if getattr(photos, "total_count", 0) and photos.photos:
                tg_file = await bot.get_file(photos.photos[0][-1].file_id)
                avatars_dir.mkdir(parents=True, exist_ok=True)
                await bot.download_file(tg_file.file_path, destination=path)
                return web.FileResponse(path, headers={"Cache-Control": "private, max-age=3600"})
        except Exception:  # нет доступа к фото, сеть, приватность — показываем заглушку
            pass
        avatars_dir.mkdir(parents=True, exist_ok=True)
        missing.touch()
        return web.Response(status=404)

    ad_dir = media_root / "ad"

    def ad_file(row: dict) -> Path:
        return ad_dir / f"{row['id']}{row['ext']}"

    def ad_id_of(request: web.Request) -> int | None:
        try:
            return int(request.match_info["id"])
        except ValueError:
            return None

    async def ad_video(request: web.Request) -> web.Response:
        row = db.get_ad(ad_id_of(request) or -1)
        if not row or not ad_file(row).exists():
            return web.Response(status=404)
        return web.FileResponse(ad_file(row), headers={"Cache-Control": "no-cache"})

    async def admin_ads_list(request: web.Request) -> web.Response:
        _, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        return _json([{"id": a["id"], "startSec": a["start_sec"], "link": a["link"]} for a in db.list_ads()])

    async def admin_ad_add(request: web.Request) -> web.Response:
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        try:
            body = await request.json()
            start = int(body.get("startSec", -1))
        except (ValueError, TypeError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if not (0 <= start <= 86400):
            return _err("bad_time", 400)
        link = str(body.get("link", "")).strip()
        if link and not URL_RE.match(link):
            return _err("bad_url", 400)
        uid = str(body.get("uploadId", "") or "")
        item = uploads.get(uid, user["id"])
        if not item:
            return _err("video_required", 400)
        if len(item["received"]) != item["total"]:
            return _err("upload_incomplete", 400)
        ext = Path(str(item.get("name", ""))).suffix.lower()
        if not re.fullmatch(r"\.[a-z0-9]{1,5}", ext):
            ext = ".mp4"
        ad_dir.mkdir(parents=True, exist_ok=True)
        ad_id = db.add_ad(start, link, ext)  # ролик не перекодируется, сохраняется как есть
        shutil.move(str(item["path"]), ad_dir / f"{ad_id}{ext}")
        uploads.discard(uid)
        return _json({"ok": True, "id": ad_id}, 201)

    async def admin_ad_delete(request: web.Request) -> web.Response:
        _, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        row = db.get_ad(ad_id_of(request) or -1)
        if not row:
            return _err("not_found", 404)
        ad_file(row).unlink(missing_ok=True)
        db.delete_ad(row["id"])
        return _json({"ok": True})

    # ---- избранное ----
    async def favorites_list(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ids = db.favorites(user["id"])
        out = []
        for film_id in ids:
            film = db.get_film(film_id)
            if film and film["is_active"]:
                out.append(api.film_json(user, film, favs=set(ids)))
        return _json(out)

    async def favorite_toggle(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        added = db.toggle_favorite(user["id"], film["id"])
        return _json({"ok": True, "favorite": added})

    # ---- тикеты поддержки ----
    def can_see_ticket(user: dict, ticket: dict) -> bool:
        """Пользователь видит свои тикеты, оператор — те, что ждут у операторов, редактор —
        переданные админам (зелёные на контент), админ и владелец — все."""
        role = user["role"]
        if at_least(role, ADMIN):
            return True
        if role == EDITOR:
            return ticket["assignee"] == TICKET_STAFF
        if role == OPERATOR:
            return ticket["assignee"] == TICKET_OPERATOR
        return ticket["user_id"] == user["id"]

    def visible_tickets(user: dict) -> list[dict]:
        role = user["role"]
        if at_least(role, ADMIN):
            rows = db.tickets()
        elif role == EDITOR:
            rows = db.tickets(assignee=TICKET_STAFF)
        elif role == OPERATOR:
            rows = db.tickets(assignee=TICKET_OPERATOR)
        else:
            rows = db.tickets(user_id=user["id"])
        if at_least(role, OPERATOR):  # у персонала бывают и свои личные тикеты
            seen = {t["id"] for t in rows}
            rows = rows + [t for t in db.tickets(user_id=user["id"]) if t["id"] not in seen]
        return rows

    def ticket_json(user: dict, t: dict, queue: dict[int, int], with_messages: bool = True) -> dict:
        data = {
            "id": t["id"], "kind": t["kind"], "kindTitle": TICKET_TITLES[t["kind"]],
            "text": t["text"], "createdAt": t["created_at"], "assignee": t["assignee"],
            "authorId": t["user_id"], "author": db.display_name(t["user_id"]),
            "queue": queue.get(t["id"]),      # номер в очереди; None — тикет уже передан админам
            "closed": t.get("status") == TICKET_CLOSED,
            "mine": t["user_id"] == user["id"],
            # кто что может сделать с тикетом: решает сервер, а не роли на клиенте
            "canClose": t["user_id"] == user["id"] or at_least(user["role"], OPERATOR),
            "canDelete": at_least(user["role"], OPERATOR),
            "canTransfer": user["role"] == OPERATOR and t["assignee"] == TICKET_OPERATOR,
        }
        # переписка нужна только в чате: список тикетов без неё в десятки раз легче
        if with_messages:
            data["messages"] = [{
                "id": m["id"], "text": m["text"], "date": m["created_at"],
                "image": f"/api/tickets/{t['id']}/images/{m['image']}" if m.get("image") else None,
                "mine": m["author_id"] == user["id"], "role": m["author_role"],
                "roleTitle": ROLE_TITLES.get(m["author_role"], ROLE_TITLES[OPERATOR]),
            } for m in db.ticket_messages(t["id"])]
        return data

    def ticket_or_error(request: web.Request, user: dict) -> tuple[dict | None, web.Response | None]:
        try:
            tid = int(request.match_info["id"])
        except (KeyError, ValueError):
            return None, _err("bad_id", 400)
        ticket = db.get_ticket(tid)
        if not ticket:
            return None, _err("not_found", 404)
        if not can_see_ticket(user, ticket):
            return None, _err("forbidden", 403)
        return ticket, None

    async def tickets_list(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        queue = db.ticket_queue()
        return _json({
            "kinds": [{"id": k, "title": TICKET_TITLES[k]} for k in TICKET_KINDS],
            "answer": TICKET_ANSWER,
            "tickets": [ticket_json(user, t, queue, with_messages=False)
                        for t in visible_tickets(user)],
        })

    async def ticket_one(request: web.Request) -> web.Response:
        """Один тикет для чата. Раньше чат открывался списком всех тикетов вместе с
        перепиской каждого — отсюда и долгое открытие."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        return _json(ticket_json(user, ticket, db.ticket_queue()))

    async def ticket_create(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        kind = str(body.get("kind", "")).strip().lower()
        if kind not in TICKET_KINDS:
            return _err("bad_kind", 400)
        text = str(body.get("text", "")).strip()[:2000]
        if not text:
            return _err("empty", 400)
        ticket_id = db.add_ticket(user["id"], kind, text)
        return _json({"ok": True, "id": ticket_id, "answer": TICKET_ANSWER}, 201)

    async def ticket_reply(request: web.Request) -> web.Response:
        """Сообщение в тикет. Персонал отвечает от имени Оператора."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if ticket.get("status") == TICKET_CLOSED:
            return _err("ticket_closed", 400)
        text = str(body.get("text", "")).strip()[:2000]
        image = str(body.get("image", "") or "").strip()
        if not text and not image:
            return _err("empty", 400)
        if image and not (tickets_dir / str(ticket["id"]) / image).is_file():
            return _err("bad_image", 400)
        message_id = db.add_ticket_message(ticket["id"], user["id"], user["role"], text, image or None)
        row = db.get_ticket_message(message_id)
        # ответ персонала уходит пользователю в Telegram; если он заблокировал бота —
        # тикеты удаляются сразу (держать переписку с тем, кого нет в боте, незачем)
        if user["id"] != ticket["user_id"]:
            try:
                await bot.send_message(ticket["user_id"],
                                       f"Ответ на ваш тикет #{ticket['id']}:\n{text[:300]}")
            except TelegramForbiddenError:
                db.delete_user_tickets(ticket["user_id"])
                return _err("user_left", 410)
            except TelegramAPIError:
                pass
        return _json({"ok": True, "message": {
            "id": row["id"], "text": row["text"], "date": row["created_at"],
            "image": f"/api/tickets/{ticket['id']}/images/{row['image']}" if row["image"] else None,
            "mine": True, "role": user["role"],
            "roleTitle": ROLE_TITLES.get(user["role"], ROLE_TITLES[OPERATOR]),
        }})

    async def ticket_image(request: web.Request) -> web.Response:
        """Картинка к сообщению тикета: приходит в base64 внутри обычного JSON-запроса.
        Тип проверяется по содержимому, размер — до 10 МБ."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        if ticket.get("status") == TICKET_CLOSED:
            return _err("ticket_closed", 400)
        # base64 примерно в полтора раза больше самой картинки: больше 20 МБ в запросе быть не должно
        if request.content_length and request.content_length > TICKET_IMAGE_MAX * 2:
            return _err("too_large", 413)
        try:
            body = await request.json()
        except (ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        raw = str(body.get("image", "") or "").strip()
        if not raw:
            return _err("bad_image", 400)
        if raw.startswith("data:"):                      # data:image/png;base64,....
            head, _, raw = raw.partition(",")
            if not head[5:].split(";")[0].strip().lower().startswith("image/"):
                return _err("bad_image", 400)
        try:
            data = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            return _err("bad_image", 400)
        ext = sniff_image(data)
        if ext is None:
            return _err("bad_image", 400)
        if len(data) > TICKET_IMAGE_MAX:
            return _err("too_large", 413)
        folder = tickets_dir / str(ticket["id"])
        folder.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{ext}"
        with open(folder / name, "wb") as fh:
            fh.write(data)
        return _json({"ok": True, "name": name, "url": f"/api/tickets/{ticket['id']}/images/{name}"})

    async def ticket_image_get(request: web.Request) -> web.StreamResponse:
        user = api.user_of(request)
        if not user:
            return web.Response(status=401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        folder = (tickets_dir / str(ticket["id"])).resolve()
        target = (folder / request.match_info["name"]).resolve()
        if folder not in target.parents or not target.is_file():
            return web.Response(status=404)
        return web.FileResponse(target, headers={"Cache-Control": "private, max-age=3600"})

    async def ticket_close(request: web.Request) -> web.Response:
        """Закрыть тикет: он закрывается у обеих сторон и через 10 минут удаляется из базы."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        if ticket["user_id"] != user["id"] and not at_least(user["role"], OPERATOR):
            return _err("forbidden", 403)
        db.close_ticket(ticket["id"])
        return _json({"ok": True, "id": ticket["id"], "ttl": TICKET_TTL})

    async def ticket_transfer(request: web.Request) -> web.Response:
        """Оператор передаёт тикет админам и владельцу; админ или владелец возвращает операторам."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        try:
            body = await request.json()
            to = str(body.get("to", "")).strip()
        except (ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if to not in (TICKET_OPERATOR, TICKET_STAFF):
            return _err("bad_request", 400)
        if to == TICKET_STAFF and user["role"] != OPERATOR:
            return _err("forbidden", 403)      # передаёт админам только оператор
        if to == TICKET_OPERATOR and not at_least(user["role"], ADMIN):
            return _err("forbidden", 403)
        db.set_ticket_assignee(ticket["id"], to)
        if to == TICKET_STAFF:
            for uid in set(db.staff_ids(ADMIN)) | {cfg.owner_id}:
                try:
                    await bot.send_message(
                        uid, f"Тикет #{ticket['id']} ({TICKET_TITLES[ticket['kind']]}) передан вам.\n"
                             f"От: {db.display_name(ticket['user_id'])} ({ticket['user_id']})\n"
                             f"{ticket['text'][:300]}")
                except TelegramAPIError:
                    pass
        return _json({"ok": True, "assignee": to})

    async def ticket_delete(request: web.Request) -> web.Response:
        """«Удалить» есть только у оператора, редактора, админа и владельца: тикет стирается
        из базы сразу, вместе с перепиской и картинками. Автор тикета удалять не может —
        у него есть «Закрыть», и такой тикет удалится сам через 10 минут."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        ticket, bad = ticket_or_error(request, user)
        if bad:
            return bad
        if not at_least(user["role"], OPERATOR):
            return _err("forbidden", 403)
        db.delete_ticket(ticket["id"])
        shutil.rmtree(tickets_dir / str(ticket["id"]), ignore_errors=True)   # картинки тоже удаляем
        return _json({"ok": True, "id": ticket["id"], "immediate": True})

    async def tour_seen(request: web.Request) -> web.Response:
        """Подсказки по кнопкам показаны один раз и больше не показываются никогда."""
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        db.set_tour_seen(user["id"])
        return _json({"ok": True})

    async def admin_ban(request: web.Request) -> web.Response:
        """Бан: сохраняется в базе, после перезахода в бота не снимается.
        Забаненному закрыт весь сайт, кроме поддержки — там он пишет апелляцию."""
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        try:
            body = await request.json()
            target_id = int(body.get("id"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if target_id == cfg.owner_id:
            return _err("owner", 400)
        banned = bool(body.get("banned", True))
        db.set_banned(target_id, banned)
        row = db.get_user(target_id) or {}
        return _json({"ok": True, "id": target_id, "banned": bool(row.get("banned"))})

    routes = [
        ("GET", "/health", health),
        ("GET", "/", page),
        ("GET", "/app", page),
        ("GET", "/static/hls.min.js", static_hls),
        ("GET", "/api/config", config),
        ("GET", "/api/me", me),
        ("GET", "/api/films", films),
        ("GET", "/api/films/{film_id}", film_detail),
        ("GET", "/api/films/{film_id}/poster", poster),
        ("POST", "/api/films/{film_id}/play", play),
        ("GET", "/api/media/{film_id}/{token}/{path:.+}", media),
        ("POST", "/api/films/{film_id}/reviews", add_review),
        ("POST", "/api/films/{film_id}/progress", progress),
        ("POST", "/api/films/{film_id}/complete", complete_view),
        ("GET", "/api/me/history", history),
        ("GET", "/api/avatar/{uid}", avatar),
        ("GET", "/api/ad/{id}/video", ad_video),
        ("GET", "/api/admin/ads", admin_ads_list),
        ("POST", "/api/admin/ads", admin_ad_add),
        ("POST", "/api/admin/ads/{id}/delete", admin_ad_delete),
        ("POST", "/api/reviews/{id}/delete", delete_review),
        ("POST", "/api/me/style", my_style),
        ("POST", "/api/pay/invoice", invoice),
        ("POST", "/api/pay/stars", pay_stars),
        ("GET", "/api/admin/films", admin_films),
        ("POST", "/api/admin/films", admin_create),
        ("POST", "/api/admin/uploads", upload_init),
        ("PUT", "/api/admin/uploads/{uid}/{index}", upload_chunk),
        ("POST", "/api/admin/films/{id}/update", admin_update),
        ("POST", "/api/admin/films/{id}/poster", admin_poster),
        ("POST", "/api/admin/films/{id}/delete", admin_delete),
        ("POST", "/api/admin/series", admin_series_create),
        ("POST", "/api/admin/films/{id}/reprocess", admin_reprocess),
        ("POST", "/api/admin/films/{id}/subtitles", admin_subtitle_add),
        ("POST", "/api/admin/films/{id}/subtitles/{lang}/delete", admin_subtitle_delete),
        ("GET", "/api/admin/users", admin_users),
        ("POST", "/api/admin/role", admin_role),
        ("POST", "/api/admin/vip", admin_vip),
        ("POST", "/api/admin/stars", admin_stars),
        ("GET", "/api/admin/sales", admin_sales),
        ("POST", "/api/admin/sales/{id}/refund", admin_refund),
        ("GET", "/api/admin/stats", admin_stats),
        ("GET", "/api/tickets", tickets_list),
        ("GET", "/api/tickets/{id}", ticket_one),
        ("POST", "/api/tickets", ticket_create),
        ("POST", "/api/tickets/{id}/messages", ticket_reply),
        ("POST", "/api/tickets/{id}/transfer", ticket_transfer),
        ("POST", "/api/tickets/{id}/delete", ticket_delete),
        ("POST", "/api/tickets/{id}/close", ticket_close),
        ("POST", "/api/tickets/{id}/images", ticket_image),
        ("POST", "/api/tour", tour_seen),
        ("GET", "/api/tickets/{id}/images/{name}", ticket_image_get),
        ("GET", "/api/favorites", favorites_list),
        ("POST", "/api/films/{film_id}/favorite", favorite_toggle),
        ("POST", "/api/admin/ban", admin_ban),
    ]
    for method, path, handler in routes:
        app.router.add_route(method, path, handler)

    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret).register(
        app, path=cfg.webhook_path
    )
    return app


# ============================================================================
# Запуск
# ============================================================================
async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    db = Database(cfg.db_path)
    cfg.media_dir.mkdir(parents=True, exist_ok=True)
    shutil.rmtree(cfg.media_dir / "tmp", ignore_errors=True)  # остатки прерванных загрузок

    for admin_id in cfg.initial_admins:
        if admin_id != cfg.owner_id and db.get_user(admin_id) is None:
            db.set_role(admin_id, ADMIN, added_by=cfg.owner_id)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.update.outer_middleware(RoleMiddleware(db, cfg.owner_id))
    transcoder = Transcoder(bot, db, cfg.media_dir)
    dp.include_router(build_router(cfg, db, transcoder))

    me = await bot.get_me()
    log.info("бот @%s, владелец: %s, режим: %s, БД: %s", me.username, cfg.owner_id, cfg.mode, cfg.db_path)

    # видео, которые не обработаны (после обновления или сбоя), ставим в очередь заново;
    # оставшиеся без исходника сразу помечаем failed — иначе каждый перезапуск спамит ошибками
    lost = mark_lost_sources(db, cfg.media_dir)
    if lost:
        log.warning("нет исходного видео, помечены как failed: %s", lost)
    for film in db.list_video_films():
        folder = cfg.media_dir / str(film["id"])
        if film["status"] != "ready" or not (folder / "master.m3u8").exists():
            if film["status"] != "failed" or film["video_file_id"]:
                transcoder.enqueue(film["id"])
        elif not hls_ready(folder) and (folder / "source.mp4").exists():
            # старый набор качеств: пересобираем из исходника, старое видео играет до замены
            transcoder.queue.put_nowait(film["id"])
    transcoder.start()
    storage_task = asyncio.create_task(storage_loop(db, cfg.media_dir))
    tickets_task = asyncio.create_task(tickets_loop(db, cfg.media_dir))

    await bot.set_my_commands([BotCommand(command="start", description="Открыть")])
    if cfg.webapp_url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Аниме", web_app=WebAppInfo(url=cfg.webapp_url)))

    runner = web.AppRunner(build_web(cfg, db, bot, dp, transcoder, me.username))
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", cfg.port).start()
    log.info("HTTP слушает 0.0.0.0:%s", cfg.port)

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    poller: asyncio.Task | None = None
    try:
        if cfg.mode == "webhook":
            url = f"{cfg.public_url}{cfg.webhook_path}"
            await bot.set_webhook(url, secret_token=cfg.webhook_secret,
                                  allowed_updates=dp.resolve_used_update_types(), drop_pending_updates=False)
            log.info("webhook установлен: %s", url)
        else:
            await bot.delete_webhook(drop_pending_updates=False)
            poller = asyncio.create_task(
                dp.start_polling(bot, handle_signals=False, allowed_updates=dp.resolve_used_update_types())
            )
        await stop.wait()
    finally:
        log.info("останавливаюсь")
        await transcoder.stop()
        storage_task.cancel()
        tickets_task.cancel()
        if poller:
            poller.cancel()
        await runner.cleanup()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

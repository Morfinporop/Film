#!/usr/bin/env python3
"""
Видеохостинг фильмов в Telegram: бот-вход, Mini App, VIP-подписка за Telegram Stars.
Весь проект запускается одним файлом: python main.py

Бот: /start с текстом и кнопкой Mini App. Видео, отправленное админом боту, становится фильмом.
Mini App: каталог, просмотр в HLS с выбором качества, отзывы, комментарии, VIP, админ-панель.

Роли: owner (OWNER_ID) > admin > editor > user.
  owner  — всё, получает звёзды, возвраты, баланс бота.
  admin  — фильмы любые, выдаёт редакторов, VIP, видит пользователей.
  editor — загружает фильмы, управляет только своими.
  user   — смотрит; VIP-фильмы открыты по подписке.
"""

from __future__ import annotations

import asyncio
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
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.types import (
    BotCommand,
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
LADDER = (144, 240, 360, 480, 720, 1080)        # качества HLS: от 144p до 1080p
BITRATE_KBPS = {144: 120, 240: 300, 360: 800, 480: 1400, 720: 2500, 1080: 5000}
AUDIO_KBPS = {144: 64, 240: 64, 360: 96}
BOT_UPLOAD_LIMIT = 20 * 1024 * 1024             # лимит Bot API на скачивание файла (cloud API)
UPLOAD_LIMIT = 2 * 1024 ** 3                    # максимум файла при загрузке через Mini App: 2 ГБ
CHUNK_SIZE = 8 * 1024 * 1024                    # видео уходит частями: прокси не держит долгие запросы
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa", ".sub"}
VIP_MONTH_DAYS, VIP_YEAR_DAYS = 30, 365
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
TIKTOK_RE = re.compile(r"tiktok\.com/(?:@[^/]+/video/|v/|embed/(?:v2/)?)(\d+)")
YOUTUBE_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})")

log = logging.getLogger("film-hub")


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
    editor_price: int
    admin_price: int

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
        vip_month=_price(env, "VIP_MONTH_PRICE", 100),
        vip_year=_price(env, "VIP_YEAR_PRICE", 900),
        editor_price=_price(env, "EDITOR_PRICE", 500),
        admin_price=_price(env, "ADMIN_PRICE", 2000),
    )


# ============================================================================
# Роли и права
# ============================================================================
OWNER, ADMIN, EDITOR, USER = "owner", "admin", "editor", "user"
RANK = {USER: 0, EDITOR: 1, ADMIN: 2, OWNER: 3}
ROLE_TITLES = {OWNER: "Владелец", ADMIN: "Админ", EDITOR: "Редактор", USER: "Пользователь"}


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
    kind          TEXT    NOT NULL CHECK (kind IN ('link', 'video')),
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
    added_by    INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS reviews (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    rating     INTEGER NOT NULL CHECK (rating BETWEEN 1 AND 5),
    text       TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL DEFAULT (datetime('now')),
    UNIQUE (film_id, user_id)
);

CREATE TABLE IF NOT EXISTS comments (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    film_id    INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    text       TEXT    NOT NULL,
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
"""

INDEXES = """
CREATE INDEX IF NOT EXISTS idx_films_active ON films (is_active, id);
CREATE INDEX IF NOT EXISTS idx_films_owner ON films (owner_id);
CREATE INDEX IF NOT EXISTS idx_reviews_film ON reviews (film_id);
CREATE INDEX IF NOT EXISTS idx_comments_film ON comments (film_id, id);
CREATE INDEX IF NOT EXISTS idx_sales_refund ON sales (refunded, id);
"""

# колонки, которых могло не быть в ранних версиях
FILM_COLUMNS = {
    "owner_id": "INTEGER",
    "description": "TEXT NOT NULL DEFAULT ''",
    "release_date": "TEXT NOT NULL DEFAULT ''",
    "creator": "TEXT NOT NULL DEFAULT ''",
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
}


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
        if old_films_sql and "video_file_id IS NOT NULL" in old_films_sql:
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

        self.conn.executescript(TABLES)
        for column, ddl in FILM_COLUMNS.items():
            if column not in self._columns("films"):
                self.conn.execute(f"ALTER TABLE films ADD COLUMN {column} {ddl}")
        if "vip_until" not in self._columns("users"):
            self.conn.execute("ALTER TABLE users ADD COLUMN vip_until TEXT")
        self.conn.executescript(INDEXES)

    # ---- пользователи ----
    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def touch_user(self, tg_id: int, username: str | None, first_name: str | None) -> None:
        self.conn.execute(
            """INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username,
                 first_name = excluded.first_name, last_seen = datetime('now')""",
            (tg_id, username, first_name),
        )

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

    def staff(self) -> list[dict]:
        return self._all("SELECT * FROM users WHERE role IN ('admin', 'editor') ORDER BY role, tg_id")

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
        current = parse_dt((self.get_user(tg_id) or {}).get("vip_until")) or now
        new_until = max(current, now) + timedelta(days=days)
        new_until = max(new_until, now)
        self.conn.execute("UPDATE users SET vip_until = ? WHERE tg_id = ?", (fmt_dt(new_until), tg_id))
        return new_until

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
                   "url", "status", "error", "duration", "width", "height", "poster"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return
        sets = ", ".join(f"{k} = ?" for k, _ in items)
        self.conn.execute(f"UPDATE films SET {sets} WHERE id = ?", (*[v for _, v in items], film_id))

    def set_status(self, film_id: int, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE films SET status = ?, error = ? WHERE id = ?", (status, error, film_id))

    def delete_film(self, film_id: int) -> None:
        self.conn.execute("DELETE FROM reviews WHERE film_id = ?", (film_id,))
        self.conn.execute("DELETE FROM comments WHERE film_id = ?", (film_id,))
        self.conn.execute("DELETE FROM films WHERE id = ?", (film_id,))

    def list_films(self, *, public: bool = True, owner_id: int | None = None, limit: int = 500) -> list[dict]:
        where, params = [], []
        if public:
            where.append("is_active = 1 AND status = 'ready'")
        if owner_id is not None:
            where.append("owner_id = ?")
            params.append(owner_id)
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        return self._all(f"SELECT * FROM films{clause} ORDER BY id DESC LIMIT ?", (*params, limit))

    def list_video_films(self) -> list[dict]:
        return self._all("SELECT * FROM films WHERE kind = 'video' ORDER BY id")

    def add_view(self, film_id: int) -> None:
        self.conn.execute("UPDATE films SET views = views + 1 WHERE id = ?", (film_id,))

    # ---- отзывы и комментарии ----
    def upsert_review(self, film_id: int, user_id: int, rating: int, text: str) -> None:
        self.conn.execute(
            """INSERT INTO reviews (film_id, user_id, rating, text) VALUES (?, ?, ?, ?)
               ON CONFLICT(film_id, user_id) DO UPDATE SET rating = excluded.rating,
                 text = excluded.text, created_at = datetime('now')""",
            (film_id, user_id, rating, text),
        )

    def reviews(self, film_id: int) -> list[dict]:
        return self._all("SELECT * FROM reviews WHERE film_id = ? ORDER BY id DESC LIMIT 100", (film_id,))

    def rating(self, film_id: int) -> tuple[float, int]:
        row = self._one("SELECT AVG(rating) AS avg, COUNT(*) AS n FROM reviews WHERE film_id = ?", (film_id,))
        return (round(row["avg"] or 0, 1), row["n"]) if row else (0.0, 0)

    def get_review(self, review_id: int) -> dict | None:
        return self._one("SELECT * FROM reviews WHERE id = ?", (review_id,))

    def delete_review(self, review_id: int) -> None:
        self.conn.execute("DELETE FROM reviews WHERE id = ?", (review_id,))

    def add_comment(self, film_id: int, user_id: int, text: str) -> int:
        return int(self.conn.execute(
            "INSERT INTO comments (film_id, user_id, text) VALUES (?, ?, ?)", (film_id, user_id, text)
        ).lastrowid)

    def comments(self, film_id: int) -> list[dict]:
        return self._all("SELECT * FROM comments WHERE film_id = ? ORDER BY id DESC LIMIT 200", (film_id,))

    def comment_count(self, film_id: int) -> int:
        return self._one("SELECT COUNT(*) AS n FROM comments WHERE film_id = ?", (film_id,))["n"]

    def get_comment(self, comment_id: int) -> dict | None:
        return self._one("SELECT * FROM comments WHERE id = ?", (comment_id,))

    def delete_comment(self, comment_id: int) -> None:
        self.conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))

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
    _, err = await proc.communicate()
    return err


async def subtitle_streams(src: Path) -> list[str]:
    """Коды языков встроенных текстовых субтитров по порядку (0:s:0, 0:s:1...)."""
    err = (await _run(["-hide_banner", "-i", str(src)])).decode(errors="replace")
    langs = []
    for m in re.finditer(r"Stream #0:\d+(?:\((\w+)\))?.*?: Subtitle: (\w+)", err):
        langs.append((m.group(1) or "und").lower() if m.group(2) not in ("hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle") else "")
    return langs


ISO3 = {"rus": "ru", "eng": "en", "ukr": "uk", "deu": "de", "ger": "de", "fra": "fr", "fre": "fr",
        "spa": "es", "ita": "it", "tur": "tr", "jpn": "ja", "kor": "ko", "zho": "zh", "chi": "zh"}


def sub_name(lang: str) -> str:
    code = re.sub(r"[^a-z0-9-]", "", lang.lower())[:12]
    return ISO3.get(code, code) or "und"


async def extract_subtitles(src: Path, out_dir: Path) -> None:
    """Извлекает встроенные текстовые субтитры в WebVTT. Растровые (PGS/DVD) пропускаются."""
    out_dir.mkdir(parents=True, exist_ok=True)
    for k, lang in enumerate(await subtitle_streams(src)):
        if not lang:
            continue
        name = sub_name(lang)
        target = out_dir / f"{name}.vtt"
        n = 2
        while target.exists():  # несколько дорожек одного языка
            target = out_dir / f"{name}{n}.vtt"
            n += 1
        await _run(["-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                    "-map", f"0:s:{k}", "-c:s", "webvtt", str(target)])
        if not target.exists() or target.stat().st_size == 0:
            target.unlink(missing_ok=True)


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
    """Одна фоновая очередь: скачивает файл из Telegram (если нужно) и строит HLS-качества."""

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

        ladder = [h for h in LADDER if h <= height] or [LADDER[0]]
        await extract_subtitles(src, folder / "subs")
        variants: list[tuple[int, int, int]] = []
        for h in ladder:
            if self.db.get_film(film_id) is None:  # фильм удалили во время обработки
                return
            w = max(2, round(width * h / height / 2) * 2)
            out = folder / f"{h}p"
            out.mkdir(exist_ok=True)
            kbps = BITRATE_KBPS[h]
            akbps = AUDIO_KBPS.get(h, 128)
            err = await _run([
                "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", f"scale={w}:{h}",
                "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high",
                "-b:v", f"{kbps}k", "-maxrate", f"{int(kbps * 1.1)}k", "-bufsize", f"{kbps * 2}k",
                "-g", "48", "-keyint_min", "48", "-sc_threshold", "0",
                "-c:a", "aac", "-b:a", f"{akbps}k", "-ac", "2",
                "-f", "hls", "-hls_time", "6", "-hls_playlist_type", "vod",
                "-hls_segment_filename", str(out / "seg_%04d.ts"),
                str(out / "index.m3u8"),
            ])
            if self.db.get_film(film_id) is None:
                return
            if not (out / "index.m3u8").exists():
                raise RuntimeError(f"Не удалось собрать {h}p: {err.decode(errors='replace')[-200:]}")
            variants.append((h, w, kbps))

        master = ["#EXTM3U", "#EXT-X-VERSION:3"]
        for h, w, kbps in variants:
            master.append(f'#EXT-X-STREAM-INF:BANDWIDTH={(kbps + 128) * 1000},RESOLUTION={w}x{h},NAME="{h}p"')
            master.append(f"{h}p/index.m3u8")
        (folder / "master.m3u8").write_text("\n".join(master) + "\n", encoding="utf-8")

        self.db.update_film(film_id, status="ready", error=None, duration=duration, width=width, height=height)
        log.info("фильм %s готов: %s", film_id, [v[0] for v in variants])


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
    m = TIKTOK_RE.search(url)
    if m:
        return f"https://www.tiktok.com/embed/v2/{m.group(1)}"
    m = YOUTUBE_RE.search(url)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    return url


# ============================================================================
# Авторизация Mini App (подпись initData)
# ============================================================================
def verify_init_data(init_data: str, token: str, max_age: int = 86400) -> dict | None:
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
    if time.time() - int(pairs.get("auth_date", "0") or 0) > max_age:
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
    "Здравствуйте, искатель фильмов! Вы попали к нам удачно. "
    "Здесь множество фильмов и сериалов, в том числе озвученных с помощью ИИ, как в TikTok. "
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
    "role_editor": "Права редактора",
    "role_admin": "Права админа",
}


def parse_payload(payload: str) -> tuple[str, int]:
    """Payload инвойса: 'vip_month', 'vip_year', 'donate:<n>', 'role_editor', 'role_admin'."""
    kind, _, arg = payload.partition(":")
    return kind, int(arg) if arg.isdigit() else 0


def expected_amount(cfg: Config, kind: str, arg: int) -> int | None:
    return {
        "vip_month": cfg.vip_month,
        "vip_year": cfg.vip_year,
        "role_editor": cfg.editor_price,
        "role_admin": cfg.admin_price,
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
            f"Фильм #{film_id} принят и обрабатывается в качестве 360p, 720p, 1080p. "
            "Заполните описание и обложку в Админ-панели.",
            reply_markup=webapp_kb(f"{cfg.webapp_url}#admin/film/{film_id}") if cfg.webapp_url else None,
        )

    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot, role: str) -> None:
        kind, arg = parse_payload(query.invoice_payload)
        expected = expected_amount(cfg, kind, arg)
        ok, error = True, None
        if expected is None or expected != query.total_amount:
            ok, error = False, "Сумма оплаты не совпадает с ценой"
        elif kind == "role_editor" and at_least(role, EDITOR):
            ok, error = False, "У вас уже есть эта роль"
        elif kind == "role_admin" and at_least(role, ADMIN):
            ok, error = False, "У вас уже есть эта роль"
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
    """Эффект оплаты: продление VIP или выдача роли. Донат — только в статистику."""
    if kind == "vip_month":
        db.grant_vip(uid, VIP_MONTH_DAYS)
    elif kind == "vip_year":
        db.grant_vip(uid, VIP_YEAR_DAYS)
    elif kind in ("role_editor", "role_admin"):
        target = EDITOR if kind == "role_editor" else ADMIN
        if RANK[db.get_role(uid)] < RANK[target]:
            db.set_role(uid, target, added_by=cfg.owner_id)


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
        user = verify_init_data(request.headers.get("X-Init-Data", ""), self.cfg.bot_token)
        if not user:
            return None
        uid = int(user["id"])
        self.db.touch_user(uid, user.get("username"), user.get("first_name"))
        role = OWNER if uid == self.cfg.owner_id else self.db.get_role(uid)
        row = self.db.get_user(uid) or {}
        return {"id": uid, "name": user.get("first_name") or user.get("username") or "Пользователь",
                "role": role, "vip_until": row.get("vip_until")}

    def vip_active(self, user: dict | None) -> bool:
        if not user:
            return False
        if at_least(user["role"], ADMIN):
            return True
        until = parse_dt(user.get("vip_until"))
        return bool(until and until > utcnow())

    def locked(self, user: dict | None, film: dict) -> bool:
        return bool(film["is_vip"]) and not self.vip_active(user)

    def film_json(self, user: dict | None, film: dict, detail: bool = False) -> dict:
        rating, reviews_n = self.db.rating(film["id"])
        locked = self.locked(user, film)
        data = {
            "id": film["id"],
            "title": film["title"],
            "description": film["description"],
            "releaseDate": film["release_date"],
            "creator": film["creator"],
            "kind": film["kind"],
            "isVip": bool(film["is_vip"]),
            "locked": locked,
            "views": film["views"],
            "duration": film["duration"],
            "width": film["width"],
            "height": film["height"],
            "status": film["status"],
            "hasPoster": bool(film["poster"] or film["photo_file_id"]),
            "rating": rating,
            "reviewCount": reviews_n,
            "commentCount": self.db.comment_count(film["id"]),
            "isMine": bool(user and film.get("owner_id") == user["id"]),
        }
        if film["kind"] == "link" and not locked:
            data["url"] = film["url"]
        if detail:
            data["reviews"] = [
                {"id": r["id"], "name": self.db.display_name(r["user_id"]), "rating": r["rating"],
                 "text": r["text"], "date": r["created_at"],
                 "canDelete": bool(user and (r["user_id"] == user["id"] or at_least(user["role"], ADMIN)))}
                for r in self.db.reviews(film["id"])
            ]
            data["comments"] = [
                {"id": c["id"], "name": self.db.display_name(c["user_id"]), "text": c["text"],
                 "date": c["created_at"],
                 "canDelete": bool(user and (c["user_id"] == user["id"] or at_least(user["role"], ADMIN)))}
                for c in self.db.comments(film["id"])
            ]
        return data

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


IMAGE_EXT = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _poster_ext(filename: str) -> str:
    ext = Path(filename).suffix.lower()
    return ext if ext in (".jpg", ".jpeg", ".png", ".webp") else ".jpg"


def build_web(cfg: Config, db: Database, bot: Bot, dp: Dispatcher,
              transcoder: Transcoder, bot_username: str | None) -> web.Application:
    app = web.Application(client_max_size=UPLOAD_LIMIT + 16 * 1024 * 1024)
    api = Api(cfg, db, bot, transcoder)
    media_root = cfg.media_dir
    uploads = ChunkUploads(media_root / "tmp")

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
            "prices": {"vipMonth": cfg.vip_month, "vipYear": cfg.vip_year,
                       "editor": cfg.editor_price, "admin": cfg.admin_price},
            "donate": list(DONATE_OPTIONS),
        })

    async def me(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _json({"guest": True})
        return _json({
            "guest": False, "id": user["id"], "name": user["name"], "role": user["role"],
            "roleTitle": ROLE_TITLES[user["role"]], "vipUntil": user["vip_until"],
            "vipActive": api.vip_active(user),
            "canUpload": at_least(user["role"], EDITOR),
            "canManage": at_least(user["role"], ADMIN),
            "isOwner": user["role"] == OWNER,
        })

    async def films(request: web.Request) -> web.Response:
        user = api.user_of(request)
        return _json([api.film_json(user, f) for f in db.list_films()])

    async def film_detail(request: web.Request) -> web.Response:
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        user = api.user_of(request)
        return _json(api.film_json(user, film, detail=True))

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
        if film["kind"] == "video" and film["status"] != "ready":
            return _err("processing", 202)
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        if api.locked(user, film):
            return _json({"error": "vip_required"}, 403)
        db.add_view(film["id"])
        if film["kind"] == "link":
            return _json({"kind": "link", "src": embed_url(film["url"]), "url": film["url"]})
        token = make_media_token(api.secret, film["id"])
        subs = [{"lang": n, "label": SUB_LABELS.get(n.rstrip("0123456789"), n.upper()),
                 "src": f"/api/media/{film['id']}/{token}/subs/{n}.vtt"}
                for n in list_subtitles(media_root / str(film["id"]))]
        return _json({"kind": "hls", "src": f"/api/media/{film['id']}/{token}/master.m3u8", "subtitles": subs,
                      "width": film["width"], "height": film["height"]})

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
        db.upsert_review(film["id"], user["id"], rating, text)
        return _json({"ok": True})

    async def add_comment(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        film = _film_or_none(request)
        if not film:
            return _err("not_found", 404)
        if api.locked(user, film):
            return _err("vip_required", 403)
        try:
            text = str((await request.json()).get("text", "")).strip()[:1000]
        except (ValueError, json.JSONDecodeError):
            return _err("bad_request", 400)
        if not text:
            return _err("empty", 400)
        db.add_comment(film["id"], user["id"], text)
        return _json({"ok": True})

    async def delete_comment(request: web.Request) -> web.Response:
        user = api.user_of(request)
        if not user:
            return _err("auth", 401)
        comment = db.get_comment(int(request.match_info["id"]))
        if not comment:
            return _err("not_found", 404)
        if comment["user_id"] != user["id"] and not at_least(user["role"], ADMIN):
            return _err("forbidden", 403)
        db.delete_comment(comment["id"])
        return _json({"ok": True})

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
        elif kind in ("vip_month", "vip_year", "role_editor", "role_admin"):
            payload_arg = 0
        else:
            return _err("bad_request", 400)
        expected = expected_amount(cfg, kind, payload_arg)
        if expected is None:
            return _err("bad_amount", 400)
        if kind == "role_editor" and at_least(user["role"], EDITOR):
            return _err("already", 400)
        if kind == "role_admin" and at_least(user["role"], ADMIN):
            return _err("already", 400)
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
        return _json([{**api.film_json(user, f), "error": f["error"], "isActive": bool(f["is_active"]),
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
        with open(item["path"], "r+b") as fh:
            fh.seek(start)
            async for part in request.content.iter_chunked(1 << 20):
                got += len(part)
                if got > expected:
                    return _err("bad_chunk", 400)
                fh.write(part)
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
            if kind == "link" and not URL_RE.match(fields.get("url", "")):
                return _err("bad_url", 400)
            upload_item = None
            if kind == "video":
                upload_item = uploads.get(fields.get("uploadId", ""), user["id"])
                if not upload_item:
                    return _err("video_required", 400)
                if len(upload_item["received"]) != upload_item["total"]:
                    return _err("upload_incomplete", 400)
            if kind not in ("video", "link"):
                return _err("bad_kind", 400)
            film_id = db.create_film(
                title=title,
                kind=kind,
                url=fields.get("url") or None,
                owner_id=user["id"],
                description=fields.get("description", "")[:4000],
                release_date=fields.get("releaseDate", "")[:10],
                creator=fields.get("creator", "")[:120],
                is_vip=1 if fields.get("isVip") in ("1", "true", "on") else 0,
                status="ready" if kind == "link" else "processing",
            )
            folder = media_root / str(film_id)
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
        if "url" in body and film["kind"] == "link":
            if not URL_RE.match(str(body["url"])):
                return _err("bad_url", 400)
            fields["url"] = str(body["url"])
        if "isVip" in body:
            fields["is_vip"] = 1 if body["isVip"] else 0
        if "isActive" in body:
            fields["is_active"] = 1 if body["isActive"] else 0
        db.update_film(film["id"], **fields)
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
        db.delete_film(film["id"])
        shutil.rmtree(media_root / str(film["id"]), ignore_errors=True)
        return _json({"ok": True})

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
            "vipUntil": r["vip_until"], "vipActive": bool((parse_dt(r["vip_until"]) or datetime.min.replace(tzinfo=timezone.utc)) > utcnow()),
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
        if new_role not in (USER, EDITOR, ADMIN):
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
        user, bad = staff_user(request, ADMIN)
        if bad:
            return bad
        body = await request.json()
        try:
            target_id = int(body.get("id"))
            days = int(body.get("days"))
        except (TypeError, ValueError):
            return _err("bad_request", 400)
        if days == 0 or abs(days) > 3650:
            return _err("bad_days", 400)
        until = db.grant_vip(target_id, days)
        try:
            text = (f"Вам выдан VIP на {days} дн." if days > 0 else "VIP отозван.")
            await bot.send_message(target_id, text)
        except TelegramAPIError:
            pass
        return _json({"ok": True, "vipUntil": fmt_dt(until)})

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
        ("POST", "/api/films/{film_id}/comments", add_comment),
        ("POST", "/api/comments/{id}/delete", delete_comment),
        ("POST", "/api/reviews/{id}/delete", delete_review),
        ("POST", "/api/pay/invoice", invoice),
        ("GET", "/api/admin/films", admin_films),
        ("POST", "/api/admin/films", admin_create),
        ("POST", "/api/admin/uploads", upload_init),
        ("PUT", "/api/admin/uploads/{uid}/{index}", upload_chunk),
        ("POST", "/api/admin/films/{id}/update", admin_update),
        ("POST", "/api/admin/films/{id}/poster", admin_poster),
        ("POST", "/api/admin/films/{id}/delete", admin_delete),
        ("POST", "/api/admin/films/{id}/reprocess", admin_reprocess),
        ("POST", "/api/admin/films/{id}/subtitles", admin_subtitle_add),
        ("POST", "/api/admin/films/{id}/subtitles/{lang}/delete", admin_subtitle_delete),
        ("GET", "/api/admin/users", admin_users),
        ("POST", "/api/admin/role", admin_role),
        ("POST", "/api/admin/vip", admin_vip),
        ("GET", "/api/admin/sales", admin_sales),
        ("POST", "/api/admin/sales/{id}/refund", admin_refund),
        ("GET", "/api/admin/stats", admin_stats),
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

    # видео, которые не обработаны (после обновления или сбоя), ставим в очередь заново
    for film in db.list_video_films():
        ready_file = cfg.media_dir / str(film["id"]) / "master.m3u8"
        if film["status"] != "ready" or not ready_file.exists():
            if film["status"] != "failed" or film["video_file_id"]:
                transcoder.enqueue(film["id"])
    transcoder.start()

    await bot.set_my_commands([BotCommand(command="start", description="Открыть")])
    if cfg.webapp_url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Фильмы", web_app=WebAppInfo(url=cfg.webapp_url)))

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
        if poller:
            poller.cancel()
        await runner.cleanup()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

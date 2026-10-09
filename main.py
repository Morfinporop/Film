#!/usr/bin/env python3
"""
Telegram-бот каталога фильмов, VIP за Telegram Stars и Mini App.
Весь проект запускается одним файлом: python main.py

Фильмы:
  video — загруженные в бот. Смотрятся в боте и в Mini App.
  link  — по ссылке. Смотрятся только в Mini App.
  price 0 — бесплатно. price > 0 — VIP, открывается за звёзды или с баланса.

Роли:
  owner  — владелец (OWNER_ID, по умолчанию 7745681987). Назначает админов. Получает звёзды.
  admin  — управляет любыми фильмами, назначает редакторов, делает возвраты.
  editor — добавляет фильмы, правит и удаляет только свои.
  user   — смотрит каталог, покупает VIP.

Звёзды: оплата через Telegram Stars (XTR). Звёзды поступают на баланс бота,
владелец видит его в админ-панели (Статистика) и выводит через Fragment/BotFather.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import html
import io
import json
import logging
import math
import os
import re
import signal
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LabeledPrice,
    MenuButtonWebApp,
    Message,
    MessageOriginUser,
    PreCheckoutQuery,
    ReplyKeyboardMarkup,
    TelegramObject,
    WebAppInfo,
)
from aiogram.webhook.aiohttp_server import SimpleRequestHandler
from aiohttp import web

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
DEFAULT_OWNER_ID = 7745681987
DEFAULT_BACKGROUND = "https://i.pinimg.com/1200x/93/31/9d/93319d1600bd69fcb7db28fef7b4a7bb.jpg"
TOPUP_OPTIONS = (50, 100, 250, 500)
MAX_PRICE = 10000
CATALOG_PAGE = 10
ADMIN_PAGE = 8
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
DEEP_LINK_RE = re.compile(r"^film_(\d+)$")
TIKTOK_RE = re.compile(r"tiktok\.com/(?:@[^/]+/video/|v/|embed/(?:v2/)?)(\d+)")
YOUTUBE_RE = re.compile(r"(?:youtube\.com/watch\?v=|youtu\.be/)([\w-]{6,})")

log = logging.getLogger("film-bot")


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
    )


# ============================================================================
# Роли и права
# ============================================================================
OWNER, ADMIN, EDITOR, USER = "owner", "admin", "editor", "user"
RANK = {USER: 0, EDITOR: 1, ADMIN: 2, OWNER: 3}
ROLE_TITLES = {OWNER: "Владелец", ADMIN: "Админ", EDITOR: "Редактор", USER: "Пользователь"}


def at_least(role: str, minimum: str) -> bool:
    return RANK.get(role, 0) >= RANK[minimum]


def can_manage_film(role: str, user_id: int, film: dict) -> bool:
    """Админ+ — любые фильмы. Редактор — только свои."""
    if at_least(role, ADMIN):
        return True
    return role == EDITOR and film.get("owner_id") == user_id


def can_change_role(actor: str, target: str, new: str) -> bool:
    """Назначать и снимать можно только роли строго ниже своей. Владельца не трогает никто."""
    if target == OWNER or new == OWNER:
        return False
    return RANK[actor] > RANK[target] and RANK[actor] > RANK[new]


# ============================================================================
# База данных (SQLite, стандартный модуль)
# ============================================================================
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_films_active ON films (is_active, id);
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments (user_id, kind, film_id);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('link', 'video')),
    url            TEXT,
    video_file_id  TEXT,
    photo_file_id  TEXT,
    owner_id       INTEGER,
    price          INTEGER NOT NULL DEFAULT 0 CHECK (price >= 0),
    is_active      INTEGER NOT NULL DEFAULT 1,
    views          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (kind <> 'link'  OR url IS NOT NULL),
    CHECK (kind <> 'video' OR video_file_id IS NOT NULL)
);

CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    role        TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'editor', 'admin')),
    balance     INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
    added_by    INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    kind        TEXT    NOT NULL CHECK (kind IN ('film', 'topup')),
    film_id     INTEGER,
    amount      INTEGER NOT NULL CHECK (amount > 0),
    source      TEXT    NOT NULL CHECK (source IN ('stars', 'balance')),
    charge_id   TEXT    UNIQUE,
    refunded    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._migrate()

    def _columns(self, table: str) -> set[str]:
        return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

    def _migrate(self) -> None:
        """Приводит базу любой прошлой версии к текущей схеме. Данные не удаляются."""
        # Старая таблица payments (подписки: days/stars) несовместима с платежами фильмов.
        # Переименовываем её, а не удаляем.
        legacy = self._columns("payments")
        if legacy and "kind" not in legacy:
            name = f"payments_legacy_{int(time.time())}"
            self.conn.execute(f"ALTER TABLE payments RENAME TO {name}")
            log.warning("старая таблица payments сохранена как %s", name)

        self.conn.executescript(SCHEMA)  # только CREATE TABLE IF NOT EXISTS

        # Колонки, которых не было в ранних версиях
        for table, column, ddl in (
            ("films", "owner_id", "ALTER TABLE films ADD COLUMN owner_id INTEGER"),
            ("films", "price", "ALTER TABLE films ADD COLUMN price INTEGER NOT NULL DEFAULT 0"),
            ("users", "balance", "ALTER TABLE users ADD COLUMN balance INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in self._columns(table):
                self.conn.execute(ddl)

        # Индексы создаём последними, когда все колонки на месте
        self.conn.executescript(INDEXES)

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _exec(self, sql: str, params: tuple = ()) -> int:
        return self.conn.execute(sql, params).rowcount

    # ---- пользователи ----
    def touch_user(self, tg_id: int, username: str | None, first_name: str | None) -> None:
        self._exec(
            """INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username,
                 first_name = excluded.first_name, last_seen = datetime('now')""",
            (tg_id, username, first_name),
        )

    def get_user(self, tg_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    def get_role(self, tg_id: int) -> str:
        row = self.get_user(tg_id)
        return row["role"] if row else USER

    def set_role(self, tg_id: int, role: str, added_by: int | None) -> None:
        self._exec(
            """INSERT INTO users (tg_id, role, added_by) VALUES (?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role, added_by = excluded.added_by""",
            (tg_id, role, added_by),
        )

    def staff(self) -> list[dict]:
        return self._all("SELECT * FROM users WHERE role IN ('admin', 'editor') ORDER BY role, tg_id")

    def balance(self, tg_id: int) -> int:
        row = self._one("SELECT balance FROM users WHERE tg_id = ?", (tg_id,))
        return row["balance"] if row else 0

    # ---- фильмы ----
    def create_film(self, *, title: str, kind: str, owner_id: int, price: int,
                    url: str | None = None, video_file_id: str | None = None,
                    photo_file_id: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO films (title, kind, url, video_file_id, photo_file_id, owner_id, price)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, kind, url, video_file_id, photo_file_id, owner_id, price),
        )
        return int(cur.lastrowid)

    def get_film(self, film_id: int) -> dict | None:
        return self._one("SELECT * FROM films WHERE id = ?", (film_id,))

    @staticmethod
    def _where(active: bool | None = None, owner_id: int | None = None, search: str | None = None,
               vip: bool | None = None) -> tuple[str, list]:
        clauses, params = [], []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        if search:
            clauses.append("title LIKE ?")
            params.append(f"%{search}%")
        if vip is not None:
            clauses.append("price > 0" if vip else "price = 0")
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def count_films(self, **kw: Any) -> int:
        where, params = self._where(**kw)
        return self._one(f"SELECT COUNT(*) AS n FROM films{where}", tuple(params))["n"]

    def list_films(self, *, top: bool = False, limit: int = 10, offset: int = 0, **kw: Any) -> list[dict]:
        where, params = self._where(**kw)
        order = "views DESC, id DESC" if top else "id DESC"
        return self._all(
            f"SELECT * FROM films{where} ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )

    def update_film(self, film_id: int, **fields: Any) -> bool:
        allowed = ("title", "url", "is_active", "price")
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return False
        sets = ", ".join(f"{k} = ?" for k, _ in items)
        return self._exec(f"UPDATE films SET {sets} WHERE id = ?", (*[v for _, v in items], film_id)) > 0

    def delete_film(self, film_id: int) -> bool:
        return self._exec("DELETE FROM films WHERE id = ?", (film_id,)) > 0

    def add_view(self, film_id: int) -> None:
        self._exec("UPDATE films SET views = views + 1 WHERE id = ?", (film_id,))

    # ---- платежи и баланс ----
    def owns(self, user_id: int, film_id: int) -> bool:
        return self._one(
            "SELECT 1 AS ok FROM payments WHERE user_id = ? AND film_id = ? AND kind = 'film' AND refunded = 0 LIMIT 1",
            (user_id, film_id),
        ) is not None

    def owned_film_ids(self, user_id: int) -> list[int]:
        rows = self._all(
            "SELECT DISTINCT film_id FROM payments WHERE user_id = ? AND kind = 'film' AND refunded = 0",
            (user_id,),
        )
        return [r["film_id"] for r in rows]

    def pay_film_with_balance(self, user_id: int, film: dict) -> bool:
        """Списывает звёзды с баланса и открывает фильм. Атомарно."""
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            row = self._one("SELECT balance FROM users WHERE tg_id = ?", (user_id,))
            if not row or row["balance"] < film["price"] or self.owns(user_id, film["id"]):
                self.conn.execute("ROLLBACK")
                return False
            self.conn.execute("UPDATE users SET balance = balance - ? WHERE tg_id = ?", (film["price"], user_id))
            self.conn.execute(
                "INSERT INTO payments (user_id, kind, film_id, amount, source) VALUES (?, 'film', ?, ?, 'balance')",
                (user_id, film["id"], film["price"]),
            )
            self.conn.execute("COMMIT")
            return True
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def record_stars_payment(self, user_id: int, kind: str, film_id: int | None,
                             amount: int, charge_id: str) -> bool:
        """Записывает оплату Stars. Повторный charge_id игнорируется (идемпотентность)."""
        self.touch_user(user_id, None, None)
        if self._one("SELECT 1 AS ok FROM payments WHERE charge_id = ?", (charge_id,)):
            return False
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            self.conn.execute(
                """INSERT INTO payments (user_id, kind, film_id, amount, source, charge_id)
                   VALUES (?, ?, ?, ?, 'stars', ?)""",
                (user_id, kind, film_id, amount, charge_id),
            )
            if kind == "topup":
                self.conn.execute("UPDATE users SET balance = balance + ? WHERE tg_id = ?", (amount, user_id))
            self.conn.execute("COMMIT")
            return True
        except sqlite3.IntegrityError:
            self.conn.execute("ROLLBACK")
            return False
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def get_payment(self, payment_id: int) -> dict | None:
        return self._one("SELECT * FROM payments WHERE id = ?", (payment_id,))

    def refund_payment(self, payment_id: int) -> str:
        """Помечает платёж возвращённым. Для пополнения списывает баланс. Возвращает 'ok' или причину."""
        p = self.get_payment(payment_id)
        if not p or p["refunded"] or p["source"] != "stars":
            return "not_refundable"
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            if p["kind"] == "topup":
                if self.balance(p["user_id"]) < p["amount"]:
                    self.conn.execute("ROLLBACK")
                    return "low_balance"
                self.conn.execute("UPDATE users SET balance = balance - ? WHERE tg_id = ?", (p["amount"], p["user_id"]))
            self.conn.execute("UPDATE payments SET refunded = 1 WHERE id = ?", (payment_id,))
            self.conn.execute("COMMIT")
            return "ok"
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def list_sales(self, limit: int = 10, offset: int = 0) -> list[dict]:
        return self._all(
            """SELECT p.*, f.title AS film_title FROM payments p
               LEFT JOIN films f ON f.id = p.film_id
               WHERE p.source = 'stars' AND p.refunded = 0
               ORDER BY p.id DESC LIMIT ? OFFSET ?""",
            (limit, offset),
        )

    def count_sales(self) -> int:
        return self._one("SELECT COUNT(*) AS n FROM payments WHERE source = 'stars' AND refunded = 0")["n"]

    def stats(self) -> dict:
        s = self._one(
            "SELECT COUNT(*) AS films, COALESCE(SUM(is_active), 0) AS active, "
            "COALESCE(SUM(views), 0) AS views FROM films"
        ) or {}
        s["users"] = self._one("SELECT COUNT(*) AS n FROM users")["n"]
        s["staff"] = self._one("SELECT COUNT(*) AS n FROM users WHERE role IN ('admin','editor')")["n"]
        s["revenue"] = self._one(
            "SELECT COALESCE(SUM(amount), 0) AS n FROM payments WHERE source = 'stars' AND refunded = 0"
        )["n"]
        return s


# ============================================================================
# Доступ, оплата, медиа, авторизация Mini App
# ============================================================================
def has_access(db: Database, role: str, user_id: int, film: dict) -> bool:
    if film["price"] <= 0:
        return True
    if at_least(role, ADMIN) or film.get("owner_id") == user_id:
        return True
    return db.owns(user_id, film["id"])


def embed_url(url: str) -> str:
    """Для TikTok и YouTube отдаём официальный встраиваемый плеер, остальное — как есть."""
    m = TIKTOK_RE.search(url)
    if m:
        return f"https://www.tiktok.com/embed/v2/{m.group(1)}"
    m = YOUTUBE_RE.search(url)
    if m:
        return f"https://www.youtube.com/embed/{m.group(1)}"
    return url


def verify_init_data(init_data: str, token: str, max_age: int = 86400) -> dict | None:
    """Проверка подписи Telegram Mini App (initData). Возвращает пользователя или None."""
    if not init_data:
        return None
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


def stream_token(secret: bytes, film_id: int, ttl: int = 900) -> str:
    exp = int(time.time()) + ttl
    sig = hmac.new(secret, f"{film_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{exp}.{sig}"


def check_stream_token(secret: bytes, film_id: int, token: str) -> bool:
    try:
        exp_s, sig = token.split(".", 1)
        exp = int(exp_s)
    except ValueError:
        return False
    if exp < time.time():
        return False
    expected = hmac.new(secret, f"{film_id}.{exp}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(expected, sig)


class MediaTooLarge(Exception):
    pass


_media_locks: dict[str, asyncio.Lock] = {}


async def ensure_media(bot: Bot, media_dir: Path, film: dict) -> Path:
    """Скачивает видео из Telegram в кэш (Bot API отдаёт файлы до 20 МБ)."""
    media_dir.mkdir(parents=True, exist_ok=True)
    name = hashlib.sha1(film["video_file_id"].encode()).hexdigest() + ".mp4"
    path = media_dir / name
    if path.exists() and path.stat().st_size > 0:
        return path
    lock = _media_locks.setdefault(name, asyncio.Lock())
    async with lock:
        if not path.exists():
            tmp = path.with_suffix(".part")
            try:
                tg_file = await bot.get_file(film["video_file_id"])
            except TelegramBadRequest as err:
                if "too big" in str(err).lower():
                    raise MediaTooLarge from err
                raise
            await bot.download_file(tg_file.file_path, destination=tmp)
            tmp.replace(path)
    return path


# ============================================================================
# Тексты и клавиатуры (без эмодзи)
# ============================================================================
def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def price_label(film: dict) -> str:
    return "Бесплатно" if film["price"] <= 0 else f"{film['price']} звёзд"


def film_caption(film: dict) -> str:
    kind = "Видео в боте" if film["kind"] == "video" else "Mini App"
    return f"<b>{esc(film['title'])}</b>\n{kind} · {price_label(film)}"


async def send_film(bot: Bot, chat_id: int, film: dict, webapp_url: str | None = None) -> None:
    """Загруженное видео — в боте. Ссылочный фильм открывается кнопкой в Mini App."""
    if film["kind"] == "video":
        await bot.send_video(chat_id, film["video_file_id"], caption=film_caption(film), supports_streaming=True)
        return
    kb = None
    if webapp_url:
        kb = rows_kb([[InlineKeyboardButton(
            text="Смотреть в Mini App", web_app=WebAppInfo(url=f"{webapp_url}?film={film['id']}"))]])
    await bot.send_message(chat_id, film_caption(film), reply_markup=kb)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def rows_kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def short(text: str, n: int = 28) -> str:
    return text if len(text) <= n else text[: n - 1] + "…"


def film_button(film: dict, webapp_url: str | None, suffix: str = "") -> InlineKeyboardButton:
    label = short(film["title"]) + (f" · {film['price']}" if suffix and film["price"] else "")
    if film["kind"] == "link" and webapp_url:
        return InlineKeyboardButton(text=label, web_app=WebAppInfo(url=f"{webapp_url}?film={film['id']}"))
    return btn(label, f"f:{film['id']}")


def films_grid(films: list[dict], webapp_url: str | None, with_price: bool = False) -> list[list[InlineKeyboardButton]]:
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for f in films:
        row.append(film_button(f, webapp_url, with_price))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return rows


def pager(prefix: str, page: int, pages: int) -> list[InlineKeyboardButton]:
    if pages <= 1:
        return []
    return [
        btn("←", f"{prefix}:{page - 1}") if page > 0 else btn(" ", "noop"),
        btn(f"{page + 1} / {pages}", "noop"),
        btn("→", f"{prefix}:{page + 1}") if page < pages - 1 else btn(" ", "noop"),
    ]


def page_bounds(total: int, page: int, size: int) -> tuple[int, int, int]:
    pages = max(1, math.ceil(total / size))
    page = min(max(page, 0), pages - 1)
    return page, pages, page * size


def main_reply_kb(role: str, webapp_url: str | None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="Каталог"), KeyboardButton(text="VIP")],
        [KeyboardButton(text="Поиск"), KeyboardButton(text="Баланс")],
    ]
    if webapp_url:
        rows.append([KeyboardButton(text="Mini App", web_app=WebAppInfo(url=webapp_url)), KeyboardButton(text="Помощь")])
    else:
        rows.append([KeyboardButton(text="Помощь")])
    if at_least(role, EDITOR):
        rows.append([KeyboardButton(text="Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, input_field_placeholder="Выберите раздел")


def admin_panel_kb(role: str) -> InlineKeyboardMarkup:
    rows = [[btn("Добавить фильм", "a:add")], [btn("Фильмы", "a:films:0")]]
    if at_least(role, ADMIN):
        rows.append([btn("Команда", "a:team"), btn("Продажи", "a:sales:0")])
        rows.append([btn("Статистика", "a:stats")])
    rows.append([btn("В меню", "m:home")])
    return rows_kb(rows)


WELCOME = "Фильмы в каталоге, VIP-подборка за звёзды и баланс. Выберите раздел в меню."
HELP = (
    "Каталог — все фильмы.\n"
    "VIP — фильмы, которые открываются за звёзды.\n"
    "Баланс — пополнение звёздами и покупки.\n"
    "Поиск — поиск по названию.\n\n"
    "Загруженные видео смотрите прямо в боте. "
    "Фильмы по ссылке открываются в Mini App."
)


# ============================================================================
# FSM-состояния
# ============================================================================
class Search(StatesGroup):
    query = State()


class AddFilm(StatesGroup):
    media = State()
    title = State()
    kind = State()
    url = State()
    video = State()
    access = State()
    price = State()
    confirm = State()


class Edit(StatesGroup):
    title = State()
    link = State()
    price = State()


class Team(StatesGroup):
    wait_id = State()


# ============================================================================
# Middleware: роль пользователя и регистрация в БД
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


async def safe_show(callback: CallbackQuery, text: str, kb: InlineKeyboardMarkup | None = None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


def build_router(cfg: Config, db: Database) -> Router:
    router = Router(name="main")

    async def deliver(bot: Bot, chat_id: int, film: dict) -> None:
        await send_film(bot, chat_id, film, cfg.webapp_url)

    async def deny(target: Message | CallbackQuery) -> None:
        if isinstance(target, CallbackQuery):
            await target.answer("Недостаточно прав", show_alert=True)
        else:
            await target.answer("Недостаточно прав для этого действия.")

    async def show_home(message: Message, role: str) -> None:
        await message.answer(WELCOME, reply_markup=main_reply_kb(role, cfg.webapp_url))

    async def show_catalog(message: Message, page: int = 0) -> None:
        total = db.count_films(active=True)
        if total == 0:
            await message.answer("Пока в каталоге пусто.")
            return
        page, pages, offset = page_bounds(total, page, CATALOG_PAGE)
        rows = films_grid(db.list_films(active=True, limit=CATALOG_PAGE, offset=offset), cfg.webapp_url, True)
        nav = pager("cat", page, pages)
        if nav:
            rows.append(nav)
        await message.answer(f"Каталог · {total}", reply_markup=rows_kb(rows))

    async def show_vip(message: Message, page: int = 0) -> None:
        total = db.count_films(active=True, vip=True)
        if total == 0:
            await message.answer("VIP-фильмов пока нет.")
            return
        page, pages, offset = page_bounds(total, page, CATALOG_PAGE)
        rows = films_grid(db.list_films(active=True, vip=True, limit=CATALOG_PAGE, offset=offset), cfg.webapp_url, True)
        nav = pager("vip", page, pages)
        if nav:
            rows.append(nav)
        await message.answer(f"VIP · {total}", reply_markup=rows_kb(rows))

    def balance_text(uid: int) -> tuple[str, InlineKeyboardMarkup]:
        rows = [[btn(f"+{n} звёзд", f"tu:{n}") for n in TOPUP_OPTIONS[:2]],
                [btn(f"+{n} звёзд", f"tu:{n}") for n in TOPUP_OPTIONS[2:]]]
        owned = db.owned_film_ids(uid)
        text = f"Баланс: {db.balance(uid)} звёзд\nКупленных фильмов: {len(owned)}"
        if cfg.webapp_url:
            rows.append([InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=cfg.webapp_url))])
        return text, rows_kb(rows)

    async def show_admin(target: Message | CallbackQuery, role: str) -> None:
        text = f"Админ-панель · {ROLE_TITLES[role]}"
        if isinstance(target, CallbackQuery):
            await safe_show(target, text, admin_panel_kb(role))
        else:
            await target.answer(text, reply_markup=admin_panel_kb(role))

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    async def send_offer(message: Message, film: dict, uid: int) -> None:
        rows = [[btn(f"Купить за {film['price']} звёзд", f"buy:{film['id']}")]]
        if db.balance(uid) >= film["price"]:
            rows.append([btn(f"Оплатить балансом ({film['price']})", f"bal:{film['id']}")])
        rows.append([btn("Пополнить баланс", "m:balance")])
        await message.answer(film_caption(film) + "\n\nФильм откроется сразу после оплаты.",
                             reply_markup=rows_kb(rows))

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, bot: Bot, role: str, state: FSMContext) -> None:
        await state.clear()
        match = DEEP_LINK_RE.fullmatch(command.args or "")
        if match:
            film = db.get_film(int(match.group(1)))
            if film and film["is_active"]:
                if film["kind"] == "link" or has_access(db, role, message.from_user.id, film):
                    await deliver(bot, message.chat.id, film)
                    return
                await send_offer(message, film, message.from_user.id)
                return
        await show_home(message, role)

    @router.message(Command("menu"))
    async def on_menu(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        await show_home(message, role)

    @router.message(Command("cancel"))
    async def on_cancel(message: Message, role: str, state: FSMContext) -> None:
        had = await state.get_state() is not None
        await state.clear()
        await message.answer("Отменено." if had else "Нечего отменять.",
                             reply_markup=main_reply_kb(role, cfg.webapp_url))

    @router.message(Command("admin"))
    async def on_admin_cmd(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        if not at_least(role, EDITOR):
            return await deny(message)
        await show_admin(message, role)

    # ------------------------------------------------------------------
    # Кнопки главного меню (работают из любого состояния)
    # ------------------------------------------------------------------
    @router.message(F.text == "Каталог")
    async def on_btn_catalog(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_catalog(message, 0)

    @router.message(F.text == "VIP")
    async def on_btn_vip(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_vip(message, 0)

    @router.message(F.text == "Баланс")
    async def on_btn_balance(message: Message, state: FSMContext) -> None:
        await state.clear()
        text, kb = balance_text(message.from_user.id)
        await message.answer(text, reply_markup=kb)

    @router.message(F.text == "Поиск")
    async def on_btn_search(message: Message, state: FSMContext) -> None:
        await state.set_state(Search.query)
        await message.answer("Напишите часть названия.\n/cancel — отмена")

    @router.message(F.text == "Помощь")
    async def on_btn_help(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(HELP)

    @router.message(F.text == "Админ-панель")
    async def on_btn_admin(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        if not at_least(role, EDITOR):
            return await deny(message)
        await show_admin(message, role)

    @router.callback_query(F.data == "m:home")
    async def on_home_btn(callback: CallbackQuery, role: str) -> None:
        await callback.answer()
        await show_home(callback.message, role)

    @router.callback_query(F.data == "m:balance")
    async def on_balance_btn(callback: CallbackQuery) -> None:
        await callback.answer()
        text, kb = balance_text(callback.from_user.id)
        await safe_show(callback, text, kb)

    @router.callback_query(F.data == "noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    @router.message(Search.query, F.text)
    async def on_search(message: Message, state: FSMContext) -> None:
        query = message.text.strip()[:60]
        await state.clear()
        films = db.list_films(active=True, search=query, limit=20)
        if not films:
            await message.answer(f"По запросу «{esc(query)}» ничего не найдено.")
            return
        await message.answer(f"Найдено: {len(films)}", reply_markup=rows_kb(films_grid(films, cfg.webapp_url, True)))

    # ------------------------------------------------------------------
    # Каталог и VIP: листание и открытие фильма
    # ------------------------------------------------------------------
    @router.callback_query(F.data.regexp(r"^(cat|vip):\d+$"))
    async def on_page(callback: CallbackQuery) -> None:
        prefix, raw = callback.data.split(":")
        vip = prefix == "vip"
        total = db.count_films(active=True, vip=True if vip else None)
        page, pages, offset = page_bounds(total, int(raw), CATALOG_PAGE)
        films = db.list_films(active=True, vip=True if vip else None, limit=CATALOG_PAGE, offset=offset)
        rows = films_grid(films, cfg.webapp_url, True)
        nav = pager(prefix, page, pages)
        if nav:
            rows.append(nav)
        await safe_show(callback, f"{'VIP' if vip else 'Каталог'} · {total}", rows_kb(rows))
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^f:\d+$"))
    async def on_film(callback: CallbackQuery, bot: Bot, role: str) -> None:
        film = db.get_film(int(callback.data.split(":")[1]))
        if not film or not film["is_active"]:
            await callback.answer("Фильм недоступен", show_alert=True)
            return
        await callback.answer()
        uid = callback.from_user.id
        if film["kind"] == "link" or has_access(db, role, uid, film):
            db.add_view(film["id"])
            await deliver(bot, callback.message.chat.id, film)
            return
        await send_offer(callback.message, film, uid)

    @router.callback_query(F.data.regexp(r"^buy:\d+$"))
    async def on_buy(callback: CallbackQuery, bot: Bot, role: str) -> None:
        film = db.get_film(int(callback.data.split(":")[1]))
        if not film or not film["is_active"] or film["kind"] != "video":
            await callback.answer("Недоступно", show_alert=True)
            return
        if has_access(db, role, callback.from_user.id, film):
            await callback.answer("Фильм уже открыт")
            return
        await callback.answer()
        await bot.send_invoice(
            callback.message.chat.id,
            title=short(film["title"], 32),
            description=f"Открытие фильма «{short(film['title'], 200)}»",
            payload=f"film:{film['id']}",
            currency="XTR",
            prices=[LabeledPrice(label=short(film["title"], 32), amount=film["price"])],
        )

    @router.callback_query(F.data.regexp(r"^bal:\d+$"))
    async def on_pay_balance(callback: CallbackQuery, bot: Bot) -> None:
        film = db.get_film(int(callback.data.split(":")[1]))
        if not film or not film["is_active"]:
            await callback.answer("Недоступно", show_alert=True)
            return
        if not db.pay_film_with_balance(callback.from_user.id, film):
            await callback.answer("Недостаточно звёзд на балансе", show_alert=True)
            return
        await callback.answer("Фильм открыт")
        await deliver(bot, callback.message.chat.id, film)

    @router.callback_query(F.data.regexp(r"^tu:\d+$"))
    async def on_topup(callback: CallbackQuery, bot: Bot) -> None:
        amount = int(callback.data.split(":")[1])
        if amount not in TOPUP_OPTIONS:
            await callback.answer("Недоступно", show_alert=True)
            return
        await callback.answer()
        await bot.send_invoice(
            callback.message.chat.id,
            title=f"Пополнение {amount} звёзд",
            description="Звёзды зачисляются на баланс и тратятся на VIP-фильмы",
            payload=f"topup:{amount}",
            currency="XTR",
            prices=[LabeledPrice(label=f"Пополнение {amount}", amount=amount)],
        )

    # ------------------------------------------------------------------
    # Платежи Telegram Stars
    # ------------------------------------------------------------------
    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery, bot: Bot) -> None:
        kind, _, arg = query.invoice_payload.partition(":")
        ok, error = False, "Оплата недоступна"
        if kind == "film" and arg.isdigit():
            film = db.get_film(int(arg))
            if not film or not film["is_active"] or film["price"] != query.total_amount:
                error = "Фильм недоступен"
            elif db.owns(query.from_user.id, film["id"]):
                error = "Фильм уже открыт"
            else:
                ok = True
        elif kind == "topup" and arg.isdigit() and int(arg) in TOPUP_OPTIONS and int(arg) == query.total_amount:
            ok = True
        await bot.answer_pre_checkout_query(query.id, ok=ok, error_message=None if ok else error)

    @router.message(F.successful_payment)
    async def on_paid(message: Message, bot: Bot) -> None:
        sp = message.successful_payment
        kind, _, arg = sp.invoice_payload.partition(":")
        uid = message.from_user.id
        if kind == "film" and arg.isdigit():
            film = db.get_film(int(arg))
            if not film:
                await message.answer("Оплата получена, но фильм не найден. Напишите владельцу.")
                return
            if db.record_stars_payment(uid, "film", film["id"], sp.total_amount, sp.telegram_payment_charge_id):
                await message.answer("Оплата прошла. Фильм открыт.")
                await deliver(bot, message.chat.id, film)
            return
        if kind == "topup" and arg.isdigit():
            if db.record_stars_payment(uid, "topup", None, sp.total_amount, sp.telegram_payment_charge_id):
                await message.answer(f"Баланс пополнен на {sp.total_amount} звёзд. Текущий баланс: {db.balance(uid)}.")

    # ------------------------------------------------------------------
    # Мастер добавления фильма (редактор+)
    # ------------------------------------------------------------------
    async def start_add(target: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddFilm.media)
        await target.answer("Новый фильм.\nШаг 1 из 5. Пришлите видео или фото-баннер.\n/cancel — отмена")

    @router.message(Command("add"))
    async def on_add_cmd(message: Message, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(message)
        await start_add(message, state)

    @router.callback_query(F.data == "a:add")
    async def on_add_btn(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        await callback.answer()
        if not at_least(role, EDITOR):
            return await deny(callback)
        await start_add(callback.message, state)

    @router.message(AddFilm.media, F.video | F.photo)
    async def on_media(message: Message, state: FSMContext) -> None:
        if message.video:
            await state.update_data(video_file_id=message.video.file_id)
        else:
            await state.update_data(photo_file_id=message.photo[-1].file_id)
        await state.set_state(AddFilm.title)
        await message.answer("Шаг 2 из 5. Напишите название.")

    @router.message(AddFilm.media)
    async def on_media_bad(message: Message) -> None:
        await message.answer("Нужно видео или фото-баннер.")

    @router.message(AddFilm.title, F.text)
    async def on_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip()[:100])
        await state.set_state(AddFilm.kind)
        await message.answer(
            "Шаг 3 из 5. Как открывать фильм?\n"
            "По ссылке — откроется в Mini App.\n"
            "Видео в боте — загруженный файл, смотрится в боте и в Mini App.",
            reply_markup=rows_kb([
                [btn("По ссылке", "wz:kind:link"), btn("Видео в боте", "wz:kind:video")],
                [btn("Отмена", "wz:cancel")],
            ]),
        )

    @router.message(AddFilm.title)
    async def on_title_bad(message: Message) -> None:
        await message.answer("Напишите название текстом.")

    @router.callback_query(AddFilm.kind, F.data.in_({"wz:kind:link", "wz:kind:video"}))
    async def on_kind(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        kind = callback.data.split(":")[-1]
        await state.update_data(kind=kind)
        if kind == "link":
            await state.set_state(AddFilm.url)
            await callback.message.answer("Шаг 4 из 5. Пришлите ссылку (https://...).")
        elif (await state.get_data()).get("video_file_id"):
            await ask_access(callback.message, state)
        else:
            await state.set_state(AddFilm.video)
            await callback.message.answer("Шаг 4 из 5. Пришлите видеофайл.")

    @router.message(AddFilm.url, F.text)
    async def on_url(message: Message, state: FSMContext) -> None:
        url = message.text.strip()
        if not URL_RE.match(url):
            await message.answer("Нужна ссылка, начинающаяся с https://")
            return
        await state.update_data(url=url)
        await ask_access(message, state)

    @router.message(AddFilm.video, F.video)
    async def on_video(message: Message, state: FSMContext) -> None:
        await state.update_data(video_file_id=message.video.file_id)
        await ask_access(message, state)

    @router.message(AddFilm.video)
    async def on_video_bad(message: Message) -> None:
        await message.answer("Пришлите видеофайл.")

    async def ask_access(message: Message, state: FSMContext) -> None:
        await state.set_state(AddFilm.access)
        await message.answer(
            "Шаг 5 из 5. Доступ к фильму?",
            reply_markup=rows_kb([[btn("Бесплатно", "wz:acc:free"), btn("VIP за звёзды", "wz:acc:vip")]]),
        )

    @router.callback_query(AddFilm.access, F.data.in_({"wz:acc:free", "wz:acc:vip"}))
    async def on_access(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        if callback.data == "wz:acc:free":
            await state.update_data(price=0)
            return await show_confirm(callback.message, state)
        await state.set_state(AddFilm.price)
        await callback.message.answer(f"Укажите цену в звёздах (от 1 до {MAX_PRICE}).")

    @router.message(AddFilm.price, F.text)
    async def on_price(message: Message, state: FSMContext) -> None:
        text = message.text.strip()
        if not text.isdigit() or not 1 <= int(text) <= MAX_PRICE:
            await message.answer(f"Введите целое число от 1 до {MAX_PRICE}.")
            return
        await state.update_data(price=int(text))
        await show_confirm(message, state)

    async def show_confirm(message: Message, state: FSMContext) -> None:
        await state.set_state(AddFilm.confirm)
        data = await state.get_data()
        preview = {**data, "id": 0}
        await message.answer("Превью:")
        if data["kind"] == "video" and data.get("video_file_id"):
            await message.bot.send_video(message.chat.id, data["video_file_id"], caption=film_caption(preview))
        elif data.get("photo_file_id"):
            await message.bot.send_photo(message.chat.id, data["photo_file_id"], caption=film_caption(preview))
        else:
            await message.answer(film_caption(preview))
        await message.answer(
            "Сохранить фильм?",
            reply_markup=rows_kb([[btn("Сохранить", "wz:save"), btn("Отмена", "wz:cancel")]]),
        )

    @router.callback_query(AddFilm.confirm, F.data == "wz:save")
    async def on_save(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        data = await state.get_data()
        film_id = db.create_film(
            title=data["title"], kind=data["kind"], owner_id=callback.from_user.id,
            price=data.get("price", 0), url=data.get("url"),
            video_file_id=data.get("video_file_id"), photo_file_id=data.get("photo_file_id"),
        )
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer(
            f"Фильм #{film_id} добавлен в каталог.",
            reply_markup=rows_kb([[btn("Мои фильмы", "a:films:0"), btn("Добавить ещё", "a:add")]]),
        )

    @router.callback_query(F.data == "wz:cancel")
    async def on_cancel_btn(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await callback.message.answer("Отменено.")

    # ------------------------------------------------------------------
    # Управление фильмами
    # ------------------------------------------------------------------
    @router.callback_query(F.data.startswith("a:films:"))
    async def on_films_list(callback: CallbackQuery, role: str) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        scope = None if at_least(role, ADMIN) else callback.from_user.id
        total = db.count_films(owner_id=scope)
        page, pages, offset = page_bounds(total, int(callback.data.rsplit(":", 1)[1]), ADMIN_PAGE)
        films = db.list_films(owner_id=scope, limit=ADMIN_PAGE, offset=offset)
        rows = [[btn(f"{'' if f['is_active'] else 'Скрыт · '}{short(f['title'], 24)}", f"a:film:{f['id']}")]
                for f in films]
        nav = pager("a:films", page, pages)
        if nav:
            rows.append(nav)
        rows.append([btn("Назад", "a:panel")])
        text = f"Фильмы · {'все' if scope is None else 'мои'}: {total}"
        if not films:
            text += "\n\nПока пусто."
        await safe_show(callback, text, rows_kb(rows))
        await callback.answer()

    async def load_managed(callback: CallbackQuery, role: str, film_id: int) -> dict | None:
        film = db.get_film(film_id)
        if not film:
            await callback.answer("Фильм не найден", show_alert=True)
            return None
        if not can_manage_film(role, callback.from_user.id, film):
            await deny(callback)
            return None
        return film

    async def render_card(callback: CallbackQuery, film: dict) -> None:
        text = (
            f"<b>{esc(film['title'])}</b>\n"
            f"ID {film['id']} · {'видео в боте' if film['kind'] == 'video' else 'по ссылке'} · "
            f"{price_label(film)}\n"
            f"Просмотров: {film['views']}\n"
            f"Статус: {'в каталоге' if film['is_active'] else 'скрыт'}"
        )
        rows = [[btn("Открыть", f"a:open:{film['id']}"), btn("Название", f"a:rename:{film['id']}")]]
        if film["kind"] == "link":
            rows.append([btn("Сменить ссылку", f"a:link:{film['id']}")])
        rows.append([btn("Цена", f"a:price:{film['id']}")])
        rows.append([
            btn("Скрыть" if film["is_active"] else "Показать", f"a:toggle:{film['id']}"),
            btn("Удалить", f"a:del:{film['id']}"),
        ])
        rows.append([btn("К списку", "a:films:0")])
        await safe_show(callback, text, rows_kb(rows))

    @router.callback_query(F.data.regexp(r"^a:film:\d+$"))
    async def on_film_card(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if film:
            await callback.answer()
            await render_card(callback, film)

    @router.callback_query(F.data.regexp(r"^a:open:\d+$"))
    async def on_open(callback: CallbackQuery, role: str, bot: Bot) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if film:
            await callback.answer()
            await deliver(bot, callback.message.chat.id, film)

    @router.callback_query(F.data.regexp(r"^a:toggle:\d+$"))
    async def on_toggle(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        db.update_film(film["id"], is_active=0 if film["is_active"] else 1)
        await callback.answer("Готово")
        await render_card(callback, db.get_film(film["id"]))

    @router.callback_query(F.data.regexp(r"^a:del:\d+$"))
    async def on_delete_ask(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if film:
            await callback.answer()
            await safe_show(
                callback,
                f"Удалить фильм «{esc(film['title'])}»? Это необратимо.",
                rows_kb([[btn("Да, удалить", f"a:delok:{film['id']}"), btn("Нет", f"a:film:{film['id']}")]]),
            )

    @router.callback_query(F.data.regexp(r"^a:delok:\d+$"))
    async def on_delete(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if film:
            db.delete_film(film["id"])
            await callback.answer("Удалено")
            await safe_show(callback, "Фильм удалён.", rows_kb([[btn("К списку", "a:films:0")]]))

    @router.callback_query(F.data.regexp(r"^a:(rename|link|price):\d+$"))
    async def on_edit_ask(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        action, raw_id = callback.data.split(":")[1:]
        film = await load_managed(callback, role, int(raw_id))
        if not film:
            return
        if action == "link" and film["kind"] != "link":
            await callback.answer("У этого фильма нет ссылки", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.update_data(film_id=film["id"])
        prompts = {
            "rename": (Edit.title, "Новое название.\n/cancel — отмена"),
            "link": (Edit.link, "Новая ссылка (https://...).\n/cancel — отмена"),
            "price": (Edit.price, f"Новая цена в звёздах (0 — бесплатно, до {MAX_PRICE})."),
        }
        new_state, prompt = prompts[action]
        await state.set_state(new_state)
        await callback.message.answer(prompt)

    async def edit_film_from_message(message: Message, role: str, state: FSMContext, field: str, value: Any) -> None:
        data = await state.get_data()
        film = db.get_film(data.get("film_id", 0))
        await state.clear()
        if not film or not can_manage_film(role, message.from_user.id, film):
            return await deny(message)
        db.update_film(film["id"], **{field: value})
        await message.answer("Сохранено.", reply_markup=rows_kb([[btn("К фильму", f"a:film:{film['id']}")]]))

    @router.message(Edit.title, F.text)
    async def on_rename(message: Message, role: str, state: FSMContext) -> None:
        await edit_film_from_message(message, role, state, "title", message.text.strip()[:100])

    @router.message(Edit.link, F.text)
    async def on_relink(message: Message, role: str, state: FSMContext) -> None:
        url = message.text.strip()
        if not URL_RE.match(url):
            await message.answer("Нужна ссылка, начинающаяся с https://")
            return
        await edit_film_from_message(message, role, state, "url", url)

    @router.message(Edit.price, F.text)
    async def on_reprice(message: Message, role: str, state: FSMContext) -> None:
        text = message.text.strip()
        if not text.isdigit() or int(text) > MAX_PRICE:
            await message.answer(f"Введите целое число от 0 до {MAX_PRICE}.")
            return
        await edit_film_from_message(message, role, state, "price", int(text))

    # ------------------------------------------------------------------
    # Команда и продажи (админ+)
    # ------------------------------------------------------------------
    async def team_view(target: Message | CallbackQuery, actor: str) -> None:
        lines = ["Команда", f"Владелец: {cfg.owner_id}"]
        rows: list[list[InlineKeyboardButton]] = []
        for m in db.staff():
            name = esc(m["first_name"] or m["username"] or m["tg_id"])
            lines.append(f"{ROLE_TITLES[m['role']]}: {name} ({m['tg_id']})")
            if can_change_role(actor, m["role"], USER):
                rows.append([btn(f"Снять роль: {m['tg_id']}", f"a:rm:{m['tg_id']}")])
        if not db.staff():
            lines.append("\nПока никого нет.")
        add_row = [btn("Добавить редактора", "a:addm:editor")]
        if actor == OWNER:
            add_row.append(btn("Добавить админа", "a:addm:admin"))
        rows.append(add_row)
        rows.append([btn("Назад", "a:panel")])
        text = "\n".join(lines) + "\n\nПришлите Telegram ID или перешлите сообщение пользователя."
        if isinstance(target, CallbackQuery):
            await safe_show(target, text, rows_kb(rows))
            await target.answer()
        else:
            await target.answer(text, reply_markup=rows_kb(rows))

    @router.callback_query(F.data == "a:team")
    async def on_team(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        await state.clear()
        await team_view(callback, role)

    @router.callback_query(F.data.in_({"a:addm:editor", "a:addm:admin"}))
    async def on_add_member_ask(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        new_role = callback.data.split(":")[-1]
        if not can_change_role(role, USER, new_role):
            return await deny(callback)
        await callback.answer()
        await state.set_state(Team.wait_id)
        await state.update_data(new_role=new_role)
        await callback.message.answer(f"Пришлите ID для роли «{ROLE_TITLES[new_role]}» или перешлите сообщение.\n/cancel — отмена")

    @router.message(Team.wait_id)
    async def on_add_member(message: Message, role: str, bot: Bot, state: FSMContext) -> None:
        data = await state.get_data()
        new_role = data.get("new_role", EDITOR)
        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            target_id = origin.sender_user.id
        elif message.text and message.text.strip().isdigit():
            target_id = int(message.text.strip())
        else:
            await message.answer("Нужен числовой ID или пересланное сообщение.")
            return
        await state.clear()
        if target_id == cfg.owner_id:
            await message.answer("Роль владельца не меняется.")
            return
        if not can_change_role(role, db.get_role(target_id), new_role):
            return await deny(message)
        db.set_role(target_id, new_role, added_by=message.from_user.id)
        await message.answer(f"Роль «{ROLE_TITLES[new_role]}» выдана: {target_id}")
        try:
            await bot.send_message(target_id, f"Вам выдана роль «{ROLE_TITLES[new_role]}». Меню: /menu",
                                   reply_markup=main_reply_kb(new_role, cfg.webapp_url))
        except TelegramAPIError:
            await message.answer("Пользователь ещё не запускал бота, уведомление не доставлено.")
        await team_view(message, role)

    @router.callback_query(F.data.regexp(r"^a:rm:\d+$"))
    async def on_remove_member(callback: CallbackQuery, role: str, bot: Bot) -> None:
        target_id = int(callback.data.split(":")[-1])
        if not can_change_role(role, db.get_role(target_id), USER):
            return await deny(callback)
        db.set_role(target_id, USER, added_by=callback.from_user.id)
        await callback.answer("Роль снята")
        try:
            await bot.send_message(target_id, "Ваша роль в боте снята.")
        except TelegramAPIError:
            pass
        await team_view(callback, role)

    @router.callback_query(F.data.regexp(r"^a:sales:\d+$"))
    async def on_sales(callback: CallbackQuery, role: str) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        total = db.count_sales()
        page, pages, offset = page_bounds(total, int(callback.data.split(":")[-1]), 8)
        sales = db.list_sales(limit=8, offset=offset)
        lines = [f"Продажи · {total}"]
        rows: list[list[InlineKeyboardButton]] = []
        for s in sales:
            what = s["film_title"] if s["kind"] == "film" else "Пополнение"
            lines.append(f"#{s['id']} · {esc(what)} · {s['amount']} звёзд · {s['user_id']}")
            rows.append([btn(f"Вернуть #{s['id']}", f"sl:ask:{s['id']}")])
        if not sales:
            lines.append("\nПродаж пока нет.")
        nav = pager("a:sales", page, pages)
        if nav:
            rows.append(nav)
        rows.append([btn("Назад", "a:panel")])
        await safe_show(callback, "\n".join(lines), rows_kb(rows))
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^sl:ask:\d+$"))
    async def on_refund_ask(callback: CallbackQuery, role: str) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        p = db.get_payment(int(callback.data.split(":")[-1]))
        if not p or p["refunded"]:
            await callback.answer("Платёж не найден или уже возвращён", show_alert=True)
            return
        await callback.answer()
        await safe_show(
            callback,
            f"Вернуть {p['amount']} звёзд пользователю {p['user_id']}?",
            rows_kb([[btn("Да, вернуть", f"sl:ok:{p['id']}"), btn("Нет", "a:sales:0")]]),
        )

    @router.callback_query(F.data.regexp(r"^sl:ok:\d+$"))
    async def on_refund(callback: CallbackQuery, role: str, bot: Bot) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        p = db.get_payment(int(callback.data.split(":")[-1]))
        if not p or p["refunded"] or not p["charge_id"]:
            await callback.answer("Платёж не найден", show_alert=True)
            return
        if p["kind"] == "topup" and db.balance(p["user_id"]) < p["amount"]:
            await callback.answer("На балансе пользователя недостаточно звёзд", show_alert=True)
            return
        try:
            await bot.refund_star_payment(user_id=p["user_id"], telegram_payment_charge_id=p["charge_id"])
        except TelegramAPIError as err:
            log.warning("возврат не выполнен: %s", err)
            await callback.answer("Telegram отклонил возврат", show_alert=True)
            return
        db.refund_payment(p["id"])
        await callback.answer("Возврат выполнен")
        try:
            await bot.send_message(p["user_id"], f"Оплата {p['amount']} звёзд возвращена.")
        except TelegramAPIError:
            pass
        await safe_show(callback, "Возврат выполнен.", rows_kb([[btn("К продажам", "a:sales:0")]]))

    @router.callback_query(F.data == "a:stats")
    async def on_stats(callback: CallbackQuery, role: str, bot: Bot) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        s = db.stats()
        try:
            bot_balance = str((await bot.get_my_star_balance()).amount)
        except TelegramAPIError:
            bot_balance = "недоступно"
        text = (
            "Статистика\n"
            f"Фильмов: {s['films']} (в каталоге: {s['active']})\n"
            f"Просмотров: {s['views']}\n"
            f"Пользователей: {s['users']}\n"
            f"Сотрудников: {s['staff']}\n\n"
            f"Выручка за звёзды: {s['revenue']}\n"
            f"Баланс бота в Telegram: {bot_balance} звёзд"
        )
        await safe_show(callback, text, rows_kb([[btn("Продажи", "a:sales:0")], [btn("Назад", "a:panel")]]))
        await callback.answer()

    @router.callback_query(F.data == "a:panel")
    async def on_panel(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        await state.clear()
        await show_admin(callback, role)
        await callback.answer()

    # ------------------------------------------------------------------
    # Неизвестный текст
    # ------------------------------------------------------------------
    @router.message(F.text)
    async def on_fallback(message: Message, role: str, state: FSMContext) -> None:
        if await state.get_state() is None:
            await message.answer("Выберите раздел в меню.", reply_markup=main_reply_kb(role, cfg.webapp_url))

    return router


# ============================================================================
# HTTP: вебхук, Mini App, API
# ============================================================================
def build_web(cfg: Config, db: Database, bot: Bot, dp: Dispatcher, bot_username: str | None) -> web.Application:
    app = web.Application(client_max_size=1024 * 64)
    secret = hashlib.sha256(b"stream:" + cfg.bot_token.encode()).digest()

    async def authed_user(request: web.Request) -> dict | None:
        user = verify_init_data(request.headers.get("X-Init-Data", ""), cfg.bot_token)
        if user:
            db.touch_user(int(user["id"]), user.get("username"), user.get("first_name"))
        return user

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def mini_app(_: web.Request) -> web.FileResponse:
        return web.FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    async def api_config(_: web.Request) -> web.Response:
        return web.json_response({
            "botUsername": bot_username,
            "background": cfg.background_url,
            "topups": list(TOPUP_OPTIONS),
        })

    async def api_films(_: web.Request) -> web.Response:
        films = [
            {
                "id": f["id"],
                "title": f["title"],
                "kind": f["kind"],
                "price": f["price"],
                "views": f["views"],
                "hasBanner": bool(f["photo_file_id"]),
            }
            for f in db.list_films(active=True, limit=500)
        ]
        return web.json_response(films, headers={"Cache-Control": "no-store"})

    async def api_me(request: web.Request) -> web.Response:
        user = await authed_user(request)
        if not user:
            return web.json_response({"error": "auth"}, status=401)
        uid = int(user["id"])
        return web.json_response({
            "name": user.get("first_name") or "",
            "balance": db.balance(uid),
            "owned": db.owned_film_ids(uid),
        }, headers={"Cache-Control": "no-store"})

    async def api_banner(request: web.Request) -> web.Response:
        try:
            film = db.get_film(int(request.match_info["film_id"]))
        except ValueError:
            film = None
        if not film or not film["is_active"] or not film["photo_file_id"]:
            return web.Response(status=404, text="not found")
        try:
            tg_file = await bot.get_file(film["photo_file_id"])
            buf = io.BytesIO()
            await bot.download_file(tg_file.file_path, destination=buf)
        except TelegramAPIError as err:
            log.warning("баннер не загружен: %s", err)
            return web.Response(status=502, text="banner error")
        return web.Response(body=buf.getvalue(), content_type="image/jpeg",
                            headers={"Cache-Control": "public, max-age=3600"})

    async def load_film(request: web.Request) -> dict | None:
        try:
            film = db.get_film(int(request.match_info["film_id"]))
        except ValueError:
            return None
        return film if film and film["is_active"] else None

    async def api_play(request: web.Request) -> web.Response:
        user = await authed_user(request)
        if not user:
            return web.json_response({"error": "auth"}, status=401)
        film = await load_film(request)
        if not film:
            return web.json_response({"error": "not_found"}, status=404)
        uid = int(user["id"])
        role = OWNER if uid == cfg.owner_id else db.get_role(uid)
        if not has_access(db, role, uid, film):
            return web.json_response({"error": "locked", "price": film["price"]}, status=403)
        db.add_view(film["id"])
        if film["kind"] == "link":
            return web.json_response({"kind": "link", "src": embed_url(film["url"]), "url": film["url"]})
        try:
            await ensure_media(bot, cfg.media_dir, film)
        except MediaTooLarge:
            return web.json_response({"error": "too_large"}, status=413)
        except TelegramAPIError as err:
            log.warning("видео не загружено: %s", err)
            return web.json_response({"error": "media"}, status=502)
        token = stream_token(secret, film["id"])
        return web.json_response({"kind": "video", "src": f"/api/stream/{film['id']}?t={token}"})

    async def api_stream(request: web.Request) -> web.StreamResponse:
        film = await load_film(request)
        token = request.query.get("t", "")
        if not film or film["kind"] != "video" or not check_stream_token(secret, film["id"], token):
            return web.Response(status=403, text="forbidden")
        path = cfg.media_dir / (hashlib.sha1(film["video_file_id"].encode()).hexdigest() + ".mp4")
        if not path.exists():
            return web.Response(status=404, text="not cached")
        return web.FileResponse(path, headers={"Content-Type": "video/mp4", "Cache-Control": "private, max-age=600"})

    async def api_buy(request: web.Request) -> web.Response:
        user = await authed_user(request)
        if not user:
            return web.json_response({"error": "auth"}, status=401)
        film = await load_film(request)
        if not film:
            return web.json_response({"error": "not_found"}, status=404)
        uid = int(user["id"])
        if has_access(db, OWNER if uid == cfg.owner_id else db.get_role(uid), uid, film):
            return web.json_response({"ok": True, "owned": True, "balance": db.balance(uid)})
        if db.pay_film_with_balance(uid, film):
            return web.json_response({"ok": True, "owned": True, "balance": db.balance(uid)})
        link = await bot.create_invoice_link(
            title=short(film["title"], 32),
            description=f"Открытие фильма «{short(film['title'], 200)}»",
            payload=f"film:{film['id']}",
            currency="XTR",
            prices=[LabeledPrice(label=short(film["title"], 32), amount=film["price"])],
        )
        return web.json_response({"ok": False, "invoice": link})

    async def api_topup(request: web.Request) -> web.Response:
        user = await authed_user(request)
        if not user:
            return web.json_response({"error": "auth"}, status=401)
        try:
            amount = int((await request.json()).get("amount"))
        except (ValueError, TypeError, json.JSONDecodeError):
            return web.json_response({"error": "bad_amount"}, status=400)
        if amount not in TOPUP_OPTIONS:
            return web.json_response({"error": "bad_amount"}, status=400)
        link = await bot.create_invoice_link(
            title=f"Пополнение {amount} звёзд",
            description="Звёзды зачисляются на баланс и тратятся на VIP-фильмы",
            payload=f"topup:{amount}",
            currency="XTR",
            prices=[LabeledPrice(label=f"Пополнение {amount}", amount=amount)],
        )
        return web.json_response({"invoice": link})

    app.router.add_get("/health", health)
    app.router.add_get("/", mini_app)
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/films", api_films)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/banner/{film_id}", api_banner)
    app.router.add_post("/api/films/{film_id}/play", api_play)
    app.router.add_get("/api/stream/{film_id}", api_stream)
    app.router.add_post("/api/films/{film_id}/buy", api_buy)
    app.router.add_post("/api/topup", api_topup)

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

    for admin_id in cfg.initial_admins:
        if admin_id != cfg.owner_id and db.get_user(admin_id) is None:
            db.set_role(admin_id, ADMIN, added_by=cfg.owner_id)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.update.outer_middleware(RoleMiddleware(db, cfg.owner_id))
    dp.include_router(build_router(cfg, db))

    me = await bot.get_me()
    log.info("бот @%s, владелец: %s, режим: %s, БД: %s", me.username, cfg.owner_id, cfg.mode, cfg.db_path)

    await bot.set_my_commands([
        BotCommand(command="start", description="Главное меню"),
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="add", description="Добавить фильм"),
        BotCommand(command="cancel", description="Отмена"),
    ])
    if cfg.webapp_url:
        await bot.set_chat_menu_button(menu_button=MenuButtonWebApp(text="Фильмы", web_app=WebAppInfo(url=cfg.webapp_url)))

    runner = web.AppRunner(build_web(cfg, db, bot, dp, me.username))
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
            log.info("режим polling")
        await stop.wait()
    finally:
        log.info("останавливаюсь")
        if poller:
            poller.cancel()
        await runner.cleanup()
        await bot.session.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())

#!/usr/bin/env python3
"""
Telegram-бот каталога фильмов + Mini App. Весь проект запускается одним файлом:

    python main.py

Возможности:
  * каталог, популярное, умный поиск (опечатки, части слов, похожие названия);
  * фильмы: «видео в боте» или «ссылка»; можно пометить фильм как VIP;
  * VIP-подписка за Telegram Stars (встроенная оплата, без платёжного провайдера);
  * роли: владелец (OWNER_ID), админ, редактор, пользователь;
  * Mini App (index.html): просмотр видео прямо в приложении, поиск, VIP-статус.

Роли и VIP хранятся в SQLite, выдаются кнопками в боте без перезапуска.
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
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import parse_qsl

import aiohttp
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
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
DEEP_LINK_RE = re.compile(r"^film_(\d+)$")
CATALOG_PAGE = 8
ADMIN_PAGE = 8
TZ_LOCAL = timezone(timedelta(hours=5), "Екатеринбург")  # UTC+5, без перехода на летнее время

# VIP-тарифы: дни -> (название, цена в Telegram Stars)
VIP_PLANS: dict[int, tuple[str, int]] = {
    30: ("1 месяц", 150),
    90: ("3 месяца", 390),
    365: ("1 год", 1290),
}

# Иконки кнопок: если в EMOJI_IDS задан custom_emoji_id, показывается премиум-эмодзи Telegram,
# иначе — обычный эмодзи из ICON_FALLBACK.
ICON_FALLBACK = {
    "catalog": "🎬", "fire": "🔥", "search": "🔍", "vip": "💎", "app": "📱", "help": "ℹ️",
    "admin": "⚙️", "films": "🎞", "team": "👥", "stats": "📊", "add": "➕", "back": "⬅️",
    "home": "🏠", "play": "▶️", "link": "🔗", "lock": "🔒", "eye": "👁", "edit": "✏️",
    "pause": "⏸", "trash": "🗑", "ok": "✅", "no": "❌", "gift": "🎁", "spider": "🕷",
    "web": "🕸", "star": "⭐", "next": "▶️", "prev": "◀️", "similar": "🧩",
}

log = logging.getLogger("film-bot")
EMOJI_IDS: dict[str, str] = {}  # заполняется из переменной EMOJI_IDS


# ============================================================================
# Конфигурация (только переменные окружения)
# ============================================================================
@dataclass(frozen=True)
class Config:
    bot_token: str
    owner_id: int
    initial_admins: frozenset[int]
    mode: str  # webhook | polling
    public_url: str | None
    port: int
    webhook_path: str
    webhook_secret: str
    db_path: str
    emoji_ids: dict[str, str]

    @property
    def webapp_url(self) -> str | None:
        return f"{self.public_url}/app" if self.public_url else None


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

    owner_raw = (env.get("OWNER_ID") or "").strip() or str(DEFAULT_OWNER_ID)
    owner_ids = _parse_ids(owner_raw, "OWNER_ID")
    if len(owner_ids) != 1:
        raise SystemExit("OWNER_ID должен содержать ровно один Telegram ID")

    initial_admins = _parse_ids(env.get("ADMIN_IDS") or "", "ADMIN_IDS")

    mode = (env.get("BOT_MODE") or "webhook").strip().lower()
    if mode not in ("webhook", "polling"):
        raise SystemExit('BOT_MODE должен быть "webhook" или "polling"')

    public_url = (env.get("PUBLIC_URL") or "").strip().rstrip("/") or None
    if mode == "webhook":
        if not public_url:
            raise SystemExit("PUBLIC_URL обязателен в режиме webhook")
        if not public_url.startswith("https://"):
            raise SystemExit("PUBLIC_URL должен начинаться с https:// (требование Telegram)")

    port = int(env.get("PORT") or 3000)

    webhook_path = (env.get("WEBHOOK_PATH") or "/webhook").strip()
    if not webhook_path.startswith("/"):
        webhook_path = "/" + webhook_path

    secret = (env.get("WEBHOOK_SECRET") or "").strip() or hashlib.sha256(
        f"webhook:{token}".encode()
    ).hexdigest()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise SystemExit("WEBHOOK_SECRET: допустимы только латиница, цифры, _ и -")

    db_path = (env.get("DB_PATH") or "").strip() or str(BASE_DIR / "data" / "film.db")

    emoji_raw = (env.get("EMOJI_IDS") or "").strip()
    try:
        emoji_ids = {str(k): str(v) for k, v in json.loads(emoji_raw).items()} if emoji_raw else {}
    except (json.JSONDecodeError, AttributeError) as err:
        raise SystemExit(f"EMOJI_IDS должен быть JSON-объектом: {err}") from err

    return Config(
        bot_token=token,
        owner_id=next(iter(owner_ids)),
        initial_admins=frozenset(initial_admins),
        mode=mode,
        public_url=public_url,
        port=port,
        webhook_path=webhook_path,
        webhook_secret=secret,
        db_path=db_path,
        emoji_ids=emoji_ids,
    )


# ============================================================================
# Роли и права
# ============================================================================
OWNER, ADMIN, EDITOR, USER = "owner", "admin", "editor", "user"
RANK = {USER: 0, EDITOR: 1, ADMIN: 2, OWNER: 3}
ROLE_TITLES = {OWNER: "👑 Владелец", ADMIN: "🛡 Админ", EDITOR: "✏️ Редактор", USER: "Пользователь"}


def at_least(role: str, minimum: str) -> bool:
    return RANK.get(role, 0) >= RANK[minimum]


def can_manage_film(role: str, user_id: int, film: dict) -> bool:
    """Админ+ — любые фильмы; редактор — только свои."""
    if at_least(role, ADMIN):
        return True
    return role == EDITOR and film.get("owner_id") == user_id


def can_change_role(actor_role: str, target_role: str, new_role: str) -> bool:
    """Назначать и снимать можно только роли строго ниже своей; владельца никто не меняет."""
    if target_role == OWNER or new_role == OWNER:
        return False
    return RANK[actor_role] > RANK[target_role] and RANK[actor_role] > RANK[new_role]


# ============================================================================
# База данных (SQLite, стандартный модуль)
# ============================================================================
SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('link', 'video')),
    url            TEXT,
    video_file_id  TEXT,
    photo_file_id  TEXT,
    owner_id       INTEGER,
    is_vip         INTEGER NOT NULL DEFAULT 0,
    is_active      INTEGER NOT NULL DEFAULT 1,
    views          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (kind <> 'link'  OR url IS NOT NULL),
    CHECK (kind <> 'video' OR video_file_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_films_active ON films (is_active, id);

CREATE TABLE IF NOT EXISTS users (
    tg_id       INTEGER PRIMARY KEY,
    username    TEXT,
    first_name  TEXT,
    role        TEXT    NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'editor', 'admin')),
    vip_until   TEXT,
    added_by    INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);

CREATE TABLE IF NOT EXISTS payments (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL,
    days        INTEGER NOT NULL,
    stars       INTEGER NOT NULL,
    charge_id   TEXT    NOT NULL UNIQUE,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
"""


class Database:
    def __init__(self, path: str) -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self._add_missing_columns()
        self.conn.commit()

    def _add_missing_columns(self) -> None:
        """Миграции для баз, созданных старыми версиями бота."""
        def cols(table: str) -> set[str]:
            return {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}

        if "owner_id" not in cols("films"):
            self.conn.execute("ALTER TABLE films ADD COLUMN owner_id INTEGER")
        if "is_vip" not in cols("films"):
            self.conn.execute("ALTER TABLE films ADD COLUMN is_vip INTEGER NOT NULL DEFAULT 0")
        if "vip_until" not in cols("users"):
            self.conn.execute("ALTER TABLE users ADD COLUMN vip_until TEXT")

    def close(self) -> None:
        self.conn.close()

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        row = self.conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def _all(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def _exec(self, sql: str, params: tuple = ()) -> int:
        cur = self.conn.execute(sql, params)
        self.conn.commit()
        return cur.rowcount

    # ---- пользователи, роли, VIP ----
    def touch_user(self, tg_id: int, username: str | None, first_name: str | None) -> None:
        self._exec(
            """INSERT INTO users (tg_id, username, first_name) VALUES (?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET username = excluded.username,
                 first_name = excluded.first_name, last_seen = datetime('now')""",
            (tg_id, username, first_name),
        )

    def get_role(self, tg_id: int) -> str:
        row = self._one("SELECT role FROM users WHERE tg_id = ?", (tg_id,))
        return row["role"] if row else USER

    def get_user(self, tg_id: int) -> dict | None:
        return self._one("SELECT * FROM users WHERE tg_id = ?", (tg_id,))

    def set_role(self, tg_id: int, role: str, added_by: int | None) -> None:
        self._exec(
            """INSERT INTO users (tg_id, role, added_by) VALUES (?, ?, ?)
               ON CONFLICT(tg_id) DO UPDATE SET role = excluded.role, added_by = excluded.added_by""",
            (tg_id, role, added_by),
        )

    def staff(self) -> list[dict]:
        return self._all("SELECT * FROM users WHERE role IN ('admin', 'editor') ORDER BY role, tg_id")

    def is_vip(self, tg_id: int) -> bool:
        return self._one(
            "SELECT 1 AS ok FROM users WHERE tg_id = ? AND vip_until > datetime('now')", (tg_id,)
        ) is not None

    def vip_until(self, tg_id: int) -> str | None:
        row = self._one(
            "SELECT vip_until FROM users WHERE tg_id = ? AND vip_until > datetime('now')", (tg_id,)
        )
        return row["vip_until"] if row else None

    def grant_vip(self, tg_id: int, days: int) -> str | None:
        """Продлевает VIP: от текущего окончания, если оно в будущем, иначе от текущего момента."""
        self._exec("INSERT OR IGNORE INTO users (tg_id) VALUES (?)", (tg_id,))
        self._exec(
            """UPDATE users SET vip_until = datetime(
                   CASE WHEN vip_until IS NOT NULL AND vip_until > datetime('now')
                        THEN vip_until ELSE datetime('now') END, ?)
               WHERE tg_id = ?""",
            (f"+{int(days)} days", tg_id),
        )
        return self.vip_until(tg_id)

    def record_payment(self, user_id: int, days: int, stars: int, charge_id: str) -> bool:
        """True, если платёж новый (защита от повторной обработки)."""
        return self._exec(
            "INSERT OR IGNORE INTO payments (user_id, days, stars, charge_id) VALUES (?, ?, ?, ?)",
            (user_id, days, stars, charge_id),
        ) > 0

    # ---- фильмы ----
    def create_film(self, *, title: str, kind: str, owner_id: int, is_vip: bool = False,
                    url: str | None = None, video_file_id: str | None = None,
                    photo_file_id: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO films (title, kind, url, video_file_id, photo_file_id, owner_id, is_vip)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, kind, url, video_file_id, photo_file_id, owner_id, 1 if is_vip else 0),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_film(self, film_id: int) -> dict | None:
        return self._one("SELECT * FROM films WHERE id = ?", (film_id,))

    @staticmethod
    def _where(active: bool | None, owner_id: int | None) -> tuple[str, list]:
        clauses, params = [], []
        if active is not None:
            clauses.append("is_active = ?")
            params.append(1 if active else 0)
        if owner_id is not None:
            clauses.append("owner_id = ?")
            params.append(owner_id)
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def count_films(self, *, active: bool | None = None, owner_id: int | None = None) -> int:
        where, params = self._where(active, owner_id)
        return self._one(f"SELECT COUNT(*) AS n FROM films{where}", tuple(params))["n"]

    def list_films(self, *, active: bool | None = None, owner_id: int | None = None,
                   top: bool = False, limit: int = 10, offset: int = 0) -> list[dict]:
        where, params = self._where(active, owner_id)
        order = "views DESC, id DESC" if top else "id DESC"
        return self._all(
            f"SELECT * FROM films{where} ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )

    def update_film(self, film_id: int, **fields: Any) -> bool:
        allowed = {"title", "url", "is_active", "is_vip"}
        items = [(k, v) for k, v in fields.items() if k in allowed]
        if not items:
            return False
        sets = ", ".join(f"{k} = ?" for k, _ in items)
        return self._exec(f"UPDATE films SET {sets} WHERE id = ?", (*[v for _, v in items], film_id)) > 0

    def delete_film(self, film_id: int) -> bool:
        return self._exec("DELETE FROM films WHERE id = ?", (film_id,)) > 0

    def add_view(self, film_id: int) -> None:
        self._exec("UPDATE films SET views = views + 1 WHERE id = ?", (film_id,))

    def stats(self) -> dict:
        row = self._one(
            """SELECT COUNT(*) AS films, COALESCE(SUM(is_active), 0) AS active,
                      COALESCE(SUM(is_vip), 0) AS vip, COALESCE(SUM(views), 0) AS views FROM films"""
        ) or {}
        row["users"] = self._one("SELECT COUNT(*) AS n FROM users")["n"]
        row["staff"] = self._one("SELECT COUNT(*) AS n FROM users WHERE role IN ('admin','editor')")["n"]
        row["vip_users"] = self._one(
            "SELECT COUNT(*) AS n FROM users WHERE vip_until > datetime('now')")["n"]
        pay = self._one("SELECT COUNT(*) AS n, COALESCE(SUM(stars), 0) AS stars FROM payments") or {}
        row["payments"] = pay.get("n", 0)
        row["stars"] = pay.get("stars", 0)
        return row


# ============================================================================
# Умный поиск: опечатки, части слов, похожие названия
# ============================================================================
def normalize(text: str) -> str:
    t = text.lower().replace("ё", "е")
    t = re.sub(r"[^\w\s]+", " ", t)
    return " ".join(t.split())


def _token_score(word: str, tokens: list[str]) -> float:
    best = 0.0
    for tok in tokens:
        if tok == word:
            return 1.0
        if len(word) >= 2 and tok.startswith(word):
            best = max(best, 0.95)  # «интер» → «интерстеллар»
            continue
        if len(tok) >= 3 and word.startswith(tok):
            best = max(best, 0.9)
            continue
        ratio = SequenceMatcher(None, word, tok).ratio()
        threshold = 0.7 if len(word) >= 4 else 0.99  # опечатки в длинных словах
        if ratio >= threshold:
            best = max(best, ratio * 0.9)
    return best


def similarity(query: str, title: str) -> float:
    q, t = normalize(query), normalize(title)
    if not q or not t:
        return 0.0
    if q in t:
        return 1.0
    q_tokens, t_tokens = q.split(), t.split()
    token_score = sum(_token_score(w, t_tokens) for w in q_tokens) / len(q_tokens)
    whole = SequenceMatcher(None, q, t).ratio() * 0.9
    return max(token_score, whole)


def rank_films(films: list[dict], query: str, *, min_score: float, limit: int,
               exclude_id: int | None = None) -> list[dict]:
    scored = []
    for f in films:
        if f["id"] == exclude_id:
            continue
        score = similarity(query, f["title"])
        if score >= min_score:
            scored.append((score, f))
    scored.sort(key=lambda x: (-x[0], -x[1]["views"], -x[1]["id"]))
    return [f for _, f in scored[:limit]]


def smart_search(films: list[dict], query: str, limit: int = 12) -> tuple[list[dict], bool]:
    """Возвращает (результаты, точное_совпадение). Если точных нет — похожие."""
    exact = rank_films(films, query, min_score=0.6, limit=limit)
    if exact:
        return exact, True
    return rank_films(films, query, min_score=0.3, limit=limit), False


# ============================================================================
# Telegram Mini App: проверка подписи initData
# ============================================================================
def check_init_data(init_data: str, token: str, max_age: int = 86400) -> dict | None:
    """Проверяет подпись WebApp.initData (алгоритм Telegram) и возвращает пользователя."""
    if not init_data:
        return None
    params = dict(parse_qsl(init_data, keep_blank_values=True))
    their_hash = params.pop("hash", None)
    if not their_hash:
        return None
    check_string = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
    secret = hmac.new(b"WebAppData", token.encode(), hashlib.sha256).digest()
    expected = hmac.new(secret, check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, their_hash):
        return None
    try:
        if datetime.now(timezone.utc).timestamp() - int(params.get("auth_date", 0)) > max_age:
            return None
        return json.loads(params.get("user", "{}")) or None
    except (ValueError, json.JSONDecodeError):
        return None


# ============================================================================
# Тексты, карточки, клавиатуры
# ============================================================================
def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def btn(text: str, data: str | None = None, *, icon: str | None = None, url: str | None = None,
        web_app: WebAppInfo | None = None, style: str | None = None) -> InlineKeyboardButton:
    kwargs: dict[str, Any] = {}
    if icon:
        if icon in EMOJI_IDS:
            kwargs["icon_custom_emoji_id"] = EMOJI_IDS[icon]
        else:
            fallback = ICON_FALLBACK.get(icon, "")
            text = f"{fallback} {text}".strip()
    if style:
        kwargs["style"] = style  # primary | success | danger
    if url:
        kwargs["url"] = url
    elif web_app:
        kwargs["web_app"] = web_app
    else:
        kwargs["callback_data"] = data
    return InlineKeyboardButton(text=text, **kwargs)


def rows_kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def film_badge(film: dict) -> str:
    if film["is_vip"]:
        return "💎"
    return "▶️" if film["kind"] == "video" else "🔗"


def kind_text(film: dict) -> str:
    base = "видео в боте" if film["kind"] == "video" else "по ссылке"
    return f"{base} · VIP" if film["is_vip"] else base


def films_grid(films: list[dict]) -> list[list[InlineKeyboardButton]]:
    rows, row = [], []
    for f in films:
        title = f["title"] if len(f["title"]) <= 26 else f["title"][:25] + "…"
        row.append(btn(f"{film_badge(f)} {title}", f"f:{f['id']}"))
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
        btn("", f"{prefix}:{page - 1}", icon="prev") if page > 0 else btn(" ", "noop"),
        btn(f"{page + 1} / {pages}", "noop"),
        btn("", f"{prefix}:{page + 1}", icon="next") if page < pages - 1 else btn(" ", "noop"),
    ]


def page_bounds(total: int, page: int, size: int) -> tuple[int, int, int]:
    pages = max(1, math.ceil(total / size))
    page = min(max(page, 0), pages - 1)
    return page, pages, page * size


def main_reply_kb(role: str, webapp_url: str | None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🎬 Каталог"), KeyboardButton(text="🔥 Популярное")],
        [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="💎 VIP")],
    ]
    if webapp_url:
        rows.append([KeyboardButton(text="📱 Открыть Mini App", web_app=WebAppInfo(url=webapp_url))])
    last = [KeyboardButton(text="ℹ️ Помощь")]
    if at_least(role, EDITOR):
        last.append(KeyboardButton(text="⚙️ Админ-панель"))
    rows.append(last)
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True,
                               input_field_placeholder="Выберите раздел или введите название фильма")


def admin_panel_kb(role: str) -> InlineKeyboardMarkup:
    rows = [
        [btn("Добавить фильм", "a:add", icon="add", style="success")],
        [btn("Мои фильмы" if role == EDITOR else "Все фильмы", "a:films:0", icon="films")],
    ]
    if at_least(role, ADMIN):
        rows.append([btn("Команда", "a:team", icon="team"), btn("Статистика", "a:stats", icon="stats")])
        rows.append([btn("Выдать VIP", "a:gvip", icon="vip")])
    rows.append([btn("В главное меню", "m:home", icon="home")])
    return rows_kb(rows)


WELCOME = (
    "🕷 <b>Добро пожаловать</b>\n"
    "━━━━━━━━━━━━━━━\n"
    "Фильмы из TikTok в одном месте.\n"
    "▶️ смотрим прямо здесь, 🔗 открываем по ссылке, 💎 VIP-фильмы доступны по подписке.\n\n"
    "Выберите раздел кнопками внизу."
)
HELP = (
    "ℹ️ <b>Как пользоваться</b>\n"
    "━━━━━━━━━━━━━━━\n"
    "🎬 <b>Каталог</b> — все фильмы сеткой, листается по страницам\n"
    "🔥 <b>Популярное</b> — самые просматриваемые\n"
    "🔍 <b>Поиск</b> — пишите часть названия, с опечатками тоже найдём\n"
    "💎 <b>VIP</b> — подписка на закрытые фильмы\n"
    "📱 <b>Mini App</b> — каталог и просмотр в отдельном окне\n\n"
    "Значки: ▶️ видео в боте · 🔗 по ссылке · 💎 VIP"
)


# ============================================================================
# Отправка фильмов
# ============================================================================
async def send_film(bot: Bot, chat_id: int, film: dict) -> None:
    """Карточка фильма: видео в боте или баннер/видео со ссылкой «Смотреть»."""
    caption = (
        f"🕷 <b>{esc(film['title'])}</b>\n"
        f"━━━━━━━━━━━━━━━\n"
        f"{film_badge(film)} {kind_text(film)} · 👁 {film['views']}"
    )
    # у превью (мастер добавления) ещё нет id — кнопку «Похожие» не показываем
    similar = [btn("Похожие фильмы", f"sim:{film['id']}", icon="similar")] if film.get("id") else []
    if film["kind"] == "video":
        await bot.send_video(chat_id, film["video_file_id"], caption=caption,
                             supports_streaming=True, reply_markup=rows_kb([similar]) if similar else None)
        return
    rows = [[btn("Смотреть", url=film["url"], style="primary")]]
    if similar:
        rows.append(similar)
    kb = rows_kb(rows)
    if film.get("photo_file_id"):
        await bot.send_photo(chat_id, film["photo_file_id"], caption=caption, reply_markup=kb)
    elif film.get("video_file_id"):
        await bot.send_video(chat_id, film["video_file_id"], caption=caption, reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, reply_markup=kb)


def vip_locked_text(film: dict) -> str:
    return (
        f"🔒 <b>{esc(film['title'])}</b>\n"
        "━━━━━━━━━━━━━━━\n"
        "Этот фильм доступен по VIP-подписке.\n"
        "Оформите её за Telegram Stars — доступ откроется сразу после оплаты."
    )


def fmt_date(value: str) -> str:
    dt = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return dt.astimezone(TZ_LOCAL).strftime("%d.%m.%Y")


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
    confirm = State()


class Edit(StatesGroup):
    title = State()
    link = State()


class Team(StatesGroup):
    wait_id = State()


class VipGrant(StatesGroup):
    wait_id = State()
    wait_days = State()


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
    """Редактирует сообщение, если это текст; иначе отправляет новое."""
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


def build_router(cfg: Config, db: Database) -> Router:
    router = Router(name="main")

    def has_access(role: str, uid: int, film: dict) -> bool:
        """VIP-фильм доступен сотрудникам и активным VIP-подписчикам."""
        if not film["is_vip"]:
            return True
        return at_least(role, EDITOR) or db.is_vip(uid)

    async def deny(target: Message | CallbackQuery) -> None:
        if isinstance(target, CallbackQuery):
            await target.answer("Недостаточно прав", show_alert=True)
        else:
            await target.answer("Недостаточно прав для этого действия.")

    def catalog_view(page: int) -> tuple[str, InlineKeyboardMarkup] | None:
        total = db.count_films(active=True)
        if total == 0:
            return None
        page, pages, offset = page_bounds(total, page, CATALOG_PAGE)
        films = db.list_films(active=True, limit=CATALOG_PAGE, offset=offset)
        rows = films_grid(films)
        nav = pager("cat", page, pages)
        if nav:
            rows.append(nav)
        rows.append([btn("Поиск", "m:search", icon="search"), btn("Популярное", "m:top", icon="fire")])
        text = f"🎬 <b>Каталог</b>\n━━━━━━━━━━━━━━━\nВсего фильмов: {total} · страница {page + 1} из {pages}"
        return text, rows_kb(rows)

    async def show_home(message: Message, role: str, uid: int) -> None:
        vip = db.vip_until(uid)
        status = f"💎 VIP до {fmt_date(vip)}" if vip else "Статус: обычный пользователь"
        if at_least(role, EDITOR):
            status = f"{ROLE_TITLES[role]} · полный доступ"
        await message.answer(f"{WELCOME}\n\n<i>{status}</i>", reply_markup=main_reply_kb(role, cfg.webapp_url))

    async def show_catalog(message: Message, page: int = 0) -> None:
        view = catalog_view(page)
        if view is None:
            await message.answer("Пока в каталоге пусто. Загляните позже!")
            return
        await message.answer(view[0], reply_markup=view[1])

    async def show_top(message: Message) -> None:
        films = db.list_films(active=True, top=True, limit=10)
        if not films:
            await message.answer("Пока нет фильмов.")
            return
        await message.answer("🔥 <b>Популярное</b>\n━━━━━━━━━━━━━━━\nСамое просматриваемое сейчас.",
                             reply_markup=rows_kb(films_grid(films)))

    async def show_vip(target: Message | CallbackQuery, role: str, uid: int) -> None:
        if at_least(role, EDITOR):
            text = f"💎 <b>VIP</b>\n━━━━━━━━━━━━━━━\n{ROLE_TITLES[role]}: у вас доступ ко всем фильмам."
            rows = [[btn("Назад", "m:home", icon="back")]]
        else:
            until = db.vip_until(uid)
            status = f"Активна до <b>{fmt_date(until)}</b>" if until else "Не активна"
            text = (
                "💎 <b>VIP-подписка</b>\n━━━━━━━━━━━━━━━\n"
                "С VIP открываются все закрытые фильмы.\n"
                "Оплата Telegram Stars, доступ сразу после оплаты. Срок продлевается.\n\n"
                f"Ваш статус: {status}"
            )
            rows = []
            for days, (title, stars) in VIP_PLANS.items():
                style = "primary" if days == 90 else None
                rows.append([btn(f"{title} · {stars} ⭐", f"vip:buy:{days}", icon="vip", style=style)])
            rows.append([btn("Назад", "m:home", icon="back")])
        if isinstance(target, CallbackQuery):
            await safe_show(target, text, rows_kb(rows))
            await target.answer()
        else:
            await target.answer(text, reply_markup=rows_kb(rows))

    async def show_admin(target: Message | CallbackQuery, role: str) -> None:
        text = f"⚙️ <b>Админ-панель</b>\n━━━━━━━━━━━━━━━\n{ROLE_TITLES[role]}"
        if isinstance(target, CallbackQuery):
            await safe_show(target, text, admin_panel_kb(role))
        else:
            await target.answer(text, reply_markup=admin_panel_kb(role))

    # ------------------------------------------------------------------
    # Команды и deep links
    # ------------------------------------------------------------------
    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, bot: Bot, role: str,
                       state: FSMContext) -> None:
        await state.clear()
        payload = (command.args or "").strip()
        if payload == "vip":
            return await show_vip(message, role, message.from_user.id)
        match = DEEP_LINK_RE.fullmatch(payload)
        if match:
            film = db.get_film(int(match.group(1)))
            if film and film["is_active"]:
                if not has_access(role, message.from_user.id, film):
                    await message.answer(vip_locked_text(film), reply_markup=rows_kb([
                        [btn("Оформить VIP", "vip:menu", icon="vip", style="primary")]]))
                    return
                db.add_view(film["id"])
                await send_film(bot, message.chat.id, film)
                return
            await message.answer("Этот фильм больше недоступен.")
        await show_home(message, role, message.from_user.id)

    @router.message(Command("menu"))
    async def on_menu(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        await show_home(message, role, message.from_user.id)

    @router.message(Command("cancel"))
    async def on_cancel(message: Message, role: str, state: FSMContext) -> None:
        had = await state.get_state() is not None
        await state.clear()
        await message.answer("Действие отменено." if had else "Отменять нечего.",
                             reply_markup=main_reply_kb(role, cfg.webapp_url))

    @router.message(Command("admin"))
    async def on_admin_cmd(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        if not at_least(role, EDITOR):
            return await deny(message)
        await show_admin(message, role)

    @router.message(Command("add"))
    async def on_add_cmd(message: Message, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(message)
        await start_add(message, state)

    # ------------------------------------------------------------------
    # Кнопки главного меню (работают из любого состояния)
    # ------------------------------------------------------------------
    @router.message(F.text == "🎬 Каталог")
    async def on_btn_catalog(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_catalog(message, 0)

    @router.message(F.text == "🔥 Популярное")
    async def on_btn_top(message: Message, state: FSMContext) -> None:
        await state.clear()
        await show_top(message)

    @router.message(F.text == "🔍 Поиск")
    async def on_btn_search(message: Message, state: FSMContext) -> None:
        await state.set_state(Search.query)
        await message.answer(
            "🔍 <b>Поиск</b>\n━━━━━━━━━━━━━━━\n"
            "Напишите название или его часть.\n"
            "Опечатки тоже подойдут: «интерстелар» найдёт «Интерстеллар».\n\n"
            "/cancel — выйти из поиска"
        )

    @router.message(F.text == "💎 VIP")
    async def on_btn_vip(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        await show_vip(message, role, message.from_user.id)

    @router.message(F.text == "ℹ️ Помощь")
    async def on_btn_help(message: Message, state: FSMContext) -> None:
        await state.clear()
        await message.answer(HELP)

    @router.message(F.text == "⚙️ Админ-панель")
    async def on_btn_admin(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        if not at_least(role, EDITOR):
            return await deny(message)
        await show_admin(message, role)

    # ------------------------------------------------------------------
    # Поиск
    # ------------------------------------------------------------------
    @router.message(Search.query, F.text)
    async def on_search(message: Message, state: FSMContext) -> None:
        query = message.text.strip()[:60]
        await state.clear()
        films = db.list_films(active=True, limit=1000)
        results, exact = smart_search(films, query)
        if not results:
            await message.answer(
                f"По запросу «{esc(query)}» ничего похожего нет.\n"
                "Попробуйте другое слово или посмотрите каталог.",
                reply_markup=rows_kb([[btn("Каталог", "m:catalog", icon="catalog")]]),
            )
            return
        head = (f"🔍 Найдено: {len(results)}" if exact else
                f"Точного совпадения нет. Похожие по «{esc(query)}»:")
        await message.answer(head, reply_markup=rows_kb(films_grid(results)))

    # ------------------------------------------------------------------
    # Мастер добавления фильма (редактор+)
    # ------------------------------------------------------------------
    async def start_add(target: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddFilm.media)
        await target.answer(
            "🎞 <b>Новый фильм</b>\n━━━━━━━━━━━━━━━\n"
            "Шаг 1 из 4. Пришлите <b>видео</b> или <b>фото-баннер</b>.\n/cancel — отмена"
        )

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
        await message.answer("Шаг 2 из 4. Напишите <b>название</b> фильма.")

    @router.message(AddFilm.media)
    async def on_media_bad(message: Message) -> None:
        await message.answer("Нужно видео или фото-баннер.")

    @router.message(AddFilm.title, F.text)
    async def on_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip()[:100])
        await state.set_state(AddFilm.kind)
        await message.answer(
            "Шаг 3 из 4. Как открывать фильм?\n\n"
            "🔗 <b>По ссылке</b> — кнопка «Смотреть» ведёт на ваш переходник.\n"
            "▶️ <b>В боте</b> — фильм проигрывается в Telegram и в Mini App.",
            reply_markup=rows_kb([
                [btn("По ссылке", "wz:kind:link", icon="link"), btn("В боте", "wz:kind:video", icon="play")],
                [btn("Отмена", "wz:cancel", icon="cancel")],
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
        data = await state.get_data()
        if kind == "link":
            await state.set_state(AddFilm.url)
            await callback.message.answer("Шаг 4 из 4. Пришлите ссылку-переходник (https://...).")
        elif data.get("video_file_id"):
            await show_confirm(callback.message, state)
        else:
            await state.set_state(AddFilm.video)
            await callback.message.answer("Шаг 4 из 4. Пришлите видеофайл фильма.")

    @router.message(AddFilm.url, F.text)
    async def on_url(message: Message, state: FSMContext) -> None:
        url = message.text.strip()
        if not URL_RE.match(url):
            await message.answer("Нужна ссылка, начинающаяся с https:// или http://")
            return
        await state.update_data(url=url)
        await show_confirm(message, state)

    @router.message(AddFilm.video, F.video)
    async def on_video(message: Message, state: FSMContext) -> None:
        await state.update_data(video_file_id=message.video.file_id)
        await show_confirm(message, state)

    @router.message(AddFilm.video)
    async def on_video_bad(message: Message) -> None:
        await message.answer("Пришлите именно видеофайл.")

    def confirm_kb(is_vip: bool) -> InlineKeyboardMarkup:
        return rows_kb([
            [btn("Закрытый VIP" if not is_vip else "Сделать бесплатным",
                 "wz:vip", icon="vip")],
            [btn("Сохранить", "wz:save", icon="ok", style="success"), btn("Отмена", "wz:cancel", icon="no")],
        ])

    async def show_confirm(message: Message, state: FSMContext) -> None:
        await state.set_state(AddFilm.confirm)
        data = await state.get_data()
        await message.answer("👀 <b>Превью карточки</b>")
        await send_film(message.bot, message.chat.id, {**data, "views": 0, "is_vip": data.get("is_vip", False)})
        access = "💎 VIP-фильм" if data.get("is_vip") else "🆓 Бесплатный"
        await message.answer(f"Сохранить фильм? · {access}", reply_markup=confirm_kb(bool(data.get("is_vip"))))

    @router.callback_query(AddFilm.confirm, F.data == "wz:vip")
    async def on_toggle_vip_draft(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        is_vip = not data.get("is_vip", False)
        await state.update_data(is_vip=is_vip)
        access = "💎 VIP-фильм" if is_vip else "🆓 Бесплатный"
        await callback.answer("Доступ изменён")
        await safe_show(callback, f"Сохранить фильм? · {access}", confirm_kb(is_vip))

    @router.callback_query(AddFilm.confirm, F.data == "wz:save")
    async def on_save(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        data = await state.get_data()
        film_id = db.create_film(
            title=data["title"],
            kind=data["kind"],
            owner_id=callback.from_user.id,
            is_vip=bool(data.get("is_vip")),
            url=data.get("url"),
            video_file_id=data.get("video_file_id"),
            photo_file_id=data.get("photo_file_id"),
        )
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer(
            f"✅ <b>Фильм #{film_id} добавлен</b>\nОн уже виден в каталоге.",
            reply_markup=rows_kb([[btn("Мои фильмы", "a:films:0", icon="films"),
                                   btn("Добавить ещё", "a:add", icon="add")]]),
        )

    @router.callback_query(F.data == "wz:cancel")
    async def on_cancel_btn(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await callback.message.answer("Добавление отменено.")

    # ------------------------------------------------------------------
    # Управление фильмами (редактор — свои, админ+ — все)
    # ------------------------------------------------------------------
    @router.callback_query(F.data.startswith("a:films:"))
    async def on_films_list(callback: CallbackQuery, role: str) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        scope_owner = None if at_least(role, ADMIN) else callback.from_user.id
        total = db.count_films(owner_id=scope_owner)
        page, pages, offset = page_bounds(total, int(callback.data.rsplit(":", 1)[1]), ADMIN_PAGE)
        films = db.list_films(owner_id=scope_owner, limit=ADMIN_PAGE, offset=offset)
        rows = [
            [btn(f"{'🟢' if f['is_active'] else '⚪'} {film_badge(f)} #{f['id']} {f['title'][:24]}",
                 f"a:film:{f['id']}")]
            for f in films
        ]
        nav = pager("a:films", page, pages)
        if nav:
            rows.append(nav)
        rows.append([btn("Назад", "a:panel", icon="back")])
        text = f"🎞 <b>{'Все фильмы' if scope_owner is None else 'Мои фильмы'}</b> · {total}"
        if not films:
            text += "\n\nПока пусто. Нажмите «Добавить фильм»."
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
        status = "показан в каталоге" if film["is_active"] else "скрыт"
        text = (
            f"🎞 <b>{esc(film['title'])}</b>\n"
            "━━━━━━━━━━━━━━━\n"
            f"ID: {film['id']} · {kind_text(film)}\n"
            f"👁 Просмотров: {film['views']}\n"
            f"Статус: {status}"
        )
        rows = [[btn("Открыть", f"a:open:{film['id']}", icon="play"),
                 btn("Название", f"a:rename:{film['id']}", icon="edit")]]
        if film["kind"] == "link":
            rows.append([btn("Сменить ссылку", f"a:link:{film['id']}", icon="link")])
        rows.append([
            btn("Бесплатный" if film["is_vip"] else "Сделать VIP", f"a:vipf:{film['id']}", icon="vip"),
            btn("Скрыть" if film["is_active"] else "Показать", f"a:toggle:{film['id']}", icon="pause"),
        ])
        rows.append([btn("Удалить", f"a:del:{film['id']}", icon="trash", style="danger")])
        rows.append([btn("К списку", "a:films:0", icon="back")])
        await safe_show(callback, text, rows_kb(rows))

    @router.callback_query(F.data.regexp(r"^a:film:\d+$"))
    async def on_film_card(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        await callback.answer()
        await render_card(callback, film)

    @router.callback_query(F.data.regexp(r"^a:open:\d+$"))
    async def on_open(callback: CallbackQuery, role: str, bot: Bot) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        await callback.answer()
        await send_film(bot, callback.message.chat.id, film)

    @router.callback_query(F.data.regexp(r"^a:toggle:\d+$"))
    async def on_toggle(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        db.update_film(film["id"], is_active=0 if film["is_active"] else 1)
        await callback.answer("Готово")
        await render_card(callback, db.get_film(film["id"]))

    @router.callback_query(F.data.regexp(r"^a:vipf:\d+$"))
    async def on_toggle_vip(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        db.update_film(film["id"], is_vip=0 if film["is_vip"] else 1)
        await callback.answer("Доступ изменён")
        await render_card(callback, db.get_film(film["id"]))

    @router.callback_query(F.data.regexp(r"^a:del:\d+$"))
    async def on_delete_ask(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        await callback.answer()
        await safe_show(
            callback,
            f"Удалить фильм #{film['id']} «{esc(film['title'])}»?\nЭто необратимо.",
            rows_kb([[btn("Да, удалить", f"a:delok:{film['id']}", icon="trash", style="danger"),
                      btn("Нет", f"a:film:{film['id']}", icon="no")]]),
        )

    @router.callback_query(F.data.regexp(r"^a:delok:\d+$"))
    async def on_delete(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        db.delete_film(film["id"])
        await callback.answer("Фильм удалён")
        await safe_show(callback, f"🗑 Фильм #{film['id']} удалён.",
                        rows_kb([[btn("К списку", "a:films:0", icon="back")]]))

    @router.callback_query(F.data.regexp(r"^a:(rename|link):\d+$"))
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
        if action == "rename":
            await state.set_state(Edit.title)
            await callback.message.answer("Пришлите новое название.\n/cancel — отмена")
        else:
            await state.set_state(Edit.link)
            await callback.message.answer("Пришлите новую ссылку (https://...).\n/cancel — отмена")

    @router.message(Edit.title, F.text)
    async def on_rename(message: Message, role: str, state: FSMContext) -> None:
        film = db.get_film((await state.get_data())["film_id"])
        await state.clear()
        if not film or not can_manage_film(role, message.from_user.id, film):
            return await deny(message)
        db.update_film(film["id"], title=message.text.strip()[:100])
        await message.answer("✅ Название обновлено.",
                             reply_markup=rows_kb([[btn("К фильму", f"a:film:{film['id']}", icon="back")]]))

    @router.message(Edit.link, F.text)
    async def on_relink(message: Message, role: str, state: FSMContext) -> None:
        url = message.text.strip()
        if not URL_RE.match(url):
            await message.answer("Нужна ссылка, начинающаяся с https://")
            return
        film = db.get_film((await state.get_data())["film_id"])
        await state.clear()
        if not film or not can_manage_film(role, message.from_user.id, film):
            return await deny(message)
        db.update_film(film["id"], url=url)
        await message.answer("✅ Ссылка обновлена.",
                             reply_markup=rows_kb([[btn("К фильму", f"a:film:{film['id']}", icon="back")]]))

    # ------------------------------------------------------------------
    # Команда: роли (админ+)
    # ------------------------------------------------------------------
    async def team_view(target: Message | CallbackQuery, actor_role: str) -> None:
        lines = ["👥 <b>Команда</b>", "━━━━━━━━━━━━━━━", f"{ROLE_TITLES[OWNER]} · <code>{cfg.owner_id}</code>"]
        rows: list[list[InlineKeyboardButton]] = []
        staff = db.staff()
        for m in staff:
            name = esc(m["first_name"] or m["username"] or m["tg_id"])
            lines.append(f"{ROLE_TITLES[m['role']]} · {name} · <code>{m['tg_id']}</code>")
            if can_change_role(actor_role, m["role"], USER):
                rows.append([btn(f"Снять роль: {m['tg_id']}", f"a:rm:{m['tg_id']}", icon="trash", style="danger")])
        if not staff:
            lines.append("\nПока нет админов и редакторов.")
        add_row = [btn("Редактор", "a:addm:editor", icon="add", style="success")]
        if actor_role == OWNER:
            add_row.append(btn("Админ", "a:addm:admin", icon="admin", style="primary"))
        rows.append(add_row)
        rows.append([btn("Назад", "a:panel", icon="back")])
        text = "\n".join(lines) + (
            "\n\nЧтобы выдать роль, пришлите Telegram ID пользователя или перешлите его сообщение."
        )
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
        await callback.message.answer(
            f"Пришлите Telegram ID для роли «{ROLE_TITLES[new_role]}» "
            "или перешлите сообщение пользователя.\n/cancel — отмена"
        )

    @router.message(Team.wait_id)
    async def on_add_member(message: Message, role: str, bot: Bot, state: FSMContext) -> None:
        new_role = (await state.get_data()).get("new_role", EDITOR)
        await state.clear()
        target_id = extract_user_id(message)
        if target_id is None:
            await message.answer("Нужен числовой Telegram ID или пересланное сообщение. Повторите из меню «Команда».")
            return
        if target_id == cfg.owner_id:
            await message.answer("Это владелец бота, его роль не меняется.")
            return
        if not can_change_role(role, db.get_role(target_id), new_role):
            return await deny(message)
        db.set_role(target_id, new_role, added_by=message.from_user.id)
        await message.answer(f"✅ Роль «{ROLE_TITLES[new_role]}» выдана: <code>{target_id}</code>")
        try:
            await bot.send_message(
                target_id,
                f"Вам выдана роль «{ROLE_TITLES[new_role]}». Меню: /start",
                reply_markup=main_reply_kb(new_role, cfg.webapp_url),
            )
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

    # ------------------------------------------------------------------
    # Выдача VIP вручную (админ+)
    # ------------------------------------------------------------------
    @router.callback_query(F.data == "a:gvip")
    async def on_gvip(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        await callback.answer()
        await state.clear()
        await state.set_state(VipGrant.wait_id)
        await callback.message.answer("Пришлите Telegram ID пользователя или перешлите его сообщение.\n/cancel — отмена")

    @router.message(VipGrant.wait_id)
    async def on_gvip_id(message: Message, state: FSMContext) -> None:
        target_id = extract_user_id(message)
        if target_id is None:
            await message.answer("Нужен числовой ID или пересланное сообщение.")
            return
        await state.update_data(target_id=target_id)
        await state.set_state(VipGrant.wait_days)
        await message.answer(
            f"Пользователь <code>{target_id}</code>. Выберите срок:",
            reply_markup=rows_kb([[btn(f"{t}", f"gvd:{d}", icon="gift", style="success")]
                                  for d, (t, _) in VIP_PLANS.items()]),
        )

    @router.callback_query(VipGrant.wait_days, F.data.startswith("gvd:"))
    async def on_gvip_days(callback: CallbackQuery, role: str, bot: Bot, state: FSMContext) -> None:
        days = int(callback.data.split(":")[1])
        target_id = (await state.get_data())["target_id"]
        await state.clear()
        until = db.grant_vip(target_id, days)
        await callback.answer("VIP выдан")
        await callback.message.answer(f"💎 VIP выдан <code>{target_id}</code> до {fmt_date(until)}.")
        try:
            await bot.send_message(target_id, f"💎 Вам выдан VIP на {VIP_PLANS[days][0]}. Приятного просмотра!")
        except TelegramAPIError:
            pass

    # ------------------------------------------------------------------
    # Статистика
    # ------------------------------------------------------------------
    @router.callback_query(F.data == "a:stats")
    async def on_stats(callback: CallbackQuery, role: str) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        s = db.stats()
        text = (
            "📊 <b>Статистика</b>\n━━━━━━━━━━━━━━━\n"
            f"Фильмов: {s['films']} · показано: {s['active']} · VIP: {s['vip']}\n"
            f"Просмотров: {s['views']}\n"
            f"Пользователей: {s['users']} · сотрудников: {s['staff']}\n"
            f"VIP-подписчиков: {s['vip_users']}\n"
            f"Оплат: {s['payments']} · заработано: {s['stars']} ⭐"
        )
        await safe_show(callback, text, rows_kb([[btn("Назад", "a:panel", icon="back")]]))
        await callback.answer()

    @router.callback_query(F.data == "a:panel")
    async def on_panel(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        await state.clear()
        await show_admin(callback, role)
        await callback.answer()

    # ------------------------------------------------------------------
    # VIP: покупка за Telegram Stars
    # ------------------------------------------------------------------
    @router.callback_query(F.data == "vip:menu")
    async def on_vip_menu(callback: CallbackQuery, role: str) -> None:
        await show_vip(callback, role, callback.from_user.id)

    @router.callback_query(F.data.regexp(r"^vip:buy:\d+$"))
    async def on_vip_buy(callback: CallbackQuery, bot: Bot) -> None:
        days = int(callback.data.split(":")[-1])
        if days not in VIP_PLANS:
            return await callback.answer("Тариф недоступен", show_alert=True)
        title, stars = VIP_PLANS[days]
        await callback.answer()
        await bot.send_invoice(
            chat_id=callback.message.chat.id,
            title=f"VIP-подписка · {title}",
            description="Доступ ко всем закрытым фильмам каталога.",
            payload=f"vip:{days}:{callback.from_user.id}",
            provider_token="",  # Telegram Stars не требуют платёжного провайдера
            currency="XTR",
            prices=[LabeledPrice(label=f"VIP · {title}", amount=stars)],
        )

    @router.pre_checkout_query()
    async def on_pre_checkout(query: PreCheckoutQuery) -> None:
        ok = False
        try:
            kind, days_raw, uid_raw = query.invoice_payload.split(":")
            days = int(days_raw)
            ok = (kind == "vip" and days in VIP_PLANS and int(uid_raw) == query.from_user.id
                  and query.total_amount == VIP_PLANS[days][1])
        except (ValueError, KeyError):
            ok = False
        await query.answer(ok=ok, error_message=None if ok else "Тариф недоступен, попробуйте позже.")

    @router.message(F.successful_payment)
    async def on_paid(message: Message) -> None:
        sp = message.successful_payment
        days = int(sp.invoice_payload.split(":")[1])
        uid = message.from_user.id
        if db.record_payment(uid, days, sp.total_amount, sp.telegram_payment_charge_id):
            until = db.grant_vip(uid, days)
        else:  # повторное уведомление — не выдаём дважды
            until = db.vip_until(uid)
        await message.answer(
            "💎 <b>Оплата прошла, VIP активирован!</b>\n━━━━━━━━━━━━━━━\n"
            f"Срок: {VIP_PLANS[days][0]}\nДоступ до: <b>{fmt_date(until)}</b>\n\n"
            "Закрытые фильмы теперь открыты.",
            reply_markup=rows_kb([[btn("В каталог", "m:catalog", icon="catalog", style="primary")]]),
        )

    # ------------------------------------------------------------------
    # Каталог и карточки (для всех)
    # ------------------------------------------------------------------
    @router.callback_query(F.data.regexp(r"^cat:\d+$"))
    async def on_catalog_page(callback: CallbackQuery) -> None:
        view = catalog_view(int(callback.data.split(":")[1]))
        if view is None:
            return await callback.answer("Каталог пуст", show_alert=True)
        await safe_show(callback, view[0], view[1])
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^f:\d+$"))
    async def on_film_button(callback: CallbackQuery, role: str, bot: Bot) -> None:
        film = db.get_film(int(callback.data.split(":")[1]))
        if not film or not film["is_active"]:
            return await callback.answer("Фильм недоступен", show_alert=True)
        if not has_access(role, callback.from_user.id, film):
            await callback.answer()
            await callback.message.answer(vip_locked_text(film), reply_markup=rows_kb([
                [btn("Оформить VIP", "vip:menu", icon="vip", style="primary")]]))
            return
        db.add_view(film["id"])
        await callback.answer()
        await send_film(bot, callback.message.chat.id, db.get_film(film["id"]))

    @router.callback_query(F.data.regexp(r"^sim:\d+$"))
    async def on_similar(callback: CallbackQuery) -> None:
        film = db.get_film(int(callback.data.split(":")[1]))
        if not film:
            return await callback.answer("Фильм не найден", show_alert=True)
        films = db.list_films(active=True, limit=1000)
        similar = rank_films(films, film["title"], min_score=0.3, limit=6, exclude_id=film["id"])
        await callback.answer()
        if not similar:
            await callback.message.answer("Похожих фильмов пока нет.")
            return
        await callback.message.answer(f"🧩 <b>Похожие на «{esc(film['title'])}»</b>",
                                      reply_markup=rows_kb(films_grid(similar)))

    @router.callback_query(F.data.in_({"m:home", "m:catalog", "m:search", "m:top"}))
    async def on_inline_nav(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        await callback.answer()
        await state.clear()
        action = callback.data.split(":")[1]
        if action == "home":
            await show_home(callback.message, role, callback.from_user.id)
        elif action == "catalog":
            await show_catalog(callback.message, 0)
        elif action == "top":
            await show_top(callback.message)
        else:
            await state.set_state(Search.query)
            await callback.message.answer("🔍 Напишите название фильма. /cancel — выход")

    @router.callback_query(F.data == "noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    # ------------------------------------------------------------------
    # Служебное: определить ID кастомного эмодзи (для EMOJI_IDS)
    # ------------------------------------------------------------------
    @router.message(lambda m: bool(m.entities) and any(e.type == "custom_emoji" for e in m.entities))
    async def on_emoji_probe(message: Message, role: str, state: FSMContext) -> None:
        if not at_least(role, ADMIN):
            return
        ids = [e.custom_emoji_id for e in message.entities if e.type == "custom_emoji"]
        await message.answer(
            "ID премиум-эмодзи:\n" + "\n".join(f"<code>{i}</code>" for i in ids) +
            "\n\nДобавьте их в EMOJI_IDS, например: {\"play\": \"<id>\"}"
        )

    # ------------------------------------------------------------------
    # Неизвестный текст: подсказка
    # ------------------------------------------------------------------
    @router.message(F.text)
    async def on_fallback(message: Message, role: str, state: FSMContext) -> None:
        if await state.get_state() is None:
            await message.answer("Не понял. Используйте кнопки меню ниже.",
                                 reply_markup=main_reply_kb(role, cfg.webapp_url))

    return router


def extract_user_id(message: Message) -> int | None:
    origin = message.forward_origin
    if isinstance(origin, MessageOriginUser):
        return origin.sender_user.id
    if message.text and message.text.strip().isdigit():
        return int(message.text.strip())
    return None


# ============================================================================
# HTTP: Mini App, API, стрим видео
# ============================================================================
def build_web(cfg: Config, db: Database, bot: Bot, dp: Dispatcher, bot_username: str | None) -> web.Application:
    app = web.Application()

    async def open_session(app_: web.Application) -> None:
        app_["http"] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None, sock_read=60))

    async def close_session(app_: web.Application) -> None:
        await app_["http"].close()

    app.on_startup.append(open_session)
    app.on_cleanup.append(close_session)

    def viewer(request: web.Request) -> dict | None:
        """Пользователь Mini App из подписанного initData (заголовок или ?init= для видео)."""
        init = request.headers.get("X-Telegram-Init-Data") or request.query.get("init", "")
        return check_init_data(init, cfg.bot_token)

    def viewer_access(request: web.Request) -> tuple[int | None, str]:
        user = viewer(request)
        if not user:
            return None, USER
        uid = int(user["id"])
        role = OWNER if uid == cfg.owner_id else db.get_role(uid)
        return uid, role

    def has_access(uid: int | None, role: str, film: dict) -> bool:
        if not film["is_vip"]:
            return True
        return at_least(role, EDITOR) or (uid is not None and db.is_vip(uid))

    def public_film(film: dict, uid: int | None, role: str) -> dict:
        locked = not has_access(uid, role, film)
        return {
            "id": film["id"],
            "title": film["title"],
            "kind": film["kind"],
            "vip": bool(film["is_vip"]),
            "locked": locked,
            # ссылку и видео отдаём только тем, у кого есть доступ
            "url": None if locked or film["kind"] != "link" else film["url"],
            "playable": film["kind"] == "video" and not locked,
            "hasBanner": bool(film["photo_file_id"]),
            "views": film["views"],
        }

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def mini_app(_: web.Request) -> web.FileResponse:
        return web.FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    async def api_config(_: web.Request) -> web.Response:
        return web.json_response({
            "botUsername": bot_username,
            "plans": [{"days": d, "title": t, "stars": s} for d, (t, s) in VIP_PLANS.items()],
        })

    async def api_me(request: web.Request) -> web.Response:
        uid, role = viewer_access(request)
        if uid is None:
            return web.json_response({"guest": True, "role": USER, "vip": False})
        until = db.vip_until(uid)
        return web.json_response({
            "guest": False,
            "id": uid,
            "role": role,
            "vip": bool(until) or at_least(role, EDITOR),
            "vipUntil": fmt_date(until) if until else None,
        })

    async def api_films(request: web.Request) -> web.Response:
        uid, role = viewer_access(request)
        films = [public_film(f, uid, role) for f in db.list_films(active=True, limit=500)]
        return web.json_response(films, headers={"Cache-Control": "no-store"})

    async def api_search(request: web.Request) -> web.Response:
        uid, role = viewer_access(request)
        query = request.query.get("q", "").strip()[:60]
        if not query:
            return web.json_response({"results": [], "exact": True})
        films = db.list_films(active=True, limit=1000)
        results, exact = smart_search(films, query, limit=30)
        return web.json_response({
            "results": [public_film(f, uid, role) for f in results],
            "exact": exact,
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

    async def api_video(request: web.Request) -> web.StreamResponse:
        """Стрим видео из Telegram с поддержкой перемотки (Range). Лимит Bot API — 20 МБ."""
        try:
            film = db.get_film(int(request.match_info["film_id"]))
        except ValueError:
            film = None
        if not film or not film["is_active"] or film["kind"] != "video":
            return web.json_response({"error": "not_found"}, status=404)

        uid, role = viewer_access(request)
        if not has_access(uid, role, film):
            return web.json_response({"error": "vip"}, status=403)

        try:
            tg_file = await bot.get_file(film["video_file_id"])
        except TelegramBadRequest:
            return web.json_response({"error": "too_big"}, status=413)
        except TelegramAPIError:
            return web.json_response({"error": "unavailable"}, status=502)

        if not request.headers.get("Range") or request.headers.get("Range", "").startswith("bytes=0-"):
            db.add_view(film["id"])

        upstream_headers = {}
        if "Range" in request.headers:
            upstream_headers["Range"] = request.headers["Range"]
        url = f"https://api.telegram.org/file/bot{cfg.bot_token}/{tg_file.file_path}"
        try:
            up = await app["http"].get(url, headers=upstream_headers)
        except aiohttp.ClientError as err:
            log.warning("видео недоступно: %s", err)
            return web.json_response({"error": "unavailable"}, status=502)
        try:
            if up.status >= 400:
                return web.json_response({"error": "upstream"}, status=502)
            resp = web.StreamResponse(status=up.status)
            resp.headers["Content-Type"] = up.headers.get("Content-Type", "video/mp4")
            for h in ("Content-Length", "Content-Range"):
                if h in up.headers:
                    resp.headers[h] = up.headers[h]
            resp.headers["Accept-Ranges"] = "bytes"
            resp.headers["Cache-Control"] = "private, max-age=300"
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
        finally:
            up.release()

    app.router.add_get("/health", health)
    app.router.add_get("/", mini_app)
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/me", api_me)
    app.router.add_get("/api/films", api_films)
    app.router.add_get("/api/search", api_search)
    app.router.add_get("/api/banner/{film_id}", api_banner)
    app.router.add_get("/api/video/{film_id}", api_video)

    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret).register(
        app, path=cfg.webhook_path
    )
    return app


# ============================================================================
# Запуск
# ============================================================================
async def main() -> None:
    global EMOJI_IDS
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = load_config()
    EMOJI_IDS = cfg.emoji_ids
    db = Database(cfg.db_path)

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
        BotCommand(command="vip", description="VIP-подписка"),
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="cancel", description="Отмена"),
    ])
    if cfg.webapp_url:
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="Фильмы", web_app=WebAppInfo(url=cfg.webapp_url))
        )

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
            await bot.set_webhook(
                url,
                secret_token=cfg.webhook_secret,
                allowed_updates=dp.resolve_used_update_types(),
                drop_pending_updates=False,
            )
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

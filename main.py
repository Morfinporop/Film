#!/usr/bin/env python3
"""
Telegram-бот каталога фильмов + Mini App. Весь проект запускается одним файлом:

    python main.py

Роли:
  owner  — владелец (OWNER_ID, по умолчанию 7745681987). Всё, включая назначение админов.
  admin  — назначается владельцем. Управляет любыми фильмами, назначает редакторов.
  editor — назначается админом/владельцем. Добавляет фильмы, правит и удаляет только свои.
  user   — смотрит каталог, поиск, популярное.

Роли хранятся в SQLite, поэтому выдаются кнопками в боте без перезапуска.
"""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import math
import os
import re
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

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
    MenuButtonWebApp,
    Message,
    MessageOriginUser,
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
CATALOG_PAGE = 10
ADMIN_PAGE = 8

log = logging.getLogger("film-bot")


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
    """Назначать и снимать можно только роли строго ниже своей."""
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
    added_by    INTEGER,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    last_seen   TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_users_role ON users (role);
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
        # миграция для баз, созданных старыми версиями
        cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(films)")}
        if "owner_id" not in cols:
            self.conn.execute("ALTER TABLE films ADD COLUMN owner_id INTEGER")
        self.conn.commit()

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

    # ---- пользователи и роли ----
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
        return self._all(
            "SELECT * FROM users WHERE role IN ('admin', 'editor') ORDER BY role, tg_id"
        )

    # ---- фильмы ----
    def create_film(self, *, title: str, kind: str, owner_id: int, url: str | None = None,
                    video_file_id: str | None = None, photo_file_id: str | None = None) -> int:
        cur = self.conn.execute(
            """INSERT INTO films (title, kind, url, video_file_id, photo_file_id, owner_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, kind, url, video_file_id, photo_file_id, owner_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_film(self, film_id: int) -> dict | None:
        return self._one("SELECT * FROM films WHERE id = ?", (film_id,))

    @staticmethod
    def _where(active: bool | None, owner_id: int | None, search: str | None) -> tuple[str, list]:
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
        return (" WHERE " + " AND ".join(clauses)) if clauses else "", params

    def count_films(self, *, active: bool | None = None, owner_id: int | None = None,
                    search: str | None = None) -> int:
        where, params = self._where(active, owner_id, search)
        return self._one(f"SELECT COUNT(*) AS n FROM films{where}", tuple(params))["n"]

    def list_films(self, *, active: bool | None = None, owner_id: int | None = None,
                   search: str | None = None, top: bool = False,
                   limit: int = 10, offset: int = 0) -> list[dict]:
        where, params = self._where(active, owner_id, search)
        order = "views DESC, id DESC" if top else "id DESC"
        return self._all(
            f"SELECT * FROM films{where} ORDER BY {order} LIMIT ? OFFSET ?",
            tuple(params) + (limit, offset),
        )

    def update_film(self, film_id: int, **fields: Any) -> bool:
        allowed = {"title", "url", "is_active"}
        sets = ", ".join(f"{k} = ?" for k in fields if k in allowed)
        if not sets:
            return False
        values = [v for k, v in fields.items() if k in allowed]
        return self._exec(f"UPDATE films SET {sets} WHERE id = ?", (*values, film_id)) > 0

    def delete_film(self, film_id: int) -> bool:
        return self._exec("DELETE FROM films WHERE id = ?", (film_id,)) > 0

    def add_view(self, film_id: int) -> None:
        self._exec("UPDATE films SET views = views + 1 WHERE id = ?", (film_id,))

    def stats(self) -> dict:
        row = self._one(
            """SELECT COUNT(*) AS films,
                      COALESCE(SUM(is_active), 0) AS active,
                      COALESCE(SUM(views), 0) AS views FROM films"""
        ) or {}
        row["users"] = self._one("SELECT COUNT(*) AS n FROM users")["n"]
        row["staff"] = self._one("SELECT COUNT(*) AS n FROM users WHERE role IN ('admin','editor')")["n"]
        return row


# ============================================================================
# Тексты, карточки, клавиатуры
# ============================================================================
def esc(text: Any) -> str:
    return html.escape(str(text), quote=False)


def user_label(row: dict | None, fallback: Any) -> str:
    if row and row.get("first_name"):
        return esc(row["first_name"])
    if row and row.get("username"):
        return "@" + esc(row["username"])
    return str(fallback)


def kind_label(film: dict) -> str:
    return "видео в боте" if film["kind"] == "video" else "ссылка"


async def send_film(bot: Bot, chat_id: int, film: dict) -> None:
    """Видео — в боте. Ссылочный — баннер/видео + кнопка-переход."""
    caption = f"🎬 <b>{esc(film['title'])}</b>"
    if film["kind"] == "video":
        await bot.send_video(chat_id, film["video_file_id"], caption=caption, supports_streaming=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Смотреть", url=film["url"])]])
    if film.get("photo_file_id"):
        await bot.send_photo(chat_id, film["photo_file_id"], caption=caption, reply_markup=kb)
    elif film.get("video_file_id"):
        await bot.send_video(chat_id, film["video_file_id"], caption=caption, reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, reply_markup=kb)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def rows_kb(rows: list[list[InlineKeyboardButton]]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def films_grid(films: list[dict]) -> list[list[InlineKeyboardButton]]:
    """Сетка кнопок фильмов: по 2 в ряд."""
    rows, row = [], []
    for f in films:
        label = f["title"] if len(f["title"]) <= 28 else f["title"][:27] + "…"
        row.append(btn(f"🎬 {label}", f"f:{f['id']}"))
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
        btn("◀️", f"{prefix}:{page - 1}") if page > 0 else btn(" ", "noop"),
        btn(f"{page + 1}/{pages}", "noop"),
        btn("▶️", f"{prefix}:{page + 1}") if page < pages - 1 else btn(" ", "noop"),
    ]


def page_bounds(total: int, page: int, size: int) -> tuple[int, int, int]:
    pages = max(1, math.ceil(total / size))
    page = min(max(page, 0), pages - 1)
    return page, pages, page * size


def main_reply_kb(role: str, webapp_url: str | None) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🎬 Каталог"), KeyboardButton(text="🔥 Популярное")],
        [KeyboardButton(text="🔍 Поиск"), KeyboardButton(text="ℹ️ Помощь")],
    ]
    if webapp_url:
        rows.append([KeyboardButton(text="📱 Mini App", web_app=WebAppInfo(url=webapp_url))])
    if at_least(role, EDITOR):
        rows.append([KeyboardButton(text="⚙️ Админ-панель")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


def admin_panel_kb(role: str) -> InlineKeyboardMarkup:
    rows = [
        [btn("➕ Добавить фильм", "a:add")],
        [btn("🎞 Фильмы", "a:films:0")],
    ]
    if at_least(role, ADMIN):
        rows.append([btn("👥 Команда", "a:team"), btn("📊 Статистика", "a:stats")])
    rows.append([btn("🏠 В главное меню", "m:home")])
    return rows_kb(rows)


WELCOME = (
    "🍿 <b>Добро пожаловать!</b>\n"
    "Здесь собраны фильмы из TikTok. Выбирайте в каталоге, "
    "ищите по названию или смотрите популярное."
)
HELP = (
    "ℹ️ <b>Помощь</b>\n"
    "• 🎬 Каталог — все фильмы сеткой, листается по страницам\n"
    "• 🔥 Популярное — самые просматриваемые\n"
    "• 🔍 Поиск — введите часть названия\n"
    "• 📱 Mini App — каталог в отдельном окне\n\n"
    "Фильм «Смотреть» откроет ссылку, фильм «в боте» проиграется здесь же."
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
    confirm = State()


class Edit(StatesGroup):
    title = State()
    link = State()


class Team(StatesGroup):
    wait_id = State()


# ============================================================================
# Middleware: роль текущего пользователя + регистрация в БД
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
    """Редактирует сообщение, если возможно; иначе отправляет новое."""
    try:
        await callback.message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=kb)


def build_router(cfg: Config, db: Database) -> Router:
    router = Router(name="main")

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
            await message.answer("Пока в каталоге пусто. Загляните позже!")
            return
        page, pages, offset = page_bounds(total, page, CATALOG_PAGE)
        films = db.list_films(active=True, limit=CATALOG_PAGE, offset=offset)
        rows = films_grid(films)
        nav = pager("cat", page, pages)
        if nav:
            rows.append(nav)
        if cfg.webapp_url:
            rows.append([InlineKeyboardButton(text="Открыть Mini App", web_app=WebAppInfo(url=cfg.webapp_url))])
        await message.answer(f"🎬 <b>Каталог</b> · всего {total}", reply_markup=rows_kb(rows))

    async def show_top(message: Message) -> None:
        films = db.list_films(active=True, top=True, limit=10)
        if not films:
            await message.answer("Пока нет фильмов.")
            return
        await message.answer("🔥 <b>Популярное</b>", reply_markup=rows_kb(films_grid(films)))

    async def show_admin(target: Message | CallbackQuery, role: str) -> None:
        # Вызывается и из reply-кнопки (Message), и из inline-кнопки (CallbackQuery)
        text = f"⚙️ <b>Админ-панель</b> · {ROLE_TITLES[role]}"
        if isinstance(target, CallbackQuery):
            await safe_show(target, text, admin_panel_kb(role))
        else:
            await target.answer(text, reply_markup=admin_panel_kb(role))

    # ------------------------------------------------------------------
    # Команды
    # ------------------------------------------------------------------
    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, bot: Bot, role: str, state: FSMContext) -> None:
        await state.clear()
        match = DEEP_LINK_RE.fullmatch(command.args or "")
        if match:
            film = db.get_film(int(match.group(1)))
            if film and film["is_active"]:
                db.add_view(film["id"])
                await send_film(bot, message.chat.id, film)
                return
            await message.answer("Этот фильм больше недоступен.")
        await show_home(message, role)

    @router.message(Command("menu"))
    async def on_menu(message: Message, role: str, state: FSMContext) -> None:
        await state.clear()
        await show_home(message, role)

    @router.message(Command("cancel"))
    async def on_cancel(message: Message, role: str, state: FSMContext) -> None:
        had = await state.get_state() is not None
        await state.clear()
        await message.answer("❌ Действие отменено." if had else "Нечего отменять.",
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
        await message.answer("🔍 Напишите часть названия фильма.\n/cancel — отмена")

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
        films = db.list_films(active=True, search=query, limit=20)
        if not films:
            await message.answer(f"По запросу «{esc(query)}» ничего не найдено.")
            return
        await message.answer(f"🔍 Найдено: {len(films)}", reply_markup=rows_kb(films_grid(films)))

    # ------------------------------------------------------------------
    # Мастер добавления фильма (редактор+)
    # ------------------------------------------------------------------
    async def start_add(target: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddFilm.media)
        await target.answer("🎞 <b>Новый фильм</b>\nШаг 1/4. Пришлите видео или фото-баннер.\n/cancel — отмена")

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
        await message.answer("Шаг 2/4. Напишите <b>название</b> фильма.")

    @router.message(AddFilm.media)
    async def on_media_bad(message: Message) -> None:
        await message.answer("Нужно видео или фото-баннер.")

    @router.message(AddFilm.title, F.text)
    async def on_title(message: Message, state: FSMContext) -> None:
        await state.update_data(title=message.text.strip()[:100])
        await state.set_state(AddFilm.kind)
        await message.answer(
            "Шаг 3/4. Как открывать фильм?\n"
            "🔗 <b>Ссылка</b> — кнопка «Смотреть» ведёт на ваш переходник.\n"
            "▶️ <b>В боте</b> — фильм проигрывается прямо в Telegram.",
            reply_markup=rows_kb([
                [btn("🔗 Ссылка", "wz:kind:link"), btn("▶️ В боте", "wz:kind:video")],
                [btn("❌ Отмена", "wz:cancel")],
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
            await callback.message.answer("Шаг 4/4. Пришлите ссылку-переходник (https://...).")
        elif data.get("video_file_id"):
            await show_confirm(callback.message, state)
        else:
            await state.set_state(AddFilm.video)
            await callback.message.answer("Шаг 4/4. Пришлите видеофайл фильма.")

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

    async def show_confirm(message: Message, state: FSMContext) -> None:
        await state.set_state(AddFilm.confirm)
        data = await state.get_data()
        await message.answer("👀 Превью карточки:")
        await send_film(message.bot, message.chat.id, data)
        await message.answer(
            "Сохранить фильм?",
            reply_markup=rows_kb([[btn("✅ Сохранить", "wz:save"), btn("❌ Отмена", "wz:cancel")]]),
        )

    @router.callback_query(AddFilm.confirm, F.data == "wz:save")
    async def on_save(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        data = await state.get_data()
        film_id = db.create_film(
            title=data["title"],
            kind=data["kind"],
            owner_id=callback.from_user.id,
            url=data.get("url"),
            video_file_id=data.get("video_file_id"),
            photo_file_id=data.get("photo_file_id"),
        )
        await state.clear()
        await callback.answer("Сохранено")
        await callback.message.answer(
            f"✅ Фильм #{film_id} добавлен в каталог.",
            reply_markup=rows_kb([[btn("🎞 Мои фильмы", "a:films:0"), btn("➕ Ещё", "a:add")]]),
        )

    @router.callback_query(F.data == "wz:cancel")
    async def on_cancel_btn(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await callback.message.answer("❌ Отменено.")

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
            [btn(f"{'🟢' if f['is_active'] else '⚪'} #{f['id']} {f['title'][:26]}", f"a:film:{f['id']}")]
            for f in films
        ]
        nav = pager("a:films", page, pages)
        if nav:
            rows.append(nav)
        rows.append([btn("⬅️ Назад", "a:panel")])
        text = f"🎞 <b>Фильмы</b> · {'все' if scope_owner is None else 'мои'}: {total}"
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
            f"ID: {film['id']} · {kind_label(film)} · 👁 {film['views']}\n"
            f"Статус: {status}\n"
            f"Автор: {film['owner_id']}"
        )
        rows = [
            [btn("👁 Открыть", f"a:open:{film['id']}"), btn("✏️ Название", f"a:rename:{film['id']}")],
        ]
        if film["kind"] == "link":
            rows.append([btn("🔗 Сменить ссылку", f"a:link:{film['id']}")])
        rows.append([
            btn("⏸ Скрыть" if film["is_active"] else "▶️ Показать", f"a:toggle:{film['id']}"),
            btn("🗑 Удалить", f"a:del:{film['id']}"),
        ])
        rows.append([btn("⬅️ К списку", "a:films:0")])
        await safe_show(callback, text, rows_kb(rows))

    @router.callback_query(F.data.regexp(r"^a:film:\d+$"))
    async def on_film_card(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        await callback.answer()
        await render_card(callback, film)

    @router.callback_query(F.data.regexp(r"^a:open:\d+$"))
    async def on_open(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        await callback.answer()
        await send_film(callback.bot, callback.message.chat.id, film)

    @router.callback_query(F.data.regexp(r"^a:toggle:\d+$"))
    async def on_toggle(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        db.update_film(film["id"], is_active=0 if film["is_active"] else 1)
        await callback.answer("Скрыт из каталога" if film["is_active"] else "Показан в каталоге")
        film = db.get_film(film["id"])
        await render_card(callback, film)

    @router.callback_query(F.data.regexp(r"^a:del:\d+$"))
    async def on_delete_ask(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        await callback.answer()
        await safe_show(
            callback,
            f"Удалить фильм #{film['id']} «{esc(film['title'])}»? Это необратимо.",
            rows_kb([[btn("✅ Да, удалить", f"a:delok:{film['id']}"), btn("↩️ Нет", f"a:film:{film['id']}")]]),
        )

    @router.callback_query(F.data.regexp(r"^a:delok:\d+$"))
    async def on_delete(callback: CallbackQuery, role: str) -> None:
        film = await load_managed(callback, role, int(callback.data.split(":")[-1]))
        if not film:
            return
        db.delete_film(film["id"])
        await callback.answer("Фильм удалён")
        await safe_show(callback, f"🗑 Фильм #{film['id']} удалён.", rows_kb([[btn("⬅️ К списку", "a:films:0")]]))

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
        data = await state.get_data()
        film = db.get_film(data["film_id"])
        await state.clear()
        if not film or not can_manage_film(role, message.from_user.id, film):
            return await deny(message)
        db.update_film(film["id"], title=message.text.strip()[:100])
        await message.answer("✅ Название обновлено.", reply_markup=rows_kb(
            [[btn("⬅️ К фильму", f"a:film:{film['id']}")]]))

    @router.message(Edit.link, F.text)
    async def on_relink(message: Message, role: str, state: FSMContext) -> None:
        url = message.text.strip()
        if not URL_RE.match(url):
            await message.answer("Нужна ссылка, начинающаяся с https://")
            return
        data = await state.get_data()
        film = db.get_film(data["film_id"])
        await state.clear()
        if not film or not can_manage_film(role, message.from_user.id, film):
            return await deny(message)
        db.update_film(film["id"], url=url)
        await message.answer("✅ Ссылка обновлена.", reply_markup=rows_kb(
            [[btn("⬅️ К фильму", f"a:film:{film['id']}")]]))

    # ------------------------------------------------------------------
    # Команда: админы и редакторы (админ+)
    # ------------------------------------------------------------------
    async def team_view(target: Message | CallbackQuery, actor_role: str) -> None:
        lines = [f"👥 <b>Команда</b>", f"{ROLE_TITLES[OWNER]}: <code>{cfg.owner_id}</code>"]
        rows: list[list[InlineKeyboardButton]] = []
        for m in db.staff():
            lines.append(f"{ROLE_TITLES[m['role']]}: {user_label(m, m['tg_id'])} <code>{m['tg_id']}</code>")
            if can_change_role(actor_role, m["role"], USER):
                rows.append([btn(f"❌ Снять: {m['tg_id']}", f"a:rm:{m['tg_id']}")])
        if not db.staff():
            lines.append("\nПока нет админов и редакторов.")
        add_row = [btn("➕ Редактор", "a:addm:editor")]
        if actor_role == OWNER:
            add_row.append(btn("➕ Админ", "a:addm:admin"))
        rows.append(add_row)
        rows.append([btn("⬅️ Назад", "a:panel")])
        text = "\n".join(lines) + "\n\nЧтобы выдать роль, пришлите Telegram ID или перешлите сообщение пользователя."
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
            f"Пришлите Telegram ID пользователя для роли «{ROLE_TITLES[new_role]}» "
            "или перешлите его сообщение.\n/cancel — отмена"
        )

    @router.message(Team.wait_id)
    async def on_add_member(message: Message, role: str, bot: Bot, state: FSMContext) -> None:
        data = await state.get_data()
        new_role = data.get("new_role", EDITOR)
        await state.clear()

        origin = message.forward_origin
        if isinstance(origin, MessageOriginUser):
            target_id = origin.sender_user.id
        elif message.text and message.text.strip().isdigit():
            target_id = int(message.text.strip())
        else:
            await message.answer("Нужен числовой Telegram ID или пересланное сообщение. Нажмите роль заново.")
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
                f"Вам выдана роль «{ROLE_TITLES[new_role]}». Откройте меню: /start",
                reply_markup=main_reply_kb(new_role, cfg.webapp_url),
            )
        except TelegramAPIError:
            await message.answer("Пользователь ещё не запускал бота, уведомление не доставлено.")
        await team_view(message, role)

    @router.callback_query(F.data.regexp(r"^a:rm:\d+$"))
    async def on_remove_member(callback: CallbackQuery, role: str, bot: Bot) -> None:
        target_id = int(callback.data.split(":")[-1])
        current = db.get_role(target_id)
        if not can_change_role(role, current, USER):
            return await deny(callback)
        db.set_role(target_id, USER, added_by=callback.from_user.id)
        await callback.answer("Роль снята")
        try:
            await bot.send_message(target_id, "Ваша роль в боте снята.")
        except TelegramAPIError:
            pass
        await team_view(callback, role)

    # ------------------------------------------------------------------
    # Статистика и панель
    # ------------------------------------------------------------------
    @router.callback_query(F.data == "a:stats")
    async def on_stats(callback: CallbackQuery, role: str) -> None:
        if not at_least(role, ADMIN):
            return await deny(callback)
        s = db.stats()
        text = (
            "📊 <b>Статистика</b>\n"
            f"Фильмов: {s['films']} (показано: {s['active']})\n"
            f"Просмотров: {s['views']}\n"
            f"Пользователей: {s['users']}\n"
            f"Сотрудников: {s['staff']}"
        )
        await safe_show(callback, text, rows_kb([[btn("⬅️ Назад", "a:panel")]]))
        await callback.answer()

    @router.callback_query(F.data == "a:panel")
    async def on_panel(callback: CallbackQuery, role: str, state: FSMContext) -> None:
        if not at_least(role, EDITOR):
            return await deny(callback)
        await state.clear()
        await show_admin(callback, role)
        await callback.answer()

    # ------------------------------------------------------------------
    # Каталог и карточки (для всех)
    # ------------------------------------------------------------------
    @router.callback_query(F.data.regexp(r"^cat:\d+$"))
    async def on_catalog_page(callback: CallbackQuery) -> None:
        total = db.count_films(active=True)
        page, pages, offset = page_bounds(total, int(callback.data.split(":")[1]), CATALOG_PAGE)
        films = db.list_films(active=True, limit=CATALOG_PAGE, offset=offset)
        rows = films_grid(films)
        nav = pager("cat", page, pages)
        if nav:
            rows.append(nav)
        await safe_show(callback, f"🎬 <b>Каталог</b> · всего {total}", rows_kb(rows))
        await callback.answer()

    @router.callback_query(F.data.regexp(r"^f:\d+$"))
    async def on_film_button(callback: CallbackQuery, bot: Bot) -> None:
        film = db.get_film(int(callback.data.split(":")[1]))
        if not film or not film["is_active"]:
            await callback.answer("Фильм недоступен", show_alert=True)
            return
        db.add_view(film["id"])
        await callback.answer()
        await send_film(bot, callback.message.chat.id, film)

    @router.callback_query(F.data == "m:home")
    async def on_home_btn(callback: CallbackQuery, role: str) -> None:
        await callback.answer()
        await show_home(callback.message, role)

    @router.callback_query(F.data == "noop")
    async def on_noop(callback: CallbackQuery) -> None:
        await callback.answer()

    # ------------------------------------------------------------------
    # Неизвестный текст: подсказка, без молчания
    # ------------------------------------------------------------------
    @router.message(F.text)
    async def on_fallback(message: Message, role: str, state: FSMContext) -> None:
        if await state.get_state() is None:
            await message.answer("Не понял. Используйте кнопки меню ниже.",
                                 reply_markup=main_reply_kb(role, cfg.webapp_url))

    return router


# ============================================================================
# HTTP: вебхук, Mini App, API
# ============================================================================
def build_web(cfg: Config, db: Database, bot: Bot, dp: Dispatcher, bot_username: str | None) -> web.Application:
    app = web.Application()

    async def health(_: web.Request) -> web.Response:
        return web.Response(text="ok")

    async def mini_app(_: web.Request) -> web.FileResponse:
        return web.FileResponse(INDEX_HTML, headers={"Cache-Control": "no-cache"})

    async def api_config(_: web.Request) -> web.Response:
        return web.json_response({"botUsername": bot_username})

    async def api_films(_: web.Request) -> web.Response:
        films = [
            {
                "id": f["id"],
                "title": f["title"],
                "kind": f["kind"],
                "url": f["url"] if f["kind"] == "link" else None,
                "hasBanner": bool(f["photo_file_id"]),
                "views": f["views"],
            }
            for f in db.list_films(active=True, limit=500)
        ]
        return web.json_response(films, headers={"Cache-Control": "no-store"})

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

    app.router.add_get("/health", health)
    app.router.add_get("/", mini_app)
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/films", api_films)
    app.router.add_get("/api/banner/{film_id}", api_banner)

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

    # Стартовые админы из ADMIN_IDS (не перезаписывают уже назначенные роли)
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

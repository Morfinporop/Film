#!/usr/bin/env python3
"""
Telegram-бот каталога фильмов + Mini App. Весь проект запускается одним файлом:

    python main.py

Что внутри:
  * бот на aiogram 3 (вебхук на PORT, по умолчанию 3000; либо polling для локальной отладки);
  * админ (ADMIN_IDS) добавляет фильмы мастером /add: видео или баннер, название,
    тип показа — «ссылка» (кнопка ведёт на переходник) или «видео в боте»;
  * пользователи видят каталог сеткой кнопок (/start);
  * Mini App (index.html) открывается кнопкой меню, данные берёт из /api/films;
  * SQLite (стандартный модуль sqlite3), файл по умолчанию data/film.db (/app/data/film.db на bothost);
  * вся конфигурация — из переменных окружения.
"""

from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import signal
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    MenuButtonWebApp,
    Message,
    WebAppInfo,
)
from aiohttp import web
from aiogram.webhook.aiohttp_server import SimpleRequestHandler

BASE_DIR = Path(__file__).resolve().parent
INDEX_HTML = BASE_DIR / "index.html"
URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
DEEP_LINK_RE = re.compile(r"^film_(\d+)$")

log = logging.getLogger("film-bot")


# ----------------------------------------------------------------------------
# Конфигурация (только переменные окружения)
# ----------------------------------------------------------------------------
@dataclass(frozen=True)
class Config:
    bot_token: str
    admin_ids: frozenset[int]
    mode: str  # webhook | polling
    public_url: str | None
    port: int
    webhook_path: str
    webhook_secret: str
    db_path: str

    @property
    def webapp_url(self) -> str | None:
        return f"{self.public_url}/app" if self.public_url else None


def load_config(env: os.environ | dict = os.environ) -> Config:
    token = env.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN не задан: добавьте его в переменные окружения")

    admin_raw = env.get("ADMIN_IDS", "").strip()
    admin_ids: set[int] = set()
    for part in re.split(r"[\s,;]+", admin_raw):
        if not part:
            continue
        if not part.isdigit():
            raise SystemExit(f"ADMIN_IDS: некорректный Telegram ID «{part}»")
        admin_ids.add(int(part))
    if not admin_ids:
        raise SystemExit("ADMIN_IDS не задан: укажите Telegram ID администратора")

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

    import hashlib

    secret = (env.get("WEBHOOK_SECRET") or "").strip() or hashlib.sha256(
        f"webhook:{token}".encode()
    ).hexdigest()
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
        raise SystemExit("WEBHOOK_SECRET: допустимы только латиница, цифры, _ и -")

    db_path = (env.get("DB_PATH") or "").strip() or str(BASE_DIR / "data" / "film.db")

    return Config(
        bot_token=token,
        admin_ids=frozenset(admin_ids),
        mode=mode,
        public_url=public_url,
        port=port,
        webhook_path=webhook_path,
        webhook_secret=secret,
        db_path=db_path,
    )


# ----------------------------------------------------------------------------
# База данных (SQLite)
# ----------------------------------------------------------------------------
SCHEMA = """
CREATE TABLE IF NOT EXISTS films (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    title          TEXT    NOT NULL,
    kind           TEXT    NOT NULL CHECK (kind IN ('link', 'video')),
    url            TEXT,
    video_file_id  TEXT,
    photo_file_id  TEXT,
    is_active      INTEGER NOT NULL DEFAULT 1,
    views          INTEGER NOT NULL DEFAULT 0,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    CHECK (kind <> 'link'  OR url IS NOT NULL),
    CHECK (kind <> 'video' OR video_file_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_films_active ON films (is_active, id);
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
        self.conn.commit()

    def _rows(self, sql: str, params: tuple = ()) -> list[dict]:
        return [dict(r) for r in self.conn.execute(sql, params).fetchall()]

    def create_film(
        self,
        *,
        title: str,
        kind: str,
        url: str | None = None,
        video_file_id: str | None = None,
        photo_file_id: str | None = None,
    ) -> int:
        cur = self.conn.execute(
            "INSERT INTO films (title, kind, url, video_file_id, photo_file_id) VALUES (?, ?, ?, ?, ?)",
            (title, kind, url, video_file_id, photo_file_id),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def get_film(self, film_id: int) -> dict | None:
        row = self.conn.execute("SELECT * FROM films WHERE id = ?", (film_id,)).fetchone()
        return dict(row) if row else None

    def list_active(self) -> list[dict]:
        return self._rows("SELECT * FROM films WHERE is_active = 1 ORDER BY id DESC LIMIT 200")

    def list_all(self) -> list[dict]:
        return self._rows("SELECT * FROM films ORDER BY id DESC LIMIT 200")

    def set_active(self, film_id: int, active: bool) -> bool:
        cur = self.conn.execute(
            "UPDATE films SET is_active = ? WHERE id = ?", (1 if active else 0, film_id)
        )
        self.conn.commit()
        return cur.rowcount > 0

    def delete(self, film_id: int) -> bool:
        cur = self.conn.execute("DELETE FROM films WHERE id = ?", (film_id,))
        self.conn.commit()
        return cur.rowcount > 0

    def add_view(self, film_id: int) -> None:
        self.conn.execute("UPDATE films SET views = views + 1 WHERE id = ?", (film_id,))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()


# ----------------------------------------------------------------------------
# Карточки и клавиатуры
# ----------------------------------------------------------------------------
def esc(text: str) -> str:
    return html.escape(str(text), quote=False)


async def send_film(bot: Bot, chat_id: int, film: dict) -> None:
    """Видео-фильм — видео в боте. Ссылочный — баннер/видео + кнопка-переход."""
    caption = f"🎬 <b>{esc(film['title'])}</b>"

    if film["kind"] == "video":
        await bot.send_video(chat_id, film["video_file_id"], caption=caption, supports_streaming=True)
        return

    kb = InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="▶️ Смотреть", url=film["url"])]]
    )
    if film.get("photo_file_id"):
        await bot.send_photo(chat_id, film["photo_file_id"], caption=caption, reply_markup=kb)
    elif film.get("video_file_id"):
        await bot.send_video(chat_id, film["video_file_id"], caption=caption, reply_markup=kb)
    else:
        await bot.send_message(chat_id, caption, reply_markup=kb)


def catalog_markup(films: list[dict], webapp_url: str | None) -> InlineKeyboardMarkup:
    """Сетка кнопок каталога: по 2 в ряд, без лишнего текста на кнопках."""
    rows: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []
    for film in films:
        title = film["title"]
        label = title if len(title) <= 28 else title[:27] + "…"
        row.append(InlineKeyboardButton(text=f"🎬 {label}", callback_data=f"f:{film['id']}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if webapp_url:
        rows.append([InlineKeyboardButton(text="📱 Открыть каталог", web_app=WebAppInfo(url=webapp_url))])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ----------------------------------------------------------------------------
# Пользовательские хендлеры
# ----------------------------------------------------------------------------
def build_user_router(cfg: Config, db: Database) -> Router:
    router = Router(name="user")

    async def show_catalog(message: Message) -> None:
        films = db.list_active()
        if not films:
            await message.answer("Пока в каталоге пусто. Загляните позже!")
            return
        await message.answer(
            "🍿 <b>Каталог фильмов</b>\nВыберите фильм кнопкой ниже.",
            reply_markup=catalog_markup(films, cfg.webapp_url),
        )

    @router.message(CommandStart())
    async def on_start(message: Message, command: CommandObject, bot: Bot) -> None:
        match = DEEP_LINK_RE.fullmatch(command.args or "")
        if match:
            film = db.get_film(int(match.group(1)))
            if film and film["is_active"]:
                db.add_view(film["id"])
                await send_film(bot, message.chat.id, film)
                return
            await message.answer("Этот фильм больше недоступен.")
        await show_catalog(message)

    @router.message(Command("catalog", "menu"))
    async def on_catalog(message: Message) -> None:
        await show_catalog(message)

    @router.callback_query(F.data.startswith("f:"))
    async def on_film_button(callback: CallbackQuery, bot: Bot) -> None:
        try:
            film = db.get_film(int(callback.data.split(":", 1)[1]))
        except ValueError:
            film = None
        if not film or not film["is_active"]:
            await callback.answer("Фильм недоступен", show_alert=True)
            return
        db.add_view(film["id"])
        await callback.answer()
        await send_film(bot, callback.message.chat.id, film)

    return router


# ----------------------------------------------------------------------------
# Админ: мастер добавления и управление
# ----------------------------------------------------------------------------
class AddFilm(StatesGroup):
    media = State()
    title = State()
    kind = State()
    url = State()
    video = State()
    confirm = State()


ADMIN_HELP = (
    "⚙️ <b>Админ-панель</b>\n"
    "/add — добавить фильм\n"
    "/list — список и управление\n"
    "/cancel — отменить мастер"
)


def build_admin_router(cfg: Config, db: Database) -> Router:
    router = Router(name="admin")
    # Всё, что приходит не от админа, в этом роутере просто не обрабатывается.
    router.message.filter(F.from_user.id.in_(cfg.admin_ids))
    router.callback_query.filter(F.from_user.id.in_(cfg.admin_ids))

    @router.message(Command("admin"))
    async def on_admin(message: Message) -> None:
        await message.answer(ADMIN_HELP)

    @router.message(Command("cancel"))
    async def on_cancel(message: Message, state: FSMContext) -> None:
        had = await state.get_state() is not None
        await state.clear()
        await message.answer("❌ Добавление отменено." if had else "Нечего отменять.")

    @router.callback_query(F.data == "wz:cancel")
    async def on_cancel_button(callback: CallbackQuery, state: FSMContext) -> None:
        await state.clear()
        await callback.answer()
        await callback.message.answer("❌ Добавление отменено.")

    # ---- /add ----
    @router.message(Command("add"))
    async def on_add(message: Message, state: FSMContext) -> None:
        await state.clear()
        await state.set_state(AddFilm.media)
        await message.answer(
            "🎞 Шаг 1/4. Пришлите <b>видео</b> или <b>баннер</b> (фото) фильма.\n/cancel — отмена"
        )

    @router.message(AddFilm.media, F.video | F.photo)
    async def on_media(message: Message, state: FSMContext) -> None:
        if message.video:
            await state.update_data(video_file_id=message.video.file_id)
        else:
            await state.update_data(photo_file_id=message.photo[-1].file_id)  # самое большое фото
        await state.set_state(AddFilm.title)
        await message.answer("🎞 Шаг 2/4. Напишите <b>название</b> фильма.")

    @router.message(AddFilm.media)
    async def on_media_invalid(message: Message) -> None:
        await message.answer("Нужно видео или фото-баннер. Пришлите файл.")

    @router.message(AddFilm.title, F.text)
    async def on_title(message: Message, state: FSMContext) -> None:
        if message.text.startswith("/"):
            await message.answer("Напишите название текстом.")
            return
        await state.update_data(title=message.text.strip()[:100])
        await state.set_state(AddFilm.kind)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="🔗 Ссылка (переход)", callback_data="wz:kind:link")],
                [InlineKeyboardButton(text="▶️ Видео в боте", callback_data="wz:kind:video")],
            ]
        )
        await message.answer(
            "🎞 Шаг 3/4. Как открывать фильм?\n"
            "🔗 <b>Ссылка</b> — кнопка ведёт на ваш переходник.\n"
            "▶️ <b>Видео в боте</b> — фильм проигрывается прямо в Telegram.",
            reply_markup=kb,
        )

    @router.message(AddFilm.title)
    async def on_title_invalid(message: Message) -> None:
        await message.answer("Напишите название текстом.")

    @router.callback_query(AddFilm.kind, F.data.in_({"wz:kind:link", "wz:kind:video"}))
    async def on_kind(callback: CallbackQuery, state: FSMContext) -> None:
        await callback.answer()
        kind = callback.data.split(":")[-1]
        await state.update_data(kind=kind)
        data = await state.get_data()

        if kind == "link":
            await state.set_state(AddFilm.url)
            await callback.message.answer("🎞 Шаг 4/4. Пришлите ссылку-переходник (https://...).")
        elif data.get("video_file_id"):
            await show_confirm(callback.message, state)
        else:
            await state.set_state(AddFilm.video)
            await callback.message.answer("🎞 Шаг 4/4. Пришлите видеофайл фильма.")

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
    async def on_video_invalid(message: Message) -> None:
        await message.answer("Пришлите именно видеофайл.")

    async def show_confirm(message: Message, state: FSMContext) -> None:
        await state.set_state(AddFilm.confirm)
        data = await state.get_data()
        await message.answer("👀 Превью карточки:")
        await send_film(message.bot, message.chat.id, data)
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="✅ Сохранить", callback_data="wz:save"),
                    InlineKeyboardButton(text="❌ Отмена", callback_data="wz:cancel"),
                ]
            ]
        )
        await message.answer("Сохранить фильм?", reply_markup=kb)

    @router.callback_query(AddFilm.confirm, F.data == "wz:save")
    async def on_save(callback: CallbackQuery, state: FSMContext) -> None:
        data = await state.get_data()
        film_id = db.create_film(
            title=data["title"],
            kind=data["kind"],
            url=data.get("url"),
            video_file_id=data.get("video_file_id"),
            photo_file_id=data.get("photo_file_id"),
        )
        await state.clear()
        await callback.answer()
        await callback.message.answer(f"✅ Фильм #{film_id} сохранён и доступен в каталоге (/start).")

    # ---- управление ----
    @router.message(Command("list"))
    async def on_list(message: Message) -> None:
        films = db.list_all()
        if not films:
            await message.answer("Фильмов пока нет. Используйте /add.")
            return
        lines = []
        rows: list[list[InlineKeyboardButton]] = []
        for f in films[:20]:
            mark = "🟢" if f["is_active"] else "⚪️"
            kind = "▶️видео" if f["kind"] == "video" else "🔗ссылка"
            lines.append(f"{mark} <b>#{f['id']}</b> {esc(f['title'])} — {kind}, 👁 {f['views']}")
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{'⏸' if f['is_active'] else '▶️'} #{f['id']}",
                        callback_data=f"adm:tog:{f['id']}",
                    ),
                    InlineKeyboardButton(text=f"🗑 #{f['id']}", callback_data=f"adm:del:{f['id']}"),
                ]
            )
        await message.answer(
            "\n".join(lines)[:4000],
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    @router.callback_query(F.data.regexp(r"^adm:(tog|del):\d+$"))
    async def on_manage(callback: CallbackQuery) -> None:
        _, action, raw_id = callback.data.split(":")
        film_id = int(raw_id)
        if action == "del":
            ok = db.delete(film_id)
            await callback.answer(f"Фильм #{film_id} удалён" if ok else "Не найдено")
            return
        film = db.get_film(film_id)
        if not film:
            await callback.answer("Не найдено", show_alert=True)
            return
        db.set_active(film_id, not film["is_active"])
        await callback.answer("Скрыт из каталога" if film["is_active"] else "Показан в каталоге")

    return router


# ----------------------------------------------------------------------------
# HTTP: вебхук, Mini App, API для Mini App
# ----------------------------------------------------------------------------
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
                # ссылку отдаём только для ссылочных фильмов; видео — только через бота
                "url": f["url"] if f["kind"] == "link" else None,
                "hasBanner": bool(f["photo_file_id"]),
            }
            for f in db.list_active()
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
        return web.Response(
            body=buf.getvalue(),
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=3600"},
        )

    app.router.add_get("/health", health)
    app.router.add_get("/", mini_app)
    app.router.add_get("/app", mini_app)
    app.router.add_get("/api/config", api_config)
    app.router.add_get("/api/films", api_films)
    app.router.add_get("/api/banner/{film_id}", api_banner)

    # Входящие обновления Telegram; секрет проверяется aiogram автоматически.
    SimpleRequestHandler(
        dispatcher=dp, bot=bot, secret_token=cfg.webhook_secret
    ).register(app, path=cfg.webhook_path)

    return app


# ----------------------------------------------------------------------------
# Запуск
# ----------------------------------------------------------------------------
async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    cfg = load_config()
    db = Database(cfg.db_path)

    bot = Bot(cfg.bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp.include_router(build_admin_router(cfg, db))  # админ — первым, чтобы мастер перехватывал сообщения
    dp.include_router(build_user_router(cfg, db))

    me = await bot.get_me()
    log.info("бот @%s, режим: %s, БД: %s", me.username, cfg.mode, cfg.db_path)

    await bot.set_my_commands(
        [
            BotCommand(command="start", description="Каталог фильмов"),
            BotCommand(command="catalog", description="Открыть каталог"),
        ]
    )
    if cfg.webapp_url:
        # Кнопка меню слева от поля ввода открывает Mini App.
        await bot.set_chat_menu_button(
            menu_button=MenuButtonWebApp(text="🎬 Фильмы", web_app=WebAppInfo(url=cfg.webapp_url))
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

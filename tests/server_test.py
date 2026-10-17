"""Серверные проверки: подсказки, избранное, тикеты (права, base64-картинки, ?init=),
бан, миграция старой базы. Запуск: python tests/server_test.py (нужны зависимости из requirements)."""
import asyncio, base64, hashlib, hmac, json, os, sqlite3, struct, sys, tempfile, time, zlib
from urllib.parse import urlencode

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("BOT_TOKEN", "123456:AA-test")
TOKEN = os.environ["BOT_TOKEN"]

import main
from main import Database, Config, Transcoder, build_web
from aiogram import Bot, Dispatcher
from aiohttp import web
import aiohttp

OWNER, OPR, ADM, USR, OTH = 1, 2, 3, 5, 6
FAILS = []


def check(name, cond, extra=None):
    print(("OK   " if cond else "FAIL ") + name + ("" if cond or extra is None else "  " + repr(extra)))
    if not cond:
        FAILS.append(name)


def png_bytes():
    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    ihdr = struct.pack(">IIBBBBB", 2, 2, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\xff\x00\x00" * 2 for _ in range(2))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def init_data(uid):
    pairs = {"auth_date": str(int(time.time())),
             "user": json.dumps({"id": uid, "first_name": "Тест", "username": "t"})}
    check_s = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    secret = hmac.new(b"WebAppData", TOKEN.encode(), hashlib.sha256).digest()
    pairs["hash"] = hmac.new(secret, check_s.encode(), hashlib.sha256).hexdigest()
    return urlencode(pairs)


async def run():
    tmp = tempfile.mkdtemp()
    cfg = Config(bot_token=TOKEN, owner_id=OWNER, initial_admins=frozenset({ADM}),
                 mode="polling", public_url=None, port=3000, webhook_path="/webhook",
                 webhook_secret="s", db_path=os.path.join(tmp, "film.db"),
                 background_url="", vip_month=199, vip_year=900)
    db = Database(cfg.db_path)
    for uid, role in ((OWNER, "owner"), (OPR, "operator"), (ADM, "admin"),
                      (USR, "user"), (OTH, "user")):
        db.conn.execute("INSERT OR IGNORE INTO users (tg_id, role) VALUES (?, ?)", (uid, role))
    fid = db.create_film(title="Тест", kind="video", status="ready", owner_id=OWNER,
                         duration=300, width=1280, height=720)
    bot = Bot(TOKEN)
    tc = Transcoder(bot, db, cfg.media_dir)
    app = build_web(cfg, db, bot, Dispatcher(), tc, "testbot")
    runner = web.AppRunner(app)
    await runner.setup()
    sock = None
    import socket as _s
    sock = _s.socket(); sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]; sock.close()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    base = f"http://127.0.0.1:{port}"
    H = {u: {"X-Init-Data": init_data(u)} for u in (OWNER, OPR, ADM, USR, OTH)}

    async def req(uid, method, path, body=None):
        async with aiohttp.ClientSession() as s:
            async with s.request(method, base + path, headers=H[uid], json=body) as r:
                try:
                    return r.status, await r.json()
                except Exception:
                    return r.status, {}

    async def get_raw(uid, path, headers=None):
        async with aiohttp.ClientSession() as s:
            async with s.get(base + path, headers=headers if headers is not None else H[uid]) as r:
                return r.status, await r.read()

    # ---------- подсказки ----------
    _, me0 = await req(USR, "GET", "/api/me")
    check("до первого входа подсказки не показаны", me0["tourSeen"] is False, me0.get("tourSeen"))
    st, _ = await req(USR, "POST", "/api/tour", {})
    _, me1 = await req(USR, "GET", "/api/me")
    check("после отметки подсказки больше не показываются", st == 200 and me1["tourSeen"] is True)
    check("отметка лежит в базе", bool(db.get_user(USR)["tour_seen"]))

    # ---------- избранное ----------
    st, r = await req(USR, "POST", f"/api/films/{fid}/favorite", {})
    _, det = await req(USR, "GET", f"/api/films/{fid}")
    check("страница фильма знает про избранное", st == 200 and det["favorite"] is True, det["favorite"])
    _, det2 = await req(OTH, "GET", f"/api/films/{fid}")
    check("у другого пользователя сердечко пустое", det2["favorite"] is False)
    _, favs = await req(USR, "GET", "/api/favorites")
    check("избранное в списке пользователя", [f["id"] for f in favs] == [fid], favs)
    await req(USR, "POST", f"/api/films/{fid}/favorite", {})

    # ---------- тикеты: права ----------
    st, t1 = await req(USR, "POST", "/api/tickets", {"kind": "bug", "text": "не работает"})
    check("тикеты создаются", st == 201 and t1["id"], t1)
    _, lst = await req(USR, "GET", "/api/tickets")
    check("в списке тикетов нет переписки", "messages" not in lst["tickets"][0], list(lst["tickets"][0]))
    _, one = await req(USR, "GET", f"/api/tickets/{t1['id']}")
    check("автору нельзя удалять, можно закрыть", one["canDelete"] is False and one["canClose"] is True)
    check("у автора нет передачи админам", one["canTransfer"] is False)
    check("в одном тикете есть переписка", one["messages"] == [])
    st, _ = await req(USR, "POST", f"/api/tickets/{t1['id']}/delete", {})
    check("автор не удалил свой тикет", st == 403)
    _, one_o = await req(OPR, "GET", f"/api/tickets/{t1['id']}")
    check("у оператора есть передача и удаление", one_o["canTransfer"] is True and one_o["canDelete"] is True)
    st, _ = await req(ADM, "POST", f"/api/tickets/{t1['id']}/transfer", {"to": "staff"})
    check("админ не передаёт админам", st == 403)
    st, tr = await req(OPR, "POST", f"/api/tickets/{t1['id']}/transfer", {"to": "staff"})
    check("оператор передал админам", st == 200 and tr["assignee"] == "staff")
    st, _ = await req(ADM, "POST", f"/api/tickets/{t1['id']}/transfer", {"to": "operator"})
    check("админ вернул операторам", st == 200)
    st, _ = await req(OPR, "POST", f"/api/tickets/{t1['id']}/delete", {})
    check("оператор удалил тикет сразу", st == 200 and db.get_ticket(t1["id"]) is None)

    # ---------- закрытие и автоудаление ----------
    _, t2 = await req(USR, "POST", "/api/tickets", {"kind": "idea", "text": "закрыть потом"})
    st, cl = await req(USR, "POST", f"/api/tickets/{t2['id']}/close", {})
    check("автор закрыл тикет", st == 200 and cl["ttl"] == 600)
    st, _ = await req(USR, "POST", f"/api/tickets/{t2['id']}/messages", {"text": "после закрытия"})
    check("в закрытый тикет не написать", st == 400)
    st, _ = await req(OPR, "POST", f"/api/tickets/{t2['id']}/delete", {})
    check("персонал удалил закрытый тикет сразу", st == 200 and db.get_ticket(t2["id"]) is None)

    # ---------- картинка base64 и ?init= ----------
    _, t3 = await req(USR, "POST", "/api/tickets", {"kind": "bug", "text": "скрин"})
    b64 = "data:image/png;base64," + base64.b64encode(png_bytes()).decode()
    st, up = await req(USR, "POST", f"/api/tickets/{t3['id']}/images", {"image": b64})
    check("base64-картинка загрузилась", st == 200 and up["name"].endswith(".png"), up)
    st, r = await req(USR, "POST", f"/api/tickets/{t3['id']}/messages", {"text": "смотрите", "image": up["name"]})
    check("сообщение с картинкой", st == 200 and r["message"]["image"])
    img = r["message"]["image"]
    st, body = await get_raw(USR, img)
    check("с заголовком картинка отдаётся", st == 200 and body == png_bytes())
    st, _ = await get_raw(USR, img, headers={})
    check("без подписи картинка не отдаётся", st == 401)
    async with aiohttp.ClientSession() as s:
        from urllib.parse import quote
        async with s.get(base + img + "?init=" + quote(init_data(USR), safe=""), headers={}) as g:
            gb = await g.read()
            check("из <img> по ?init= картинка открывается", g.status == 200 and gb == png_bytes(), g.status)
        async with s.get(base + img + "?init=" + quote(init_data(OTH), safe=""), headers={}) as g2:
            check("чужой ?init= не открывает картинку", g2.status == 403, g2.status)
    st, r = await req(USR, "POST", f"/api/tickets/{t3['id']}/images",
                      {"image": "data:image/png;base64," + base64.b64encode(b"#!/bin/sh").decode()})
    check("под видом картинки файл не проходит", st == 400)
    st, r = await req(USR, "POST", f"/api/tickets/{t3['id']}/messages", {"text": "x", "image": "../etc/passwd"})
    check("чужой файл приложить нельзя", st == 400)
    folder = cfg.media_dir.parent / "tickets" / str(t3["id"])
    st, _ = await req(ADM, "POST", f"/api/tickets/{t3['id']}/delete", {})
    check("при удалении тикета картинки стёрты", st == 200 and not folder.exists())

    # ---------- загрузка частями: обрывы не ломают сервер ----------
    st, up = await req(ADM, "POST", "/api/admin/uploads", {"size": 2 * 2 * 1024 * 1024 + 123, "name": "v.mp4"})
    CS = up.get("chunkSize", 2 * 1024 * 1024)
    check("инициализация загрузки: части по 2 МБ", st == 200 and CS == 2 * 1024 * 1024 and up["total"] == 3, up)

    async def put_chunk(idx, data):
        async with aiohttp.ClientSession() as s:
            async with s.put(f"{base}/api/admin/uploads/{up['id']}/{idx}", headers=H[ADM], data=data) as r:
                try:
                    return r.status, await r.json()
                except Exception:
                    return r.status, {}

    st, r = await put_chunk(0, b"\x00" * CS)
    check("первая часть принята", st == 200 and r["received"] == 1, r)
    st, r = await put_chunk(1, b"\x00" * 1000)
    check("оборванная часть не засчитана и не ломает сервер", st == 400, (st, r))

    # резкий обрыв (RST) посреди тела: сервер ловит обрыв, часть не засчитана, сервер жив
    import socket as _sock
    raw = _sock.socket(); raw.connect(("127.0.0.1", port))
    raw.setsockopt(_sock.SOL_SOCKET, _sock.SO_LINGER, struct.pack("ii", 1, 0))
    head = (f"PUT /api/admin/uploads/{up['id']}/1 HTTP/1.1\r\nHost: x\r\n"
            f"X-Init-Data: {init_data(ADM)}\r\nContent-Length: {CS}\r\n\r\n")
    raw.sendall(head.encode() + b"\x00" * 200000)
    raw.close()
    await asyncio.sleep(0.5)
    st, r = await put_chunk(1, b"\x00" * CS)
    check("после обрыва часть дошла повтором", st == 200 and r["received"] == 2, (st, r))
    st, r = await put_chunk(2, b"\x00" * 123)
    check("все части на месте", st == 200 and r["received"] == 3 and r["total"] == 3, r)
    st, _ = await req(ADM, "GET", "/health")
    check("сервер пережил обрыв соединения", st == 200)

    # ---------- бан ----------
    st, _ = await req(OWNER, "POST", "/api/admin/ban", {"id": USR, "banned": True})
    st2, _ = await req(USR, "GET", "/api/films")
    st3, _ = await req(USR, "POST", "/api/tickets", {"kind": "bug", "text": "апелляция"})
    check("забаненному закрыт каталог, но открыта поддержка", st == 200 and st2 == 403 and st3 == 201,
          (st, st2, st3))
    await req(OWNER, "POST", "/api/admin/ban", {"id": USR, "banned": False})

    # ---------- запуск: фильмы с пропавшим исходником ----------
    f_lost = db.create_film(title="Пропал", kind="video", status="processing", owner_id=OWNER,
                            duration=10, width=64, height=64)
    f_src = db.create_film(title="С исходником", kind="video", status="processing", owner_id=OWNER,
                           duration=10, width=64, height=64)
    (cfg.media_dir / str(f_src)).mkdir(parents=True, exist_ok=True)
    (cfg.media_dir / str(f_src) / "source.mp4").write_bytes(b"x")
    f_done = db.create_film(title="Готов", kind="video", status="ready", owner_id=OWNER,
                            duration=10, width=64, height=64)
    (cfg.media_dir / str(f_done)).mkdir(parents=True, exist_ok=True)
    (cfg.media_dir / str(f_done) / "master.m3u8").write_text("#EXTM3U\n")
    marked = main.mark_lost_sources(db, cfg.media_dir)
    check("фильм с пропавшим исходником помечен failed",
          f_lost in marked and db.get_film(f_lost)["status"] == "failed", marked)
    check("причина понятная: предлагает загрузить видео заново",
          "заново" in (db.get_film(f_lost)["error"] or ""), db.get_film(f_lost)["error"])
    check("фильм с исходником не тронут", db.get_film(f_src)["status"] == "processing")
    check("уже собранный фильм без исходника не тронут",
          db.get_film(f_done)["status"] == "ready" and f_done not in marked)
    check("повторно failed не перепомечается", main.mark_lost_sources(db, cfg.media_dir) == [])

    # ---------- бан/разбан: флаг в списке пользователей ----------
    st, _ = await req(ADM, "POST", "/api/admin/ban", {"id": USR, "banned": True})
    async with aiohttp.ClientSession() as s:
        async with s.get(base + "/api/admin/users?q=", headers=H[ADM]) as r:
            urows = await r.json()
    u_usr = next((x for x in urows if x["id"] == USR), {})
    check("в списке пользователей есть флаг бана (без него нет кнопки «Разбанить»)",
          st == 200 and u_usr.get("banned") is True, u_usr)
    await req(ADM, "POST", "/api/admin/ban", {"id": USR, "banned": False})
    async with aiohttp.ClientSession() as s:
        async with s.get(base + "/api/admin/users?q=", headers=H[ADM]) as r:
            urows = await r.json()
    u_usr = next((x for x in urows if x["id"] == USR), {})
    check("после разбана флаг снят", u_usr.get("banned") is False)

    # ---------- сезонов больше нет ----------
    _, det = await req(USR, "GET", f"/api/films/{fid}")
    check("в данных о видео нет поля сезона", "season" not in det)
    ser = db.create_film(title="Сериал", kind="series", status="ready", owner_id=OWNER,
                         duration=0, width=0, height=0)
    db.create_film(title="Серия 1", kind="video", status="processing", owner_id=OWNER,
                   duration=10, width=64, height=64, series_id=ser, season=1, episode=1)
    e2 = db.create_film(title="Серия 2", kind="video", status="ready", owner_id=OWNER,
                        duration=10, width=64, height=64, series_id=ser, season=1, episode=2)
    check("повтор номера серии виден и без сезона",
          db.episode_exists(ser, 2) and not db.episode_exists(ser, 3))
    check("серии — плоский список по номерам", [e["episode"] for e in db.episodes(ser)] == [1, 2])
    db.conn.execute("UPDATE films SET season = 7 WHERE id = ?", (e2,))
    check("старый сезон схлопывается в общий список при запуске",
          Database(cfg.db_path).get_film(e2)["season"] == 1)

    await runner.cleanup()
    db.close()
    print("\n" + ("ПРОВАЛЕНО: " + "; ".join(FAILS) if FAILS else "ВСЕ ПРОВЕРКИ ПРОШЛИ"))
    sys.exit(1 if FAILS else 0)


def migration():
    """Старая база без новых колонок открывается и работает."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "old.db")
    con = sqlite3.connect(path)
    con.executescript("""
    CREATE TABLE users (tg_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
      role TEXT NOT NULL DEFAULT 'user', vip_until TEXT, last_seen TEXT);
    CREATE TABLE films (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, kind TEXT NOT NULL,
      status TEXT NOT NULL, genre TEXT, is_active INTEGER NOT NULL DEFAULT 1, owner_id INTEGER);
    CREATE TABLE reviews (id INTEGER PRIMARY KEY AUTOINCREMENT, film_id INTEGER NOT NULL,
      user_id INTEGER NOT NULL, rating INTEGER NOT NULL, text TEXT, created_at TEXT);
    CREATE TABLE sales (id INTEGER PRIMARY KEY AUTOINCREMENT, refunded INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE watch (user_id INTEGER, film_id INTEGER, position REAL, duration REAL, updated_at TEXT);
    CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, kind TEXT NOT NULL,
      text TEXT NOT NULL, assignee TEXT NOT NULL DEFAULT 'operator', created_at TEXT NOT NULL DEFAULT (datetime('now')));
    CREATE TABLE ticket_messages (id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER NOT NULL,
      author_id INTEGER NOT NULL, author_role TEXT NOT NULL, text TEXT NOT NULL,
      created_at TEXT NOT NULL DEFAULT (datetime('now')));
    INSERT INTO users (tg_id, role) VALUES (5, 'user');
    INSERT INTO tickets (user_id, kind, text) VALUES (5, 'bug', 'старый тикет');
    """)
    con.commit(); con.close()
    db = Database(path)
    row = db.get_user(5)
    t = db.tickets(user_id=5)
    fav_ok = db.toggle_favorite(5, 1)
    check("старая база: новые колонки добавились", "tour_seen" in row and "banned" in row, list(row))
    check("старая база: тикеты на месте", len(t) == 1 and t[0]["text"] == "старый тикет")
    check("старая база: избранное работает", fav_ok is True and db.favorites(5) == [1])
    db.close()


if __name__ == "__main__":
    migration()
    asyncio.run(run())

// Клиентские проверки (jsdom): подсказки, громкость-полоса, тикеты (картинка base64,
// ?init= в src, кнопки по ролям, delete на закрытом), избранное без двойной перерисовки.
// Запуск: NODE_PATH=<папка с node_modules> node tests/ui_test.js
const fs = require("fs");
const path = require("path");
let JSDOM;
try { ({ JSDOM } = require("jsdom")); }
catch (_) { ({ JSDOM } = require("/home/user/filmtests/node_modules/jsdom")); }
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf-8");
let fails = [];
const check = (name, cond, extra) => {
  console.log((cond ? "OK  " : "FAIL") + " " + name + (cond || extra === undefined ? "" : "  " + JSON.stringify(extra)));
  if (!cond) fails.push(name);
};

let me = { guest: false, id: 5, name: "Юзер", role: "user", roleTitle: "Пользователь", vipUntil: null,
  vipActive: false, vipForever: false, stars: 10, nameGradient: null, reviewLimit: 1, banned: false,
  canUpload: false, canManage: false, isOwner: false, tourSeen: false };
const films = [
  { id: 1, title: "Наруто", kind: "video", genre: "anime", genreTitle: "Аниме", isSeries: false, isVip: true,
    locked: false, views: 5, rating: 5, reviewCount: 1, description: "Ниндзя", reviews: [], hasPoster: false,
    releaseDate: "2002-10-03", creator: "Кисимото", country: "JP", status: "ready", duration: 1400,
    reviewLimit: 1, canReview: true, seriesId: null, isActive: true, favorite: false, subtitles: [] },
  { id: 2, title: "Том и Джерри", kind: "video", genre: "cartoon", genreTitle: "Мультфильмы", isSeries: false,
    isVip: false, locked: false, views: 9, rating: 4, reviewCount: 1, description: "Кот и мышь", reviews: [],
    hasPoster: false, releaseDate: "1940-02-10", creator: "Ханна", country: "US", status: "ready",
    duration: 420, reviewLimit: 1, canReview: true, seriesId: null, isActive: true, favorite: true, subtitles: [] },
];
const STAFF = ["operator", "editor", "admin", "owner"];
const perms = (t) => ({ canClose: t.authorId === me.id || STAFF.includes(me.role),
  canDelete: STAFF.includes(me.role),
  canTransfer: me.role === "operator" && t.assignee === "operator" });

const tickets = [
  { id: 12, kind: "bug", kindTitle: "Баг", text: "Не работает звук", createdAt: "2026-10-10 08:00:00",
    assignee: "operator", authorId: 5, author: "Юзер", queue: 1, closed: false,
    messages: [{ id: 1, text: "Здравствуйте", date: "2026-10-10 08:05:00", mine: false, role: "operator", roleTitle: "Оператор" }] },
];
let ticketState, posts, postBodies;

function reply(url, opts) {
  const body = opts && opts.body && typeof opts.body === "string" ? JSON.parse(opts.body) : {};
  if (url === "/api/me") return { ...me };
  if (url === "/api/tour") { me.tourSeen = true; return { ok: true }; }
  if (url === "/api/config") return { prices: { vipMonth: 199, vipYear: 900 }, donate: [], genres: [] };
  if (url === "/api/films") return films;
  if (url === "/api/favorites") return films.filter((f) => f.favorite).map((f) => ({ ...f }));
  if (/favorite$/.test(url)) {
    const f = films.find((x) => url.includes("/" + x.id + "/"));
    f.favorite = !f.favorite;
    return { ok: true, favorite: f.favorite };
  }
  if (url === "/api/tickets") {
    let list = ticketState;
    if (me.role === "operator") list = ticketState.filter((t) => t.assignee === "operator");
    else if (me.role === "user") list = ticketState.filter((t) => t.authorId === me.id);
    return { kinds: [{ id: "bug", title: "Баг" }], answer: "Тикет отправлен",
      tickets: list.map((t) => ({ ...t, mine: t.authorId === me.id, ...perms(t) })) };
  }
  const one = url.match(/^\/api\/tickets\/(\d+)$/);
  if (one) {
    const t = ticketState.find((x) => String(x.id) === one[1]);
    return t ? { ...t, mine: t.authorId === me.id, ...perms(t) } : { error: "not_found" };
  }
  if (/^\/api\/tickets\/\d+\/images$/.test(url)) {
    if (!/^data:image\//.test(body.image || "")) return { error: "bad_image" };
    return { ok: true, name: "a1b2c3.png", url: url + "/a1b2c3.png" };
  }
  if (/\/messages$/.test(url)) return { ok: true, message: { id: 99, text: body.text,
    date: "2026-10-10 09:00:00", mine: true, role: me.role, roleTitle: "Оператор",
    image: body.image ? "/api/tickets/12/images/" + body.image : null } };
  if (/\/close$/.test(url)) { const t = ticketState.find((x) => url.includes("/" + x.id + "/")); t.closed = true; return { ok: true, ttl: 600 }; }
  if (/\/delete$/.test(url)) { ticketState = ticketState.filter((x) => !url.includes("/" + x.id + "/")); return { ok: true }; }
  if (url === "/api/admin/uploads") return { id: "u" + Math.random().toString(36).slice(2, 8), chunkSize: 1024 * 1024, total: 1 };
  if (/\/api\/admin\/users/.test(url)) return [{ id: 7, name: "Тест Ю", role: "user", stars: 5, vipActive: false, vipForever: false, banned: true }];
  if (/^\/api\/admin\/uploads\/.+\/\d+$/.test(url)) return { ok: true, received: 1, total: 1 };
  return { ok: true };
}

function boot(role, id) {
  me = Object.assign({}, me, { role, id: id || 5, roleTitle: role, canManage: role === "admin" || role === "owner",
    canUpload: role !== "user", isOwner: role === "owner" });
  ticketState = JSON.parse(JSON.stringify(tickets));
  posts = []; postBodies = [];
  const dom = new JSDOM(html, { url: "https://app.test/#/catalog", runScripts: "outside-only", pretendToBeVisual: true });
  const { window } = dom;
  window.Telegram = { WebApp: { ready() {}, expand() {} } };
  window.fetch = async (url, opts) => {
    posts.push(String(url).split("?")[0]);
    postBodies.push({ url: String(url), body: opts && opts.body });
    return { ok: true, status: 200, json: async () => reply(String(url), opts), text: async () => "{}" };
  };
  window.Hls = { isSupported: () => false, Events: {} };
  window.confirm = () => true;
  window.scrollTo = () => {};
  window.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
  return window;
}
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

(async () => {
  // ---------- подсказки при первом входе ----------
  let w = boot("user");
  await sleep(500);
  let d = w.document;
  check("подсказки появились при первом входе", !!d.querySelector(".tour-card") && !!d.querySelector(".tour-spot"));
  check("карточка серая и плотная", /\.tour-card \{[^}]*background: #c9c9d1/.test(html));
  check("карточка ставится строго выше кнопки и не накрывает её",
    /card\.style\.top = Math\.max\(12, r\.top - card\.offsetHeight - 16\)/.test(html));
  check("в карточке «Дальше» и «Пропустить»", d.querySelector(".tour-next").textContent === "Дальше" &&
    d.querySelector(".tour-skip").textContent === "Пропустить");
  check("крестик справа сверху", !!d.querySelector(".tour-x"));
  d.querySelector(".tour-next").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(60);
  check("«Дальше» переключает кнопку", d.querySelector(".tour-card b").textContent === "Избранное");
  d.querySelector(".tour-x").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(120);
  check("крестик закрыл подсказки", !d.querySelector(".tour-card"));
  check("отметка ушла на сервер", posts.includes("/api/tour"));
  w = boot("user");   // me.tourSeen теперь true
  await sleep(500);
  check("повторно подсказок нет", !w.document.querySelector(".tour-card"));

  // ---------- громкость полосой, без круглого бегунка ----------
  check("у громкости нет круглого бегунка", !html.includes("-slider-thumb"));
  check("заливка полосы как у перемотки", html.includes(".vp-vfill"));
  check("полоса тянется pointer-событиями (телефон и ПК)",
    /volBar\.addEventListener\("pointerdown"/.test(html) && /volBar\.addEventListener\("pointermove"/.test(html));
  check("старого input-range у громкости нет", !html.includes('id="vpVolFlat" min='));

  // ---------- окна чёрные, поле тикета выше ----------
  check("окна чёрные полупрозрачные", /\.modal \{[^}]*background: rgba\(0,0,0,\.62\)/.test(html));
  check("поле ввода тикета поднято", /\.tk-dock \{[^}]*bottom: calc\(104px/.test(html));

  // ---------- избранное: сердечко кладёт в список, без двойной перерисовки ----------
  w = boot("user");
  await sleep(400);
  d = w.document;
  const paints = { n: 0 };
  const dsc = Object.getOwnPropertyDescriptor(w.Element.prototype, "innerHTML");
  Object.defineProperty(w.Element.prototype, "innerHTML", {
    get() { return dsc.get.call(this); },
    set(v) { if (typeof v === "string" && v.indexOf('class="card"') >= 0) paints.n++; return dsc.set.call(this, v); },
    configurable: true,
  });
  d.querySelector("[data-fav=\"1\"]").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(150);
  w.location.hash = "#/favorites";
  await sleep(300);
  w.location.hash = "#/catalog"; await sleep(200);
  const before = paints.n;
  w.location.hash = "#/favorites"; await sleep(400);
  check("повторный заход в избранное рисует список один раз", paints.n === before + 1, [before, paints.n]);
  check("отложенное сердечком сразу в избранном",
    [...w.document.querySelectorAll("#favList .card")].some((c) => c.textContent.includes("Наруто")));

  // ---------- тикет глазами автора: превью, без надписи, ?init= ----------
  w = boot("user");
  await sleep(300);
  w.location.hash = "#/support/12";
  await sleep(400);
  d = w.document;
  check("у автора нет «Удалить тикет»", !d.getElementById("tkDel"));
  check("у автора есть «Закрыть»", !!d.getElementById("tkClose"));
  check("превью картинки над полем ввода", d.querySelector(".tk-dock").firstElementChild.id === "tkPick");
  const png = new w.File([new Uint8Array([0x89, 0x50, 0x4e, 0x47])], "shot.png", { type: "image/png" });
  const paste = () => {
    const ev = new w.Event("paste", { bubbles: true, cancelable: true });
    ev.clipboardData = { items: [{ type: "image/png", getAsFile: () => png }] };
    d.getElementById("tkReply").dispatchEvent(ev);
  };
  paste(); await sleep(250);
  check("превью появилось, в поле ввода ничего не пишется",
    !d.getElementById("tkPick").classList.contains("hidden") &&
    d.getElementById("tkReply").placeholder === "Сообщение");
  d.getElementById("tkPickX").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(60);
  check("крестик убрал картинку до отправки", d.getElementById("tkPick").classList.contains("hidden") &&
    !postBodies.some((x) => /\/images$/.test(x.url)));
  paste(); await sleep(250);
  d.getElementById("tkReply").value = "крутое аниме спасибо";
  d.getElementById("tkSendMsg").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(350);
  check("картинка ушла base64", postBodies.some((x) => /\/images$/.test(x.url) && String(x.body).includes("data:image/png;base64,")));
  const shot = d.querySelector(".tk-shot");
  check("в сообщении картинка с подписью ?init= — откроется из <img>",
    !!shot && shot.src.includes("init="), shot && shot.src);

  // ---------- закрыл тикет — «Удалить» работает (персонал) ----------
  w = boot("operator", 2);
  await sleep(300);
  w.location.hash = "#/support/12";
  await sleep(400);
  d = w.document;
  check("у оператора «Передать админам» и «Удалить»", !!d.getElementById("tkUp") && !!d.getElementById("tkDel"));
  d.getElementById("tkClose").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(300);
  w.location.hash = "#/support"; await sleep(200);
  w.location.hash = "#/support/12"; await sleep(400);
  d = w.document;
  check("в закрытом тикете нет поля ввода", !d.getElementById("tkReply"));
  check("в закрытом тикете есть «Удалить»", !!d.getElementById("tkDel"));
  posts = [];
  d.getElementById("tkDel").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(300);
  check("«Удалить» на закрытом тикете сработал", posts.includes("/api/tickets/12/delete"), posts);

  // ---------- у админа нет «Передать админам» ----------
  w = boot("admin", 4);
  await sleep(300);
  w.location.hash = "#/support/12";
  await sleep(400);
  d = w.document;
  check("у админа нет «Передать админам»", !d.getElementById("tkUp"));
  check("у админа есть «Удалить»", !!d.getElementById("tkDel"));

  // ---------- в интерфейсе нет слова «фильм» ----------
  const markup = html.replace(/<script>[\s\S]*?<\/script>/, "").replace(/<style>[\s\S]*?<\/style>/, "");
  // «фильм» недопустим, но «мультфильм» — разрешённое слово: исключаем его lookbehind
  check("в интерфейсе нет слова «фильм»", !/(?<![а-яёь])фильм/i.test(markup));

  // ---------- загрузка видео не нагружает компьютер ----------
  const upFn = html.slice(html.indexOf("async function uploadVideoChunked"), html.indexOf("function uploadPoster"));
  check("видео уходит срезом файла, без чтения в память", !upFn.includes("FileReader") && upFn.includes("file.slice"));
  check("размер части приходит с сервера и между частями передышка",
    upFn.includes("init.chunkSize") && /setTimeout\(res, 50\)/.test(upFn));
  check("при обрыве часть повторяется до 5 раз", /attempt < 5/.test(upFn));

  // ---------- несколько фильмов за раз: очередь по одному ----------
  w = boot("admin", 4);
  await sleep(300);
  w.location.hash = "#/admin/upload";
  await sleep(400);
  d = w.document;
  const fileInput = d.getElementById("u-file");
  check("можно выбрать несколько видео сразу", fileInput.hasAttribute("multiple"));
  const created = [];
  w.XMLHttpRequest = class {
    open(m, u) { this.url = u; }
    setRequestHeader() {}
    send(fd) {
      created.push({ url: this.url, title: fd && fd.get ? fd.get("title") : null, hasUpload: fd && fd.get ? !!fd.get("uploadId") : false });
      setTimeout(() => { this.status = 200; this.responseText = JSON.stringify({ ok: true, id: 500 + created.length }); this.onload && this.onload(); }, 5);
    }
  };
  const f1 = new w.File([new Uint8Array(10)], "one.mp4", { type: "video/mp4" });
  const f2 = new w.File([new Uint8Array(10)], "two.mp4", { type: "video/mp4" });
  Object.defineProperty(fileInput, "files", { value: [f1, f2], configurable: true });
  const note = d.getElementById("u-prognote");
  posts = [];
  d.getElementById("u-send").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(700);
  const seq = posts.join(",");
  const inits = posts.filter((u) => u === "/api/admin/uploads").length;
  const puts = posts.filter((u) => /\/api\/admin\/uploads\/.+\/\d+$/.test(u)).length;
  check("оба файла загружены: у каждого своя инициализация и части", inits === 2 && puts === 2, { inits, puts });
  check("файлы идут строго по очереди, не параллельно",
    /^\/api\/admin\/uploads,\/api\/admin\/uploads\/.+,\/api\/admin\/uploads,\/api\/admin\/uploads\/.+$/.test(seq), seq);
  check("создано два фильма, у каждого свой uploadId", created.length === 2 &&
    created.every((c) => c.url === "/api/admin/films" && c.hasUpload), created);
  check("названия из имён файлов (поправятся в редакторе)", created[0].title === "one" && created[1].title === "two",
    created.map((c) => c.title));
  check("подпись очереди показывает номер файла", note.textContent.includes("2 из 2"), note.textContent);

  // ---------- пустой каталог: честный текст вместо вечной «Загрузка...» ----------
  films.length = 0;
  w = boot("user", 6);
  await sleep(400);
  d = w.document;
  check("пустой каталог говорит «Пока ничего нет»", d.querySelector("#list").textContent.includes("Пока ничего нет"),
    d.querySelector("#list").textContent);

  // ---------- Liquid Glass и iOS ----------
  check("стеклянные панели блюрят с webkit-префиксом",
    /\.glass\s*{[^}]*-webkit-backdrop-filter/.test(html) && /\.glass\s*{[^}]*backdrop-filter/.test(html));
  check("есть запасной вариант для браузеров без блюра", html.includes("@supports not ((backdrop-filter"));
  check("у видео атрибут инлайн-воспроизведения для iOS", html.includes("playsinline webkit-playsinline"));

  // ---------- админка: структура, разбан, сезоны удалены, старт на ПК ----------
  check("внешние скрипты не блокируют загрузку страницы (defer)", !/<script (?![^>]*defer)[^>]*src=/.test(html));
  check("до старта JS видна заглушка, а не пустой экран", /id="app"><div class="empty">/.test(html));
  check("сезонов в коде не осталось", !html.includes("season") && !html.includes("Сезон"));
  w = boot("admin", 4);
  await sleep(300);
  w.location.hash = "#/admin/upload";
  await sleep(400);
  d = w.document;
  check("загрузка разбита на карточки с заголовками", d.querySelectorAll(".adm-card").length >= 3 &&
    d.querySelector("#admin h2").textContent.includes("Что добавляем"));
  w.location.hash = "#/admin/users";
  await sleep(400);
  check("у забаненного видна кнопка «Разбанить»",
    d.querySelector("#ulist").textContent.includes("Разбанить") &&
    d.querySelector("#ulist").textContent.includes("Забанен"));

  console.log("\n" + (fails.length ? "ПРОВАЛЕНО: " + fails.join("; ") : "ВСЕ ПРОВЕРКИ ПРОШЛИ"));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("ОШИБКА ТЕСТА:", e); process.exit(2); });

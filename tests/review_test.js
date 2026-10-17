// Отзыв появляется в списке сразу, без перезахода; удаляется тоже без перезахода;
// видео без чёрных рамок (object-fit: cover) в окне и в полном экране.
const fs = require("fs");
const path = require("path");
let JSDOM;
try { ({ JSDOM } = require("jsdom")); } catch (_) { ({ JSDOM } = require("/home/user/filmtests/node_modules/jsdom")); }
const html = fs.readFileSync(path.join(__dirname, "..", "index.html"), "utf-8");
let fails = [];
const check = (name, cond, extra) => {
  console.log((cond ? "OK  " : "FAIL") + " " + name + (cond || extra === undefined ? "" : "  " + JSON.stringify(extra)));
  if (!cond) fails.push(name);
};

const me = { guest: false, id: 5, name: "Юзер", role: "user", roleTitle: "Пользователь", vipActive: false,
  vipForever: false, stars: 10, nameGradient: null, reviewLimit: 1, banned: false, canUpload: false,
  canManage: false, isOwner: false, tourSeen: true };
const film = { id: 1, title: "Наруто", kind: "video", genre: "anime", genreTitle: "Аниме", isSeries: false,
  isVip: false, locked: false, views: 5, rating: 5, reviewCount: 0, description: "Ниндзя",
  reviews: [], hasPoster: false, releaseDate: "2002-10-03", creator: "Кисимото", country: "JP",
  status: "ready", duration: 1400, reviewLimit: 2, canReview: true, seriesId: null, isActive: true,
  favorite: false, subtitles: [] };

function reply(url, opts) {
  const body = opts && opts.body && typeof opts.body === "string" ? JSON.parse(opts.body) : {};
  if (url === "/api/me") return me;
  if (url === "/api/config") return { prices: {}, donate: [], genres: [] };
  if (url === "/api/films") return [film];
  if (url === "/api/films/1") return film;
  if (url === "/api/films/1/reviews") {
    film.reviews = [{ id: film.reviews.length + 1, userId: me.id, name: me.name, rating: body.rating,
      text: body.text, date: "2026-10-10 16:00:00", vip: false, canDelete: true }, ...film.reviews];
    film.reviewCount = film.reviews.length;
    return { ok: true };
  }
  if (/\/api\/reviews\/\d+\/delete$/.test(url)) {
    film.reviews = film.reviews.filter((r) => !url.includes("/" + r.id + "/"));
    film.reviewCount = film.reviews.length;
    return { ok: true };
  }
  return { ok: true };
}

(async () => {
  const dom = new JSDOM(html, { url: "https://app.test/#/film/1", runScripts: "outside-only", pretendToBeVisual: true });
  const w = dom.window;
  w.Telegram = { WebApp: { ready() {}, expand() {} } };
  w.fetch = async (url, opts) => ({ ok: true, status: 200, json: async () => reply(String(url), opts), text: async () => "{}" });
  w.Hls = { isSupported: () => false, Events: {} };
  w.confirm = () => true;
  w.scrollTo = () => {};
  w.eval(html.match(/<script>([\s\S]*?)<\/script>/)[1]);
  const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  await sleep(600);
  let d = w.document;

  check("видео без чёрных рамок в обычном окне", /\.vp video \{[^}]*object-fit: cover;/.test(html));
  check("видео без чёрных рамок в полном экране", /\.vp\.fs video \{ object-fit: cover; \}/.test(html));

  check("до отзыва список пуст", d.querySelectorAll("#revList .item").length === 0);
  const vidBefore = d.getElementById("vid");
  d.getElementById("leaveReview").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(200);
  d.getElementById("modalBg").querySelector('#starPick button[data-star="5"]').dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  d.getElementById("modalBg").querySelector("#reviewText").value = "крутое аниме спасибо";
  d.getElementById("modalBg").querySelector("#sendReview").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(500);
  d = w.document;
  check("отзыв появился без перезахода", [...d.querySelectorAll("#revList .item")]
    .some((i) => i.textContent.includes("крутое аниме спасибо")));
  check("кнопка «Оставить ещё» появилась (лимит 2)", !!d.getElementById("leaveReview2"));
  check("видео не пересоздано", d.getElementById("vid") === vidBefore);

  d.querySelector("[data-del-review]").dispatchEvent(new w.MouseEvent("click", { bubbles: true }));
  await sleep(400);
  check("после удаления отзыв исчез без перезахода", d.querySelectorAll("#revList .item").length === 0);

  console.log("\n" + (fails.length ? "ПРОВАЛЕНО: " + fails.join("; ") : "ВСЕ ПРОВЕРКИ ПРОШЛИ"));
  process.exit(fails.length ? 1 : 0);
})().catch((e) => { console.error("ОШИБКА ТЕСТА:", e); process.exit(2); });

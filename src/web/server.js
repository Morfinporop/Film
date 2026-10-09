import { readFile } from 'node:fs/promises';
import { webhookCallback } from 'grammy';

const APP_HTML_URL = new URL('./public/app.html', import.meta.url);

/**
 * HTTP-обработчик приложения:
 *  POST {WEBHOOK_PATH}  — входящие обновления Telegram (проверка secret token)
 *  GET  /app            — Mini App (каталог)
 *  GET  /api/config     — имя бота для Mini App
 *  GET  /api/films      — список активных фильмов
 *  GET  /api/banner/:id — баннер фильма (прокси: токен бота не уходит в браузер)
 *  GET  /health         — проверка работоспособности
 * @param {{config: object, db: object, bot: import('grammy').Bot}} deps
 */
export function createHttpHandler({ config, db, bot }) {
  const handleUpdate = webhookCallback(bot, 'http', { secretToken: config.webhookSecret });
  let appHtmlCache = null;

  const sendJson = (res, status, data) => {
    res.writeHead(status, {
      'content-type': 'application/json; charset=utf-8',
      'cache-control': 'no-store',
    });
    res.end(JSON.stringify(data));
  };

  const sendText = (res, status, text) => {
    res.writeHead(status, { 'content-type': 'text/plain; charset=utf-8' });
    res.end(text);
  };

  async function serveBanner(res, id) {
    const film = db.getFilm(id);
    if (!film || !film.is_active || !film.photo_file_id) return sendText(res, 404, 'not found');
    try {
      const file = await bot.api.getFile(film.photo_file_id);
      const upstream = await fetch(
        `https://api.telegram.org/file/bot${config.botToken}/${file.file_path}`,
      );
      if (!upstream.ok) return sendText(res, 502, 'upstream error');
      const buf = Buffer.from(await upstream.arrayBuffer());
      res.writeHead(200, {
        'content-type': 'image/jpeg',
        'cache-control': 'public, max-age=3600',
      });
      res.end(buf);
    } catch (err) {
      console.error('[web] баннер не загружен:', err.message);
      sendText(res, 502, 'banner error');
    }
  }

  return async function handler(req, res) {
    const url = new URL(req.url ?? '/', 'http://localhost');
    const { pathname } = url;

    try {
      if (req.method === 'POST' && pathname === config.webhookPath) {
        return await handleUpdate(req, res);
      }

      if (req.method !== 'GET' && req.method !== 'HEAD') {
        return sendText(res, 405, 'method not allowed');
      }

      if (pathname === '/health') return sendText(res, 200, 'ok');

      if (pathname === '/app' || pathname === '/app/') {
        appHtmlCache ??= await readFile(APP_HTML_URL, 'utf8');
        res.writeHead(200, {
          'content-type': 'text/html; charset=utf-8',
          'cache-control': 'no-cache',
        });
        return res.end(appHtmlCache);
      }

      if (pathname === '/api/config') {
        return sendJson(res, 200, { botUsername: bot.botInfo?.username ?? null });
      }

      if (pathname === '/api/films') {
        const films = db.listActiveFilms().map((f) => ({
          id: f.id,
          title: f.title,
          kind: f.kind,
          // для ссылочных фильмов отдаём переход; для видео — только через бота
          url: f.kind === 'link' ? f.url : null,
          hasBanner: Boolean(f.photo_file_id),
        }));
        return sendJson(res, 200, films);
      }

      const banner = /^\/api\/banner\/(\d+)$/.exec(pathname);
      if (banner) return await serveBanner(res, Number(banner[1]));

      return sendText(res, 404, 'not found');
    } catch (err) {
      console.error('[web] ошибка запроса:', err);
      if (!res.headersSent) sendText(res, 500, 'internal error');
    }
  };
}

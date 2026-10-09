import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { openDb } from '../src/db/index.js';
import { createHttpHandler } from '../src/web/server.js';

const config = {
  botToken: '1:x',
  webhookPath: '/webhook',
  webhookSecret: 'secret_123',
};

// Минимальный фейковый бот: проверяем маршрутизацию и секрет без сети.
const fakeBot = {
  botInfo: { username: 'film_test_bot' },
  isRunning: () => false,
  init: async () => {},
  handleUpdate: async () => {},
  api: {},
};

async function withServer(db, fn) {
  const server = createServer(createHttpHandler({ config, db, bot: fakeBot }));
  await new Promise((r) => server.listen(0, '127.0.0.1', r));
  const base = `http://127.0.0.1:${server.address().port}`;
  try {
    await fn(base);
  } finally {
    await new Promise((r) => server.close(r));
  }
}

test('health, конфиг и список фильмов', async () => {
  const db = openDb(':memory:');
  db.createFilm({ title: 'Ссылка', kind: 'link', url: 'https://ex.ru/a', photo_file_id: 'ph' });
  db.createFilm({ title: 'Видео', kind: 'video', video_file_id: 'vid' });
  await withServer(db, async (base) => {
    assert.equal(await (await fetch(`${base}/health`)).text(), 'ok');

    const cfg = await (await fetch(`${base}/api/config`)).json();
    assert.equal(cfg.botUsername, 'film_test_bot');

    const films = await (await fetch(`${base}/api/films`)).json();
    assert.equal(films.length, 2);
    const link = films.find((f) => f.kind === 'link');
    const video = films.find((f) => f.kind === 'video');
    assert.equal(link.url, 'https://ex.ru/a');
    assert.equal(link.hasBanner, true);
    assert.equal(video.url, null, 'видео не отдаём как внешнюю ссылку');
    assert.equal(video.hasBanner, false);
  });
  db.close();
});

test('вебхук отвергает запрос без правильного secret token', async () => {
  const db = openDb(':memory:');
  await withServer(db, async (base) => {
    const res = await fetch(`${base}/webhook`, {
      method: 'POST',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ update_id: 1 }),
    });
    assert.equal(res.status, 401);
  });
  db.close();
});

test('mini app отдаётся по /app, неизвестные пути — 404', async () => {
  const db = openDb(':memory:');
  await withServer(db, async (base) => {
    const app = await fetch(`${base}/app`);
    assert.equal(app.status, 200);
    assert.match(await app.text(), /Каталог фильмов/);
    assert.equal((await fetch(`${base}/nope`)).status, 404);
  });
  db.close();
});

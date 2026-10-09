import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openDb } from '../src/db/index.js';
import { createBot } from '../src/bot/index.js';

const ADMIN = 7745681987;
const USER = 42;

/** Создаёт бота с перехватом вызовов Telegram API (сети не нужно). */
async function setup() {
  const config = {
    botToken: '1:test',
    adminIds: new Set([ADMIN]),
    publicUrl: 'https://bot.example.ru',
  };
  const db = openDb(':memory:');
  const bot = createBot({ config, db });
  const calls = [];
  bot.api.config.use(async (prev, method, payload) => {
    calls.push({ method, payload });
    if (method === 'getMe') {
      return { ok: true, result: { id: 1, is_bot: true, first_name: 'F', username: 'film_test_bot' } };
    }
    return { ok: true, result: { message_id: calls.length, chat: { id: payload?.chat_id ?? 0 } } };
  });
  await bot.init();
  return { bot, db, calls, config };
}

let updateId = 1;
const msg = (from, extra) => ({
  update_id: updateId++,
  message: {
    message_id: updateId,
    date: Math.floor(Date.now() / 1000),
    chat: { id: from, type: 'private', first_name: 'x' },
    from: { id: from, is_bot: false, first_name: 'x' },
    ...extra,
  },
});
const cb = (from, data) => ({
  update_id: updateId++,
  callback_query: {
    id: String(updateId),
    from: { id: from, is_bot: false, first_name: 'x' },
    chat_instance: '1',
    data,
    message: { message_id: 1, date: 0, chat: { id: from, type: 'private' } },
  },
});

test('/start показывает сетку каталога и кнопку Mini App', async () => {
  const { bot, db, calls } = await setup();
  db.createFilm({ title: 'Фильм А', kind: 'link', url: 'https://ex.ru/a' });
  db.createFilm({ title: 'Фильм Б', kind: 'video', video_file_id: 'v' });
  await bot.handleUpdate(msg(USER, { text: '/start', entities: [{ type: 'bot_command', offset: 0, length: 6 }] }));
  const send = calls.find((c) => c.method === 'sendMessage');
  assert.ok(send);
  const rows = send.payload.reply_markup.inline_keyboard;
  assert.equal(rows[0].length, 2);
  assert.match(rows[0][0].callback_data, /^f:\d+$/);
  assert.ok(rows.at(-1)[0].web_app, 'кнопка Mini App есть');
});

test('deep link /start film_ID присылает видео сразу', async () => {
  const { bot, db, calls } = await setup();
  const id = db.createFilm({ title: 'Видео-фильм', kind: 'video', video_file_id: 'VID' });
  await bot.handleUpdate(msg(USER, { text: `/start film_${id}`, entities: [{ type: 'bot_command', offset: 0, length: 6 }] }));
  const v = calls.find((c) => c.method === 'sendVideo');
  assert.equal(v.payload.video, 'VID');
  assert.equal(db.getFilm(id).views, 1);
});

test('кнопка фильма отдаёт карточку со ссылкой', async () => {
  const { bot, db, calls } = await setup();
  const id = db.createFilm({ title: 'Ссылочный', kind: 'link', url: 'https://ex.ru/go', photo_file_id: 'PH' });
  await bot.handleUpdate(cb(USER, `f:${id}`));
  const p = calls.find((c) => c.method === 'sendPhoto');
  assert.equal(p.payload.photo, 'PH');
  assert.equal(p.payload.reply_markup.inline_keyboard[0][0].url, 'https://ex.ru/go');
});

test('не-админ не получает доступа к /add', async () => {
  const { bot, calls } = await setup();
  await bot.handleUpdate(msg(USER, { text: '/add', entities: [{ type: 'bot_command', offset: 0, length: 4 }] }));
  assert.equal(calls.filter((c) => c.method === 'sendMessage').length, 0);
});

test('полный мастер: баннер → название → ссылка → сохранение', async () => {
  const { bot, db, calls } = await setup();
  const cmd = (t) => msg(ADMIN, { text: t, entities: [{ type: 'bot_command', offset: 0, length: t.length }] });

  await bot.handleUpdate(cmd('/add'));
  await bot.handleUpdate(msg(ADMIN, { photo: [{ file_id: 's', file_unique_id: 's', width: 1, height: 1 }, { file_id: 'BIG', file_unique_id: 'b', width: 9, height: 9 }] }));
  await bot.handleUpdate(msg(ADMIN, { text: 'Интерстеллар' }));
  await bot.handleUpdate(cb(ADMIN, 'wz:kind:link'));
  await bot.handleUpdate(msg(ADMIN, { text: 'не ссылка' }));
  await bot.handleUpdate(msg(ADMIN, { text: 'https://tiktok.example/redir' }));
  await bot.handleUpdate(cb(ADMIN, 'wz:save'));

  const [film] = db.listAllFilms();
  assert.equal(film.title, 'Интерстеллар');
  assert.equal(film.kind, 'link');
  assert.equal(film.photo_file_id, 'BIG', 'берётся самое большое фото');
  assert.equal(film.url, 'https://tiktok.example/redir');
  assert.ok(calls.some((c) => c.method === 'sendPhoto'), 'превью отправлено');
});

test('мастер видео: видеофайл в боте', async () => {
  const { bot, db } = await setup();
  await bot.handleUpdate(msg(ADMIN, { text: '/add', entities: [{ type: 'bot_command', offset: 0, length: 4 }] }));
  await bot.handleUpdate(msg(ADMIN, { video: { file_id: 'VIDEO1', file_unique_id: 'u', width: 1, height: 1, duration: 1 } }));
  await bot.handleUpdate(msg(ADMIN, { text: 'Клип' }));
  await bot.handleUpdate(cb(ADMIN, 'wz:kind:video'));
  await bot.handleUpdate(cb(ADMIN, 'wz:save'));
  const [film] = db.listAllFilms();
  assert.equal(film.kind, 'video');
  assert.equal(film.video_file_id, 'VIDEO1');
});

import { test } from 'node:test';
import assert from 'node:assert/strict';
import { openDb } from '../src/db/index.js';
import { catalogGrid } from '../src/bot/cards.js';

test('CRUD фильмов и просмотры', () => {
  const db = openDb(':memory:');
  const id = db.createFilm({ title: 'Матрица', kind: 'link', url: 'https://t.me/x/1' });
  assert.equal(db.getFilm(id).title, 'Матрица');
  assert.equal(db.listActiveFilms().length, 1);

  db.incrementViews(id);
  assert.equal(db.getFilm(id).views, 1);

  assert.equal(db.setActive(id, false), true);
  assert.equal(db.listActiveFilms().length, 0);
  assert.equal(db.listAllFilms().length, 1);

  assert.equal(db.deleteFilm(id), true);
  assert.equal(db.getFilm(id), null);
  db.close();
});

test('CHECK: ссылочный фильм без url и видео без file_id запрещены', () => {
  const db = openDb(':memory:');
  assert.throws(() => db.createFilm({ title: 'a', kind: 'link' }));
  assert.throws(() => db.createFilm({ title: 'a', kind: 'video' }));
  db.close();
});

test('сетка каталога: по 2 кнопки в ряд', () => {
  const films = [1, 2, 3, 4, 5].map((id) => ({ id, title: `F${id}` }));
  const rows = catalogGrid(films).inline_keyboard;
  assert.deepEqual(rows.map((r) => r.length), [2, 2, 1]);
  assert.equal(rows[0][0].callback_data, 'f:1');
});

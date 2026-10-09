import { test } from 'node:test';
import assert from 'node:assert/strict';
import { loadConfig } from '../src/config.js';

const base = {
  BOT_TOKEN: '123:abc',
  ADMIN_IDS: '7745681987',
  PUBLIC_URL: 'https://bot.example.ru/',
};

test('читает конфиг из переменных окружения', () => {
  const c = loadConfig(base);
  assert.equal(c.botToken, '123:abc');
  assert.ok(c.adminIds.has(7745681987));
  assert.equal(c.publicUrl, 'https://bot.example.ru');
  assert.equal(c.port, 3000);
  assert.equal(c.mode, 'webhook');
  assert.equal(c.webhookPath, '/webhook');
  assert.match(c.webhookSecret, /^[a-f0-9]{64}$/);
});

test('без BOT_TOKEN падает с понятной ошибкой', () => {
  assert.throws(() => loadConfig({ ...base, BOT_TOKEN: '' }), /BOT_TOKEN/);
});

test('несколько админов через запятую и пробелы', () => {
  const c = loadConfig({ ...base, ADMIN_IDS: '1, 2;3' });
  assert.deepEqual([...c.adminIds], [1, 2, 3]);
});

test('webhook требует https PUBLIC_URL', () => {
  assert.throws(() => loadConfig({ ...base, PUBLIC_URL: '' }), /PUBLIC_URL/);
  assert.throws(() => loadConfig({ ...base, PUBLIC_URL: 'http://x.ru' }), /https/);
});

test('polling не требует PUBLIC_URL', () => {
  const c = loadConfig({ BOT_TOKEN: 'x', ADMIN_IDS: '5', BOT_MODE: 'polling' });
  assert.equal(c.mode, 'polling');
});

test('некорректный ADMIN_IDS отклоняется', () => {
  assert.throws(() => loadConfig({ ...base, ADMIN_IDS: 'abc' }), /ADMIN_IDS/);
});

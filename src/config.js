import { createHash } from 'node:crypto';
import { resolve } from 'node:path';

/**
 * Разбирает список Telegram ID из строки вида "123, 456;789".
 * @param {string | undefined} raw
 * @returns {Set<number>}
 */
function parseAdminIds(raw) {
  const ids = new Set();
  for (const part of String(raw ?? '').split(/[\s,;]+/).filter(Boolean)) {
    const id = Number(part);
    if (!Number.isSafeInteger(id) || id <= 0) {
      throw new Error(`ADMIN_IDS: некорректный Telegram ID "${part}"`);
    }
    ids.add(id);
  }
  return ids;
}

/**
 * Загружает и валидирует конфигурацию из переменных окружения.
 * Ничего не хранится в коде: все значения приходят из process.env.
 * @param {NodeJS.ProcessEnv} env
 */
export function loadConfig(env = process.env) {
  const botToken = env.BOT_TOKEN?.trim();
  if (!botToken) {
    throw new Error('BOT_TOKEN не задан: добавьте его в переменные окружения');
  }

  const adminIds = parseAdminIds(env.ADMIN_IDS);
  if (adminIds.size === 0) {
    throw new Error('ADMIN_IDS не задан: укажите Telegram ID администратора');
  }

  const mode = (env.BOT_MODE || 'webhook').trim().toLowerCase();
  if (mode !== 'webhook' && mode !== 'polling') {
    throw new Error(`BOT_MODE должен быть "webhook" или "polling", получено "${mode}"`);
  }

  const publicUrl = (env.PUBLIC_URL || '').trim().replace(/\/+$/, '') || null;
  if (mode === 'webhook') {
    if (!publicUrl) throw new Error('PUBLIC_URL обязателен в режиме webhook');
    if (!publicUrl.startsWith('https://')) {
      throw new Error('PUBLIC_URL должен начинаться с https:// (требование Telegram)');
    }
  }

  const port = Number(env.PORT || 3000);
  if (!Number.isInteger(port) || port <= 0 || port > 65535) {
    throw new Error(`PORT некорректен: "${env.PORT}"`);
  }

  let webhookPath = (env.WEBHOOK_PATH || '/webhook').trim();
  if (!webhookPath.startsWith('/')) webhookPath = `/${webhookPath}`;

  // Если секрет не задан явно, выводим его из токена: стабилен между перезапусками
  // и не попадает в репозиторий.
  const webhookSecret =
    env.WEBHOOK_SECRET?.trim() ||
    createHash('sha256').update(`webhook:${botToken}`).digest('hex');
  if (!/^[A-Za-z0-9_-]{1,256}$/.test(webhookSecret)) {
    throw new Error('WEBHOOK_SECRET: допустимы только латиница, цифры, _ и -');
  }

  return Object.freeze({
    botToken,
    adminIds,
    mode,
    publicUrl,
    port,
    webhookPath,
    webhookSecret,
    dbPath: resolve(env.DB_PATH || 'data/film.db'),
  });
}

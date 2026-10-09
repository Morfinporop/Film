import { createServer } from 'node:http';
import { loadConfig } from './config.js';
import { openDb } from './db/index.js';
import { createBot } from './bot/index.js';
import { createHttpHandler } from './web/server.js';

const config = loadConfig();
const db = openDb(config.dbPath);
const bot = createBot({ config, db });

// Получаем данные о боте (нужны для диплинков Mini App) и проверяем токен.
await bot.init();
console.log(`[bot] @${bot.botInfo.username} инициализирован, режим: ${config.mode}`);

await bot.api.setMyCommands([
  { command: 'start', description: 'Каталог фильмов' },
  { command: 'catalog', description: 'Открыть каталог' },
]);

if (config.publicUrl) {
  // Кнопка меню (слева от поля ввода) открывает Mini App.
  await bot.api.setChatMenuButton({
    menu_button: {
      type: 'web_app',
      text: '🎬 Фильмы',
      web_app: { url: `${config.publicUrl}/app` },
    },
  });
}

const server = createServer(createHttpHandler({ config, db, bot }));

server.listen(config.port, '0.0.0.0', async () => {
  console.log(`[http] слушаю 0.0.0.0:${config.port}`);

  if (config.mode === 'webhook') {
    const url = `${config.publicUrl}${config.webhookPath}`;
    await bot.api.setWebhook(url, {
      secret_token: config.webhookSecret,
      allowed_updates: ['message', 'callback_query'],
      drop_pending_updates: false,
    });
    console.log(`[bot] webhook установлен: ${url}`);
  } else {
    await bot.api.deleteWebhook({ drop_pending_updates: false });
    console.log('[bot] режим polling');
    bot.start({ allowed_updates: ['message', 'callback_query'] });
  }
});

const shutdown = (signal) => {
  console.log(`[app] получен ${signal}, останавливаюсь`);
  server.close(() => {
    db.close();
    process.exit(0);
  });
  setTimeout(() => process.exit(0), 5000).unref();
};
process.on('SIGINT', () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

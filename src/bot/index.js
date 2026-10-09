import { Bot } from 'grammy';
import { registerAdminHandlers } from './admin.js';
import { registerUserHandlers } from './user.js';

/**
 * Создаёт экземпляр бота и подключает все хендлеры.
 * Важно: админские хендлеры регистрируются первыми, чтобы мастер
 * перехватывал сообщения раньше пользовательского сценария.
 * @param {{config: object, db: object}} deps
 */
export function createBot({ config, db }) {
  const bot = new Bot(config.botToken);

  registerAdminHandlers(bot, { config, db });
  registerUserHandlers(bot, { config, db });

  bot.catch((err) => {
    console.error('[bot] ошибка обработки обновления:', err.error ?? err);
  });

  return bot;
}

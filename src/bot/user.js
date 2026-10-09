import { catalogGrid, sendFilmCard } from './cards.js';

const WELCOME = '🍿 <b>Каталог фильмов</b>\nВыберите фильм кнопкой ниже.';
const EMPTY = 'Пока в каталоге пусто. Загляните позже!';

/**
 * Пользовательские хендлеры: /start, каталог, открытие фильма.
 * @param {import('grammy').Bot} bot
 * @param {{config: object, db: object}} deps
 */
export function registerUserHandlers(bot, { config, db }) {
  /** Отправляет каталог (сетку кнопок) и, если настроен, кнопку Mini App. */
  async function showCatalog(ctx) {
    const films = db.listActiveFilms();
    const text = films.length ? WELCOME : EMPTY;
    const kb = films.length ? catalogGrid(films) : undefined;

    if (config.publicUrl && kb) {
      kb.row().webApp('📱 Открыть каталог', `${config.publicUrl}/app`);
    }
    await ctx.reply(text, { parse_mode: 'HTML', reply_markup: kb });
  }

  bot.command('start', async (ctx) => {
    const payload = ctx.match?.trim() ?? '';
    const deepLink = /^film_(\d+)$/.exec(payload);

    if (deepLink) {
      const film = db.getFilm(Number(deepLink[1]));
      if (film && film.is_active) {
        db.incrementViews(film.id);
        return sendFilmCard(ctx.api, ctx.chat.id, film);
      }
      await ctx.reply('Этот фильм больше недоступен.');
    }
    return showCatalog(ctx);
  });

  bot.command(['catalog', 'menu'], showCatalog);

  bot.callbackQuery(/^f:(\d+)$/, async (ctx) => {
    const film = db.getFilm(Number(ctx.match[1]));
    if (!film || !film.is_active) {
      return ctx.answerCallbackQuery({ text: 'Фильм недоступен', show_alert: true });
    }
    db.incrementViews(film.id);
    await ctx.answerCallbackQuery();
    await sendFilmCard(ctx.api, ctx.chat.id, film);
  });
}

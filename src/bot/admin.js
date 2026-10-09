import { InlineKeyboard } from 'grammy';
import { esc, sendFilmCard } from './cards.js';

/** Состояние мастера добавления фильма по Telegram ID админа. */
const wizards = new Map();

const URL_RE = /^https?:\/\/\S+$/i;

const HELP = `⚙️ <b>Админ-панель</b>
/add — добавить фильм
/list — список фильмов и управление
/cancel — отменить текущий мастер`;

/**
 * Хендлеры администратора: мастер добавления, список и управление фильмами.
 * Все команды работают только для ID из ADMIN_IDS; остальным бот молчит.
 * @param {import('grammy').Bot} bot
 * @param {{config: object, db: object}} deps
 */
export function registerAdminHandlers(bot, { config, db }) {
  const isAdmin = (ctx) => Boolean(ctx.from && config.adminIds.has(ctx.from.id));

  bot.command('admin', async (ctx, next) => {
    if (!isAdmin(ctx)) return next();
    await ctx.reply(HELP, { parse_mode: 'HTML' });
  });

  bot.command('add', async (ctx, next) => {
    if (!isAdmin(ctx)) return next();
    wizards.set(ctx.from.id, { step: 'media', film: {} });
    await ctx.reply(
      '🎞 Шаг 1/4. Пришлите <b>видео</b> или <b>баннер</b> (фото) фильма.\n/cancel — отмена',
      { parse_mode: 'HTML' },
    );
  });

  bot.command('cancel', async (ctx, next) => {
    if (!isAdmin(ctx)) return next();
    const had = wizards.delete(ctx.from.id);
    await ctx.reply(had ? '❌ Добавление отменено.' : 'Нечего отменять.');
  });

  bot.command('list', async (ctx, next) => {
    if (!isAdmin(ctx)) return next();
    const films = db.listAllFilms();
    if (!films.length) return ctx.reply('Фильмов пока нет. Используйте /add.');

    const lines = films.map((f) => {
      const mark = f.is_active ? '🟢' : '⚪️';
      const type = f.kind === 'video' ? '▶️видео' : '🔗ссылка';
      return `${mark} <b>#${f.id}</b> ${esc(f.title)} — ${type}, 👁 ${f.views}`;
    });
    const kb = new InlineKeyboard();
    for (const f of films.slice(0, 20)) {
      kb.text(`${f.is_active ? '⏸' : '▶️'} #${f.id}`, `adm:tog:${f.id}`)
        .text(`🗑 #${f.id}`, `adm:del:${f.id}`)
        .row();
    }
    await ctx.reply(lines.join('\n').slice(0, 4000), { parse_mode: 'HTML', reply_markup: kb });
  });

  bot.callbackQuery(/^adm:(tog|del):(\d+)$/, async (ctx) => {
    if (!isAdmin(ctx)) return ctx.answerCallbackQuery();
    const [, action, rawId] = ctx.match;
    const id = Number(rawId);
    if (action === 'del') {
      const ok = db.deleteFilm(id);
      await ctx.answerCallbackQuery({ text: ok ? `Фильм #${id} удалён` : 'Не найдено' });
    } else {
      const film = db.getFilm(id);
      if (!film) return ctx.answerCallbackQuery({ text: 'Не найдено', show_alert: true });
      db.setActive(id, !film.is_active);
      await ctx.answerCallbackQuery({ text: film.is_active ? 'Скрыт из каталога' : 'Показан в каталоге' });
    }
  });

  // ---- мастер добавления: сообщения ----
  bot.on('message', async (ctx, next) => {
    if (!isAdmin(ctx)) return next();
    const state = wizards.get(ctx.from.id);
    if (!state) return next();

    const msg = ctx.message;

    switch (state.step) {
      case 'media': {
        if (msg.video) {
          state.film.video_file_id = msg.video.file_id;
        } else if (msg.photo) {
          state.film.photo_file_id = msg.photo.at(-1).file_id; // самое большое разрешение
        } else {
          return ctx.reply('Нужно видео или фото-баннер. Пришлите файл.');
        }
        state.step = 'title';
        return ctx.reply('🎞 Шаг 2/4. Напишите <b>название</b> фильма.', { parse_mode: 'HTML' });
      }

      case 'title': {
        if (!msg.text || msg.text.startsWith('/')) return ctx.reply('Напишите название текстом.');
        state.film.title = msg.text.trim().slice(0, 100);
        state.step = 'kind';
        const kb = new InlineKeyboard()
          .text('🔗 Ссылка (переход)', 'wz:kind:link').row()
          .text('▶️ Видео в боте', 'wz:kind:video');
        return ctx.reply(
          '🎞 Шаг 3/4. Как открывать фильм?\n' +
            '🔗 <b>Ссылка</b> — кнопка ведёт на ваш переходник.\n' +
            '▶️ <b>Видео в боте</b> — фильм проигрывается прямо в Telegram.',
          { parse_mode: 'HTML', reply_markup: kb },
        );
      }

      case 'url': {
        const url = msg.text?.trim();
        if (!url || !URL_RE.test(url)) return ctx.reply('Нужна ссылка, начинающаяся с https:// или http://');
        state.film.url = url;
        return askConfirm(ctx, state);
      }

      case 'video': {
        if (!msg.video) return ctx.reply('Пришлите именно видеофайл.');
        state.film.video_file_id = msg.video.file_id;
        return askConfirm(ctx, state);
      }

      default:
        return next();
    }
  });

  // ---- мастер добавления: кнопки ----
  bot.callbackQuery(/^wz:(kind:(link|video)|save|cancel)$/, async (ctx) => {
    if (!isAdmin(ctx)) return ctx.answerCallbackQuery();
    const state = wizards.get(ctx.from.id);
    if (!state) return ctx.answerCallbackQuery({ text: 'Мастер не активен. Нажмите /add', show_alert: true });

    const action = ctx.match[1];
    await ctx.answerCallbackQuery();

    if (action === 'cancel') {
      wizards.delete(ctx.from.id);
      return ctx.reply('❌ Добавление отменено.');
    }

    if (action === 'save') {
      const id = db.createFilm(state.film);
      wizards.delete(ctx.from.id);
      return ctx.reply(`✅ Фильм #${id} сохранён и доступен в каталоге (/start).`);
    }

    // выбор типа
    const kind = ctx.match[2];
    state.film.kind = kind;
    if (kind === 'link') {
      state.step = 'url';
      return ctx.reply('🎞 Шаг 4/4. Пришлите ссылку-переходник (https://...).');
    }
    if (state.film.video_file_id) return askConfirm(ctx, state);
    state.step = 'video';
    return ctx.reply('🎞 Шаг 4/4. Пришлите видеофайл фильма (его будет видно в боте).');
  });

  /** Показывает превью и просит подтвердить сохранение. */
  async function askConfirm(ctx, state) {
    state.step = 'confirm';
    const kb = new InlineKeyboard().text('✅ Сохранить', 'wz:save').text('❌ Отмена', 'wz:cancel');
    await ctx.reply('👀 Превью карточки:');
    await sendFilmCard(ctx.api, ctx.chat.id, state.film);
    return ctx.reply('Сохранить фильм?', { reply_markup: kb });
  }
}

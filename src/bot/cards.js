import { InlineKeyboard } from 'grammy';

/** Экранирование для parse_mode=HTML. */
export function esc(text) {
  return String(text).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

/**
 * Отправляет карточку фильма в указанный чат.
 *  - kind=link:  баннер (или видео-обложка) + кнопка-ссылка «Смотреть» (переход).
 *  - kind=video: видео отправляется прямо в Telegram, его можно смотреть в боте.
 * @param {import('grammy').Api} api
 * @param {number} chatId
 * @param {object} film строка из БД
 */
export function sendFilmCard(api, chatId, film) {
  const caption = `🎬 <b>${esc(film.title)}</b>`;
  const common = { caption, parse_mode: 'HTML' };

  if (film.kind === 'video') {
    return api.sendVideo(chatId, film.video_file_id, {
      ...common,
      supports_streaming: true,
    });
  }

  const keyboard = new InlineKeyboard().url('▶️ Смотреть', film.url);
  if (film.photo_file_id) {
    return api.sendPhoto(chatId, film.photo_file_id, { ...common, reply_markup: keyboard });
  }
  if (film.video_file_id) {
    return api.sendVideo(chatId, film.video_file_id, { ...common, reply_markup: keyboard });
  }
  return api.sendMessage(chatId, caption, { parse_mode: 'HTML', reply_markup: keyboard });
}

/**
 * Сетка кнопок каталога: по 2 в ряд, без лишнего текста на кнопках.
 * @param {Array<{id: number, title: string}>} films
 */
export function catalogGrid(films) {
  const kb = new InlineKeyboard();
  films.forEach((film, index) => {
    const label = film.title.length > 28 ? `${film.title.slice(0, 27)}…` : film.title;
    kb.text(`🎬 ${label}`, `f:${film.id}`);
    if (index % 2 === 1 && index !== films.length - 1) kb.row();
  });
  return kb;
}

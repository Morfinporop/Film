import { DatabaseSync } from 'node:sqlite';
import { mkdirSync } from 'node:fs';
import { dirname } from 'node:path';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS films (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  title          TEXT    NOT NULL,
  kind           TEXT    NOT NULL CHECK (kind IN ('link', 'video')),
  url            TEXT,
  video_file_id  TEXT,
  photo_file_id  TEXT,
  is_active      INTEGER NOT NULL DEFAULT 1,
  views          INTEGER NOT NULL DEFAULT 0,
  created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
  CHECK (kind <> 'link'  OR url IS NOT NULL),
  CHECK (kind <> 'video' OR video_file_id IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_films_active ON films (is_active, id);
`;

/**
 * Открывает SQLite-базу и возвращает репозиторий фильмов.
 * Путь берётся из конфигурации (DB_PATH), по умолчанию /app/data/film.db на bothost.
 * @param {string} path путь к файлу или ':memory:'
 */
export function openDb(path) {
  if (path !== ':memory:') mkdirSync(dirname(path), { recursive: true });

  // node:sqlite — встроенный модуль Node 22.13+, без нативной сборки.
  const db = new DatabaseSync(path);
  db.exec('PRAGMA journal_mode = WAL');
  db.exec('PRAGMA foreign_keys = ON');
  db.exec(SCHEMA);

  const stmt = {
    insert: db.prepare(`
      INSERT INTO films (title, kind, url, video_file_id, photo_file_id)
      VALUES (@title, @kind, @url, @video_file_id, @photo_file_id)`),
    get: db.prepare('SELECT * FROM films WHERE id = ?'),
    listActive: db.prepare('SELECT * FROM films WHERE is_active = 1 ORDER BY id DESC LIMIT 200'),
    listAll: db.prepare('SELECT * FROM films ORDER BY id DESC LIMIT 200'),
    setActive: db.prepare('UPDATE films SET is_active = ? WHERE id = ?'),
    remove: db.prepare('DELETE FROM films WHERE id = ?'),
    addView: db.prepare('UPDATE films SET views = views + 1 WHERE id = ?'),
  };

  return {
    /** @param {{title: string, kind: 'link'|'video', url?: string|null, video_file_id?: string|null, photo_file_id?: string|null}} film */
    createFilm(film) {
      const info = stmt.insert.run({
        title: film.title,
        kind: film.kind,
        url: film.url ?? null,
        video_file_id: film.video_file_id ?? null,
        photo_file_id: film.photo_file_id ?? null,
      });
      return Number(info.lastInsertRowid);
    },
    getFilm: (id) => stmt.get.get(id) ?? null,
    listActiveFilms: () => stmt.listActive.all(),
    listAllFilms: () => stmt.listAll.all(),
    /** @returns {boolean} найден ли фильм */
    setActive(id, active) {
      return stmt.setActive.run(active ? 1 : 0, id).changes > 0;
    },
    deleteFilm: (id) => stmt.remove.run(id).changes > 0,
    incrementViews: (id) => stmt.addView.run(id),
    close: () => db.close(),
  };
}

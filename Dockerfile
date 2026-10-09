# Образ для bothost (путь приложения /app, порт 3000, БД /app/data/film.db).
# Нативных зависимостей нет: SQLite — встроенный node:sqlite.
FROM node:22-bookworm-slim

ENV NODE_ENV=production \
    PORT=3000 \
    DB_PATH=/app/data/film.db

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci --omit=dev && npm cache clean --force

COPY src ./src

# Папка данных принадлежит непривилегированному пользователю node
RUN mkdir -p /app/data && chown -R node:node /app/data
USER node

EXPOSE 3000
VOLUME ["/app/data"]

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||3000)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["npm", "start"]

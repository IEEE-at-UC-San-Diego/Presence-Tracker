# syntax=docker/dockerfile:1
FROM oven/bun:1.4.4 AS runner

WORKDIR /app

# Install dependencies (even if none are currently declared) so Bun can manage them
COPY package.json bun.lock ./
RUN bun install --frozen-lockfile --production

# Copy frontend sources
COPY frontend ./frontend

WORKDIR /app/frontend

ENV NODE_ENV=production
ENV PORT=3132

EXPOSE 3132

CMD ["bun", "run", "server.js"]

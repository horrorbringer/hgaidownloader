FROM node:22-bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        ffmpeg \
        tini \
    && rm -rf /var/lib/apt/lists/*

# yt-dlp standalone binary: no Python runtime needed at build or run time.
RUN curl -fsSL -o /usr/local/bin/yt-dlp \
        https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp_linux \
    && chmod +x /usr/local/bin/yt-dlp \
    && /usr/local/bin/yt-dlp --version

WORKDIR /app

COPY package.json ./
RUN npm install

# Installs Chromium plus the shared libraries it needs.
RUN npx playwright install --with-deps chromium

COPY tsconfig.json ./
COPY src ./src
RUN npm run build && npm prune --omit=dev

ENV PORT=8080
EXPOSE 8080

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["node", "dist/server.js"]

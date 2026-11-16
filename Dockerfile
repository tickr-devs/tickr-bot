# Tickr on a Linux server: one image for the bot, preflight, watchdog and local API.
# See docs/deploy-server.md. No secrets, tokens, env file or data are copied in (.dockerignore
# is an allowlist); the env file and config/bot.toml are mounted read-only at run time and the
# data folder is a volume.
FROM python:3.12-slim-bookworm

# tzdata: the calendar converts to America/New_York with zoneinfo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# The container user's uid must be able to read the env file (chmod 600 on the host): build
# with --build-arg TICKR_UID=$(id -u) --build-arg TICKR_GID=$(id -g) on the server.
ARG TICKR_UID=1000
ARG TICKR_GID=1000
RUN groupadd --gid "$TICKR_GID" tickr \
    && useradd --uid "$TICKR_UID" --gid "$TICKR_GID" --no-create-home \
       --home-dir /app/data --shell /usr/sbin/nologin tickr

# The code version every run and trade records (src/tickr/version.py): the image has no .git,
# so the commit is baked in at build time with
# --build-arg TICKR_CODE_VERSION=$(git rev-parse HEAD) (docs/deploy-server.md). Without it the
# bot and preflight refuse to start.
ARG TICKR_CODE_VERSION=""
ENV TICKR_CODE_VERSION=$TICKR_CODE_VERSION

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# The repo layout is kept: config.load() takes the repo root from the config path
# (/app/config/bot.toml -> /app); strategies/ and config/calendar/ are read from there.
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install -e ".[webull]"
COPY config ./config
COPY strategies ./strategies
COPY --chmod=0755 ops/docker/entrypoint.sh /usr/local/bin/tickr-entrypoint

# Only the data folder is writable by the bot user; code and config stay root-owned.
# The working directory is the data folder, so a file a library writes to its current
# directory lands in the volume, never in the image (on the Mac it would land in the repo).
RUN mkdir -p /app/data && chown tickr:tickr /app/data
VOLUME ["/app/data"]
WORKDIR /app/data
USER tickr

ENTRYPOINT ["/usr/local/bin/tickr-entrypoint"]
CMD ["python", "-m", "tickr.api", "--config", "/app/config/bot.toml"]

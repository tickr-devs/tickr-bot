# Aperture on a Linux server: one image for the bot, preflight, watchdog and local API.
# See docs/deploy-server.md. No secrets, tokens, env file or data are copied in (.dockerignore
# is an allowlist); the env file and config/bot.toml are mounted read-only at run time and the
# data folder is a volume.
FROM python:3.12-slim-bookworm

# tzdata: the calendar converts to America/New_York with zoneinfo.
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

# The container user's uid must be able to read the env file (chmod 600 on the host): build
# with --build-arg APERTURE_UID=$(id -u) --build-arg APERTURE_GID=$(id -g) on the server.
ARG APERTURE_UID=1000
ARG APERTURE_GID=1000
RUN groupadd --gid "$APERTURE_GID" aperture \
    && useradd --uid "$APERTURE_UID" --gid "$APERTURE_GID" --no-create-home \
       --home-dir /app/data --shell /usr/sbin/nologin aperture

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
COPY --chmod=0755 ops/docker/entrypoint.sh /usr/local/bin/aperture-entrypoint

# Only the data folder is writable by the bot user; code and config stay root-owned.
# The working directory is the data folder, so a file a library writes to its current
# directory lands in the volume, never in the image (on the Mac it would land in the repo).
RUN mkdir -p /app/data && chown aperture:aperture /app/data
VOLUME ["/app/data"]
WORKDIR /app/data
USER aperture

ENTRYPOINT ["/usr/local/bin/aperture-entrypoint"]
CMD ["python", "-m", "aperture.api", "--config", "/app/config/bot.toml"]

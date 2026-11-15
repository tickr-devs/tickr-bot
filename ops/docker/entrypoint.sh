#!/bin/sh
# Tickr container entrypoint. Loads the owner's env file the same way the launchd jobs do
# (`set -a; . file`), so neither Tickr code nor the image holds the values: the file is
# mounted read-only at run time and only its path is in the container's environment.
# TICKR_ENV_FILE unset: nothing is loaded (the API needs no credentials).
# TICKR_ENV_FILE set but not readable: refuse to start, never run without it.
set -eu
if [ -n "${TICKR_ENV_FILE:-}" ]; then
  if [ ! -r "$TICKR_ENV_FILE" ]; then
    echo "entrypoint: env file $TICKR_ENV_FILE is not readable by uid $(id -u)" >&2
    exit 2
  fi
  set -a
  . "$TICKR_ENV_FILE"
  set +a
fi
exec "$@"

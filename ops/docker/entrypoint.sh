#!/bin/sh
# Aperture container entrypoint. Loads the owner's env file the same way the launchd jobs do
# (`set -a; . file`), so neither Aperture code nor the image holds the values: the file is
# mounted read-only at run time and only its path is in the container's environment.
# APERTURE_ENV_FILE unset: nothing is loaded (the API needs no credentials).
# APERTURE_ENV_FILE set but not readable: refuse to start, never run without it.
set -eu
if [ -n "${APERTURE_ENV_FILE:-}" ]; then
  if [ ! -r "$APERTURE_ENV_FILE" ]; then
    echo "entrypoint: env file $APERTURE_ENV_FILE is not readable by uid $(id -u)" >&2
    exit 2
  fi
  set -a
  . "$APERTURE_ENV_FILE"
  set +a
fi
exec "$@"

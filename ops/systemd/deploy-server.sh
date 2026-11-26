#!/bin/sh
# Install the Tickr systemd units on a Linux server, and only while the bot is flat.
#
#   deploy-server.sh COMMIT [ETC_DIR [DATA_DIR]]
#
#   COMMIT    the release: a full commit of main (40 lowercase hex characters), the tag of the
#             release image <registry>/tickr-bot:<COMMIT> (.github/workflows/server-image.yml)
#   ETC_DIR   absolute folder of the host files copied to the server (no clone is needed or
#             used): bot.toml and compose.yaml (ops/docker/compose.yaml of COMMIT); ports.env
#             and limits.env are installed here (default /srv/tickr/etc)
#   DATA_DIR  absolute host data folder mounted as the containers' data folder
#             (default /srv/tickr/data)
#
# Every Python step runs inside the release image (`docker run --rm --pull never`), never on the
# host: the server has no clone, no virtualenv and no GitHub credential (docs/deploy-server.md,
# "Server files").
#
# Order: (a) refuse a COMMIT that is not 40 lowercase hex characters (bad-commit); (b) read only
# TICKR_IMAGE_REPOSITORY from /srv/tickr/etc/registry.env (written by the server's user-data,
# also read by tickr-image-pull; never sourced here, no other line is used or printed) and
# refuse when the file or the variable is missing (registry-unknown); (c) refuse unless the
# image $TICKR_IMAGE_REPOSITORY:COMMIT is already on this host (`docker image inspect`;
# image-missing): this script never pulls, the host helper `tickr-image-pull COMMIT` does; the
# registry address (it holds the AWS account id) is never printed; (d) refuse unless ETC_DIR and
# DATA_DIR are absolute (path-not-absolute) and ETC_DIR holds bot.toml and compose.yaml
# (config-missing, compose-missing); (0) refuse unless DATA_DIR is an existing folder (never
# created here) and a mount point (the data disk is in fstab with nofail: an unmounted disk
# leaves an empty folder on the root disk, and a bot started there has no state; the units also
# carry RequiresMountsFor=DATA_DIR); (1) refuse unless the data folder's owner uid:gid is the
# release image's runtime user (`id -u` and `id -g` in a throwaway container of the image,
# entrypoint replaced, no network, no mount: no job, no broker, no env file read), so the
# containers can write the bind-mounted data folder; (1b) refuse unless ETC_DIR/compose.yaml is
# byte for byte the copy the release image carries at /app/ops/docker/compose.yaml
# (compose-mismatch: the host copy must come from COMMIT); (2) render the units, ports.env and
# limits.env with `render-systemd --image --compose` in a throwaway container of the image (no
# network; bot.toml and compose.yaml mounted read-only, compose.yaml at its own host path; only
# a scratch folder, owned by the image's user, is writable) and refuse unless the rendered
# limits.env names the release image; (3) prove flat with a fresh read-only broker read and the
# database: `require-flat` runs in a throwaway container of the image with the image's own
# entrypoint (it sources the env file, mounted read-only, as for a real job), host networking
# (the broker read) and the data folder mounted read-write (the database is read read-only, but
# the broker's token folder lives there and SQLite's WAL needs its shared-memory file), exactly
# the mounts the jobs get; it never sends an order and any doubt refuses, nothing installed;
# (4) refuse on a restart request left in the control folder (it would restart the bot the
# moment the path unit starts), then create only the control folder under the verified mount
# (owned by the image's uid:gid, mode 700); (5) install the files (all or nothing) and reload
# systemd; (6) enable the timers, the restart request watch and the API. The bot is never
# started or stopped here: a running job keeps the image it started with, the next scheduled
# start uses the new units.
#
# Overrides (names only): TICKR_UNITS_DIR (default /etc/systemd/system), TICKR_SYSTEMCTL
# (default systemctl), TICKR_DOCKER (default docker), TICKR_ENV_FILE_HOST (default
# /run/tickr/env), TICKR_SERVICE_USER (default tickr: the units' User=), TICKR_MOUNTPOINT (default mountpoint),
# TICKR_REGISTRY_FILE (default /srv/tickr/etc/registry.env). This script never reads the env file,
# prints the environment or traces the shell (docs/deploy-server.md).
set -eu

if [ "$#" -lt 1 ] || [ "$#" -gt 3 ]; then
  echo "usage: $0 COMMIT [ETC_DIR [DATA_DIR]]" >&2
  exit 2
fi
commit=$1
etc=${2:-/srv/tickr/etc}
data=${3:-/srv/tickr/data}
user=${TICKR_SERVICE_USER:-tickr}
units_dir=${TICKR_UNITS_DIR:-/etc/systemd/system}
systemctl=${TICKR_SYSTEMCTL:-systemctl}
docker=${TICKR_DOCKER:-docker}
mountpoint=${TICKR_MOUNTPOINT:-mountpoint}
config="$etc/bot.toml"
compose="$etc/compose.yaml"
env_file=${TICKR_ENV_FILE_HOST:-/run/tickr/env}

if ! printf '%s\n' "$commit" | grep -Eqx '[0-9a-f]{40}'; then
  echo "refused: bad-commit: the release commit must be 40 lowercase hex characters (the image tag); nothing was changed" >&2
  exit 1
fi
registry_file=${TICKR_REGISTRY_FILE:-/srv/tickr/etc/registry.env}
if [ ! -f "$registry_file" ] || ! registry=$(sed -n 's/^TICKR_IMAGE_REPOSITORY=//p' "$registry_file"); then
  echo "refused: registry-unknown: $registry_file is missing or unreadable; nothing was changed" >&2
  exit 1
fi
if [ -z "$registry" ] || [ "$(printf '%s\n' "$registry" | wc -l)" -ne 1 ] \
    || ! printf '%s\n' "$registry" | grep -Eqx '[a-z0-9][a-z0-9.-]*(:[0-9]+)?/[a-z0-9][a-z0-9._/-]*'; then
  echo "refused: registry-unknown: $registry_file has no single unquoted TICKR_IMAGE_REPOSITORY=<registry>/<repository> line; nothing was changed" >&2
  exit 1
fi
image="$registry:$commit"  # never printed: the registry address holds the AWS account id
if ! "$docker" image inspect "$image" >/dev/null 2>&1; then
  echo "refused: image-missing: the release image of $commit (TICKR_IMAGE_REPOSITORY in $registry_file) is not on this host (pull it first: tickr-image-pull $commit); nothing was changed" >&2
  exit 1
fi

case "$etc" in /*) ;; *)
  echo "refused: path-not-absolute: ETC_DIR $etc must be an absolute path; nothing was changed" >&2
  exit 1 ;;
esac
case "$data" in /*) ;; *)
  echo "refused: path-not-absolute: DATA_DIR $data must be an absolute path; nothing was changed" >&2
  exit 1 ;;
esac
if [ ! -f "$config" ]; then
  echo "refused: config-missing: $config is not a file (copied to the server, docs/deploy-server.md); nothing was changed" >&2
  exit 1
fi
if [ ! -f "$compose" ]; then
  echo "refused: compose-missing: $compose is not a file (ops/docker/compose.yaml of $commit, copied to the server); nothing was changed" >&2
  exit 1
fi

if [ ! -d "$data" ] || [ -L "$data" ]; then
  echo "refused: data-missing: $data is not an existing folder (it is never created here); nothing was changed" >&2
  exit 1
fi
if ! "$mountpoint" -q "$data"; then
  echo "refused: data-not-mounted: $data is not a mount point (is the data disk mounted?); nothing was changed" >&2
  exit 1
fi

image_id() { # id -u or id -g of the image's runtime user, in a throwaway container
  "$docker" run --rm --pull never --network none --entrypoint id "$image" "$1"
}
if ! image_uid=$(image_id -u) || ! image_gid=$(image_id -g); then
  echo "refused: image-user-unknown: cannot read the image's runtime uid and gid; nothing was changed" >&2
  exit 1
fi
if ! data_owner=$(stat -c %u:%g "$data"); then
  echo "refused: data-owner-unknown: cannot read the owner of $data; nothing was changed" >&2
  exit 1
fi
if [ "$data_owner" != "$image_uid:$image_gid" ]; then
  echo "refused: data-owner-mismatch: $data is owned by $data_owner but the image runs as $image_uid:$image_gid (the release image runs as the server's tickr account, ops/aws/bot service_uid); nothing was changed" >&2
  exit 1
fi

# compose.yaml is a host copy (the server has no clone): it must be the one the release image
# was built with, which the image carries at /app/ops/docker/compose.yaml. Any difference, or an
# image that cannot show its copy, refuses.
image_compose="$(mktemp)"
if ! "$docker" run --rm --pull never --network none --entrypoint cat "$image" \
    /app/ops/docker/compose.yaml > "$image_compose" || ! cmp -s "$image_compose" "$compose"; then
  rm -f "$image_compose"
  echo "refused: compose-mismatch: $compose is not the compose.yaml of the release image of $commit (copy ops/docker/compose.yaml of $commit, docs/deploy-server.md); nothing was changed" >&2
  exit 1
fi
rm -f "$image_compose"

# render-systemd in the release image: the templates come from the image (/app/ops/systemd),
# bot.toml and compose.yaml from ETC_DIR (read-only), the output goes to a scratch folder that
# only the image's user may write.
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
if ! chown "$image_uid:$image_gid" "$scratch"; then
  echo "refused: cannot give the scratch folder to the image's user; nothing was changed" >&2
  exit 1
fi
if ! "$docker" run --rm --pull never --network none --entrypoint python \
    --mount "type=bind,source=$config,target=/app/config/bot.toml,readonly" \
    --mount "type=bind,source=$compose,target=$compose,readonly" \
    --mount "type=bind,source=$scratch,target=/out" \
    "$image" -m tickr.runtime --config /app/config/bot.toml render-systemd \
    --compose "$compose" --etc "$etc" --env-file "$env_file" --data "$data" \
    --user "$user" --image "$image" --out /out >/dev/null; then
  echo "refused: render-systemd failed in the release image; run it by hand to read the cause (docs/deploy-server.md); nothing was changed" >&2
  exit 1
fi
if ! grep -qxF "TICKR_IMAGE=$image" "$scratch/limits.env"; then
  echo "refused: the rendered limits.env does not name the release image of $commit; nothing was changed" >&2
  exit 1
fi

# require-flat in the release image, with the mounts and network of the jobs (compose.yaml).
if ! "$docker" run --rm --pull never --network host -e TICKR_ENV_FILE=/run/tickr/env \
    --mount "type=bind,source=$config,target=/app/config/bot.toml,readonly" \
    --mount "type=bind,source=$env_file,target=/run/tickr/env,readonly" \
    --mount "type=bind,source=$data,target=/app/data" \
    "$image" python -m tickr.runtime --config /app/config/bot.toml require-flat; then
  echo "refused: the bot is not proven flat (see the require-flat line above); nothing was changed" >&2
  exit 1
fi

# The restart request watch (tickr-bot-restart.path) fires as soon as it starts if a request is
# already there: never install over one. The folder of the request is created here only.
request=$(sed -n 's/^PathExists=//p' "$scratch/tickr-bot-restart.path")
if [ -z "$request" ]; then
  echo "refused: the rendered tickr-bot-restart.path names no request file; nothing was installed" >&2
  exit 1
fi
if [ -e "$request" ]; then
  echo "refused: a bot restart request is waiting at $request; read it, remove it, deploy again" >&2
  exit 1
fi
control=$(dirname "$request")
if [ "$(dirname "$control")" != "$data" ]; then
  echo "refused: the request folder $control is not directly under $data; nothing was installed" >&2
  exit 1
fi
# Only the control folder is created, under the verified mount, owned by the image's user.
install -d -m 700 -o "$image_uid" -g "$image_gid" "$control"

# Install all or nothing: stage every file as a hidden temp file inside its target folder (a
# copy that fails removes the staged files and installs nothing), then rename each into place
# (same folder, so each rename is atomic). Only then is systemd told to reload.
staged=""
cleanup_staged() { for f in $staged; do rm -f "$f"; done; }
stage() { # SOURCE TARGET_DIR
  tmp="$2/.tickr-stage.$$.$(basename "$1")"
  staged="$staged $tmp"
  cp "$1" "$tmp" || { cleanup_staged; echo "refused: copy to $2 failed; nothing was installed" >&2; exit 1; }
}
for f in "$scratch"/tickr-*.service "$scratch"/tickr-*.timer "$scratch"/tickr-*.path; do
  stage "$f" "$units_dir"
done
for f in "$scratch"/ports.env "$scratch"/limits.env; do stage "$f" "$etc"; done
for tmp in $staged; do
  target=$(dirname "$tmp")/$(basename "$tmp" | sed "s/^\.tickr-stage\.$$\.//")
  mv -f "$tmp" "$target"
done
"$systemctl" daemon-reload
"$systemctl" enable tickr-preflight.timer tickr-bot.timer tickr-watchdog.timer tickr-nightly-fetch.timer \
  tickr-bot-restart.path tickr-api.service
"$systemctl" start tickr-preflight.timer tickr-bot.timer tickr-watchdog.timer tickr-nightly-fetch.timer \
  tickr-bot-restart.path
"$systemctl" restart tickr-api.service
echo "deployed: release image of $commit; units installed in $units_dir, ports.env and limits.env in $etc"

# Running Tickr on a Linux server (Docker)

Today Tickr runs on the owner's Mac under launchd (`docs/operations.md`). This page says what
changes on an always-on Linux server, gives a Docker setup for it, and lists what the owner must
decide first.

**Alerts.** The alert channel is Telegram, which works on any host (the iMessage and Mac
notification channels were removed, owner 2026-11-25). Set `TICKR_TELEGRAM_BOT_TOKEN` and
`TICKR_TELEGRAM_CHAT_ID` in the env file; without them preflight refuses the start with
`alert-channel-missing` and the bot job never starts the bot. App push (APNs) comes later.

## Audit: what is Mac-only

| Area | On the Mac | On Linux / in Docker | Done here |
|---|---|---|---|
| Alert channels | `runtime/alerts.py`: Telegram (`TelegramNotifier`, one HTTPS POST) and the alerts feed file; the same on every OS. | No change: nothing depends on the platform. | Preflight check `alert-channel` refuses with `alert-channel-missing` (blocks the start, exit 1) when Telegram is not set up; `alert-telegram` checks that both variables are set. The watchdog journals `watchdog-alert-channel-refused` with that cause, still runs and journals, and never exits 0 (1 instead of 0; 3 on a closed day), so its unit shows failed. Solved by Telegram. On the AWS server both names go into the `tickr-bot/env` secret with the Webull variables; `tickr-secrets.service` writes every name in it to the env file `/run/tickr/env`, which the bot, preflight and watchdog containers source (`ops/aws/bot/README.md`, "Fill the secret"). |
| caffeinate | `/usr/bin/caffeinate` in the bot and watchdog plists' shell line. | Not needed: a server does not sleep. Not called anywhere in Python. | Nothing. |
| launchd | Five plists rendered by `render-launchd` from `[runtime]` times. | No launchd. `render-launchd` still works but its output is useless. | `ops/docker/compose.yaml` plus host systemd timers (below). `render-systemd` writes the units and timers from `[runtime]` (below). |
| Paths | Logs in `~/Library/Logs/Tickr`; env file `~/.config/tickr/env`; repo root = parent of `config/`. | Logs are the container logs (`docker compose logs`), rotated by the json-file driver. Env file: any host path, mounted read-only at `/run/tickr/env`. Repo root is `/app` in the image. Preflight's hint still names `~/.config/tickr/env` (text only). | Compose file. |
| Watchdog schedule | Starts at load and at `[runtime] watchdog_start_local`, **Mac local time**, then runs at every hour (launchd `KeepAlive`); its broker checks come from the calendar (New York time). | Same command. The start time is the host timer's; give the timer the `America/New_York` zone so daylight-saving changes cannot move it. | Timer example below. |
| Clock | macOS syncs time automatically. Code uses UTC and converts with `zoneinfo` (`America/New_York`); `[risk] max_clock_skew_seconds` = 1 s against quote times. | Containers use the host kernel clock: the host must run NTP (chrony or systemd-timesyncd). `zoneinfo` needs tzdata. | `tzdata` installed in the image. NTP is an owner decision. |
| SQLite | `data/` in the repo; `fullfsync` on macOS (`store.py`). | `synchronous=FULL` and `fsync` are durable on a local Linux disk. WAL and the bot's `flock` lock need a local filesystem: never NFS/SMB. | Data folder is a host folder (`TICKR_DATA_HOST`, the data disk at `/srv/tickr/data`) bind-mounted at `/app/data` in every job, so host units see the same files. |
| Lock and watchdog | The watchdog checks the pid in the bot's lock file. | Separate containers have separate pid namespaces, so the watchdog would always see "no live pid" and alert `bot-down-with-open-trade`. | `pid: host` on the bot and watchdog services (see "Before real money"). |
| API binding | `[api] host` = 127.0.0.1, enforced by the API (refuses a non-loopback host). | With Docker's bridge network a 127.0.0.1 bind inside the container is unreachable from the host, and publishing a port would need a 0.0.0.0 bind, which the API refuses. | `network_mode: host`: the API is on the server's own loopback only; Docker publishes no ports. Phone access works as on the Mac (`tailscale serve` on the host to `[api] remote_port`). |
| Env file | Sourced by `/bin/sh` (`set -a; . file`) in the plist; Tickr never reads it. | Compose `env_file:` would put the values into `docker inspect`. | Bind-mounted read-only; the entrypoint sources it the same way. Only its path is in the container environment. A set-but-unreadable file refuses the start (exit 2). |
| Stopping | `launchctl kill TERM`; launchd `ExitTimeOut` = `bot_exit_timeout_seconds`. | `docker compose stop bot` sends SIGTERM; `stop_grace_period` is the SIGKILL delay. `init: true` (tini) forwards the signal to the supervisor. | `stop_grace_period: ${TICKR_BOT_EXIT_TIMEOUT}s`, from `[runtime] bot_exit_timeout_seconds` through the rendered `limits.env`. |
| Token approval | `token-needs-confirmation` alert, approve in the Webull app. | Same; the alert reaches you on Telegram. | Nothing. |

## Files

- `Dockerfile`: `python:3.12-slim-bookworm`, non-root user `tickr` (uid from a build arg),
  code and config root-owned and read-only to that user, `/app/data` the only writable folder
  and a volume. It also carries what the server's deploy needs, since the server has no clone:
  the unit templates and `deploy-server.sh` (`/app/ops/systemd`) and its own `compose.yaml`
  (`/app/ops/docker/compose.yaml`). `.dockerignore` is an allowlist: no data, env file, token or authorization file
  can enter the image.
- `ops/docker/entrypoint.sh`: sources the env file when `TICKR_ENV_FILE` is set, then `exec`.
- `ops/docker/compose.yaml`: project `tickr` (top-level `name:`, so containers are `tickr-bot-1`
  and so on whatever folder compose runs from), `api` (always on, `restart: unless-stopped`) and,
  under the `jobs` profile, `preflight`, `bot` and `watchdog` (`restart: "no"`, like the launchd
  jobs). Config and env file are read-only bind mounts; the data folder is a read-write bind
  mount of the host folder `TICKR_DATA_HOST`. Every service runs the image `TICKR_IMAGE` names
  (no default, `pull_policy: never`, no `build:`): compose never builds or pulls.

## Release flow (the server)

The server never builds an image. CI builds one from every change on main and pushes it to the
account's registry as `tickr-bot:<commit>` (full 40-character commit, uid and gid 1500;
`.github/workflows/server-image.yml`, `ops/aws/bot/README.md`, "Release image"). To release a
commit `<commit>` of main, while the bot is flat, on the server:

```sh
tickr-image-pull <commit>                                  # the host helper (below)
# copy compose.yaml, bot.toml and deploy-server.sh of <commit> (below, "Server files")
sudo /srv/tickr/bin/deploy-server.sh <commit>
```

1. `tickr-image-pull <commit>` is the host helper installed by the server bootstrap
   (`ops/aws/bot`, owned there, not by this page): it signs in to the registry, pulls
   `tickr-bot:<commit>` and adds a never-expiring `release-<commit>` tag. It reads the registry
   from `/srv/tickr/etc/registry.env`.
2. `deploy-server.sh <commit>` (below) checks the commit and that the image is already on the
   host (it never pulls), then writes `TICKR_IMAGE=<registry>/tickr-bot:<commit>` into
   `limits.env` with the other compose values. From then on every unit and every hand-run
   compose command (after loading `limits.env`) runs that image.

`/srv/tickr/etc/registry.env` (mode 644, written by the server's user-data) holds one line,
`TICKR_IMAGE_REPOSITORY=<account>.dkr.ecr.<region>.amazonaws.com/tickr-bot`, the full repository
address (the `image_repository` output of `ops/aws/bot`). The address holds the AWS account id,
so it lives only on the host: never in the repo, and the deploy script never prints it.

## Server files

The server has **no clone of the repo, no virtualenv and no GitHub credential**. Everything
Tickr runs comes from the release image; the few host files below are copied onto the server
(by SSM from the owner's side) or written by the server's own bootstrap. Every Python step of the
deploy (`render-systemd`, `require-flat`, `migrate-database`) runs inside the release image with `docker run --rm
--pull never`.

Layout (`/srv/tickr/etc` is the bootstrap's folder, mode 700, owner `tickr:tickr`; the units
run as `tickr` and read it). `deploy-server.sh` checks that folder and its `compose.yaml` and
`bot.toml` for exactly the mode and owner below and refuses otherwise (`etc-dir-unsafe`,
`etc-file-unsafe`); it never fixes them:

| Host path | What | Comes from | Mode, owner |
|---|---|---|---|
| `/srv/tickr/etc/compose.yaml` | `ops/docker/compose.yaml` of the release commit | **copied (SSM)** | 644 `tickr:tickr` |
| `/srv/tickr/etc/bot.toml` | the server's `config/bot.toml` (the commit's file, with the server's edits, e.g. the cutover `watchdog_restart_command` below) | **copied (SSM)** | 644 `tickr:tickr` |
| `/srv/tickr/etc/registry.env` | one line `TICKR_IMAGE_REPOSITORY=<account>.dkr.ecr.<region>.amazonaws.com/tickr-bot` | **copied (SSM)** unless the bootstrap already wrote it | 644 `root:root` |
| `/srv/tickr/bin/deploy-server.sh` | `ops/systemd/deploy-server.sh` of the release commit | **taken from the image** (below) or copied (SSM) | 755 `root:root` |
| `/run/tickr/env` | the credentials (env file) | written at boot by `tickr-secrets.service` from Secrets Manager (`ops/aws/bot`); **never copied, never in SSM parameters or command text** | 600 `tickr:tickr`, tmpfs |
| `/srv/tickr/etc/aws.env`, `domain.env` | region, domain | the bootstrap (user-data) | 644 |
| `/srv/tickr/etc/limits.env`, `ports.env` | rendered values | written by `deploy-server.sh` | 644 |
| `/srv/tickr/data/` | the data disk (database, token folder, `control/`) | the bootstrap mounts it; the bot writes it | 700 `tickr:tickr` |

No other `config/*.toml` is copied: `backtest.toml`, `replay.toml`, `research.toml`, `demo.toml`,
`settings_schema.toml` and `config/calendar/` are read from the image (`/app/config`); compose
mounts only `bot.toml` over the image's copy. Production only, later: the owner authorization
file (`[environments.production] authorization_file`) and its mount in `compose.yaml` (step 7
below); not needed in Sandbox.

Never copy the Mac's alert routing file (`<data_dir>/alert-routing.json`, `[alerts]
routing_file`) to the server. A file with `mac` or `imessage` cells is unusable there: every kind
goes to every channel and PUT /alerts/routing is refused until the file is deleted
(docs/api.md, "routing_cause"). On the server the routing starts with no file and the owner sets
it from the apps.

The exact SSM copy for a release `<commit>`, as root on the server (the files' contents come from
the commit in the owner's checkout; never send the env file or any secret this way):

```sh
install -m 644 -o tickr -g tickr /dev/stdin /srv/tickr/etc/compose.yaml   # ops/docker/compose.yaml of <commit>
install -m 644 -o tickr -g tickr /dev/stdin /srv/tickr/etc/bot.toml       # the server's bot.toml
install -m 644 /dev/stdin /srv/tickr/etc/registry.env                     # once, if missing
# deploy-server.sh from the image it deploys (no copy to keep in step):
install -d -m 755 /srv/tickr/bin
docker run --rm --pull never --network none --entrypoint cat \
  "$(sed -n 's/^TICKR_IMAGE_REPOSITORY=//p' /srv/tickr/etc/registry.env):<commit>" \
  /app/ops/systemd/deploy-server.sh > /srv/tickr/bin/deploy-server.sh
chmod 755 /srv/tickr/bin/deploy-server.sh
```

Consistency with the image: `deploy-server.sh` refuses (`compose-mismatch`) unless
`/srv/tickr/etc/compose.yaml` is byte for byte the copy the release image carries at
`/app/ops/docker/compose.yaml`, so a compose file from another commit cannot be installed.
`bot.toml` is the owner's config and cannot be compared: it **must be the release commit's
`config/bot.toml` plus the server's edits**. A stale one that the image's code does not accept
(a missing, renamed or unknown key) is refused by `render-systemd` in the image (config error,
nothing installed); a valid but outdated value is not detected, so copy it from the same commit
every release.

## Steps

1. Server: a local disk, Docker Engine with the compose plugin, NTP running
   (`timedatectl` shows `System clock synchronized: yes`).
2. The host files of "Server files" above (on the AWS server: copied by SSM; no clone). On a
   test box without the secrets unit, an env file by hand:
   ```sh
   install -m 600 /dev/null /srv/tickr/etc/env           # NAME='value' lines, as on the Mac
   ```
   Same variable names and quoting as `docs/operations.md`, One-time setup, step 2.
3. A file with **paths only** for compose, e.g. `/srv/tickr/etc/compose.env`:
   ```sh
   TICKR_CONFIG_HOST=/srv/tickr/etc/bot.toml
   TICKR_ENV_FILE_HOST=/srv/tickr/etc/env
   ```
   Compose also needs `limits.env` (rendered by `render-systemd`, below: the bot's grace period
   and `TICKR_DATA_HOST`, the data folder). Load both before a hand-run compose command:
   `set -a; . /srv/tickr/etc/compose.env; . /srv/tickr/etc/limits.env; set +a`.
4. The image: on the server, the release image (above, "Release flow"); `deploy-server.sh`
   writes it into `limits.env` as `TICKR_IMAGE`. Its user is uid and gid 1500, the server's
   `tickr` account; an image whose user does not own the data folder is refused
   (`data-owner-mismatch`).

   **An image built by hand** (a test box or a laptop, never the AWS server): build it with
   plain `docker build`, as the account that owns the env file, and name it in `TICKR_IMAGE`
   yourself:
   ```sh
   docker build -t tickr:dev \
       --build-arg TICKR_UID=$(id -u) --build-arg TICKR_GID=$(id -g) \
       --build-arg TICKR_CODE_VERSION=$(git rev-parse HEAD)$(git diff --quiet HEAD || echo -dirty) .
   export TICKR_IMAGE=tickr:dev
   ```
   `TICKR_CODE_VERSION` is the commit the image is built from (`-dirty` when a tracked file
   differs from it). The image has no `.git`, so this is how every run and trade records the
   code it ran on; without it preflight fails with `code-version-unknown` and the bot refuses
   to start. CI's release image sets it to the commit.
5. Token folder in the data folder (name from `[environments.*] token_dir`), then preflight:
   ```sh
   C="docker compose -f /srv/tickr/etc/compose.yaml --profile jobs"
   $C run --rm preflight mkdir -m 700 /app/data/webull-token-sandbox
   $C run --rm preflight; echo $?      # 1, alert-channel-missing, until Telegram is set
   ```
6. **Cutover step: send one test alert before the first bot start; the owner confirms it
   arrived.** With `TICKR_TELEGRAM_BOT_TOKEN` and `TICKR_TELEGRAM_CHAT_ID` in the env file
   (on AWS: in the `tickr-bot/env` secret, then `sudo systemctl restart tickr-secrets`):
   ```sh
   $C run --rm preflight python -m tickr.runtime --config /app/config/bot.toml \
       alert preflight-failed "test alert from the server"
   ```
   It sends one alert through every configured channel, exactly like the bot's jobs (same
   image, env file and config), and prints a JSON `alert` line `"delivery": "sending"`, then
   one per channel once it has answered: `"delivery":
   "shown"` with `"channel": "telegram"` when Telegram accepted it, or `"failed"` with an
   `error` naming only the HTTP status or the failure type (never the token or chat id).
   `preflight-failed` is a locked kind, so the owner's routing cannot turn it off. Do not start
   the bot until the message is on the owner's phone.
7. API: `docker compose -f /srv/tickr/etc/compose.yaml up -d api`;
   `curl -s http://127.0.0.1:8766/` on the server.
8. Production only: uncomment the authorization bind mount in `compose.yaml`.
9. Schedule the jobs with systemd timers (below). Stop the bot with
   `docker compose -f /srv/tickr/etc/compose.yaml stop bot`; logs with `... logs -f bot`.
   Before rebooting the server, the same rule as the Mac: wait for the `bot-exit` line.

### Rendered units (`render-systemd`) and the server deploy

`python -m tickr.runtime --config /app/config/bot.toml render-systemd --image IMAGE --out OUT`
writes, from `ops/systemd/*.template` (in the image at `/app/ops/systemd`), `bot.toml` and the
compose file named by `--compose` only. On the server it runs inside the release image (as
`deploy-server.sh` does it; there is no host Python):

```sh
I=<registry>/tickr-bot:<commit>; out=$(mktemp -d); chown 1500:1500 "$out"
docker run --rm --pull never --network none --entrypoint python \
  --mount type=bind,source=/srv/tickr/etc/bot.toml,target=/app/config/bot.toml,readonly \
  --mount type=bind,source=/srv/tickr/etc/compose.yaml,target=/srv/tickr/etc/compose.yaml,readonly \
  --mount type=bind,source="$out",target=/out \
  "$I" -m tickr.runtime --config /app/config/bot.toml render-systemd --image "$I" --out /out
```

It writes:

- `tickr-api.service` (always on; `docker compose up -d api`) and `tickr-{preflight,bot,watchdog,nightly-fetch}`
  `.service` + `.timer` (the compose jobs, `--profile jobs`). Timer times and weekdays are
  `[runtime]` `preflight_start_local`, `bot_start_local`, `watchdog_start_local`, `nightly_fetch_start_local` and
  `launch_weekdays`, in `America/New_York` (`--timezone`); a start missed while the server was
  down is not caught up.
- Every job unit has `Requires=` and `After=tickr-secrets.service`, the oneshot unit that writes
  the env file at `/run/tickr/env`. That unit comes from the server bootstrap; this command does
  not render it. Units name the env file by path (`TICKR_ENV_FILE_HOST`), never a value; compose
  mounts it read-only and the entrypoint sources it.
- `tickr-bot-restart.path` and `tickr-bot-restart.service`: the watchdog's bot restart on the
  server (below, "Bot restart on the server").
- `tickr-bot-start.path` and `tickr-bot-start.service`: the app's Start on the server (below,
  "Bot start on the server").
- `limits.env`: `TICKR_BOT_EXIT_TIMEOUT`, `TICKR_NIGHTLY_OUT`, `TICKR_NIGHTLY_SEED`,
  `TICKR_DATA_HOST` and `TICKR_IMAGE` (below).
- `ports.env`: `TICKR_API_PORT` and `TICKR_API_REMOTE_PORT` from `[api] port` and `remote_port`.
  Install it at `/srv/tickr/etc/ports.env`.

`--image` (required, no default) is the image every compose service runs, written to
`limits.env` as `TICKR_IMAGE`; `deploy-server.sh` passes the release image. `--compose` is the
absolute host path of the compose file every unit runs (`docker compose -f <it>`, no
`WorkingDirectory=`); render-systemd also reads it for the bot's stop grace, so inside a
container it is mounted at the same path. Defaults (flags
`--compose`, `--etc`, `--env-file`, `--data`, `--user`): `/srv/tickr/etc/compose.yaml`,
`/srv/tickr/etc` (`bot.toml` is `TICKR_CONFIG_HOST`), `/run/tickr/env`, `/srv/tickr/data` (the
data disk, written to `limits.env` as `TICKR_DATA_HOST`), `tickr`. `limits.env` (install it at `/srv/tickr/etc/limits.env`) holds
`TICKR_BOT_EXIT_TIMEOUT` = `[runtime] bot_exit_timeout_seconds` and the nightly fetch's
folders (`nightly_fetch_out`, `nightly_fetch_seed`); every unit loads it and
compose.yaml reads it as the bot's `stop_grace_period`, so the number lives only in `bot.toml`
(for a hand-run compose command: `set -a; . /srv/tickr/etc/limits.env; set +a`). `config/bot.toml` holds the server's values
(cutover, below), so the server runs the commit's file as it is.

`deploy-server.sh COMMIT [ETC [DATA]]` (on the server `/srv/tickr/bin/deploy-server.sh`,
"Server files") installs the rendered files. It needs no clone and no host Python. It
refuses (exit 1, nothing changed), in this order:

- `bad-commit`: `COMMIT` is not 40 lowercase hex characters (the tag CI gives the release
  image: `$GITHUB_SHA`). A branch name or a short commit is refused, never resolved.
- `registry-unknown`: `/srv/tickr/etc/registry.env` (`TICKR_REGISTRY_FILE`) is missing, or has no
  single unquoted `TICKR_IMAGE_REPOSITORY=<registry>/<repository>` line. The script reads that
  one line with `sed`; it never sources the file, and uses or prints no other line.
- `image-missing`: `<TICKR_IMAGE_REPOSITORY>:COMMIT` is not on the host (`docker image inspect`).
  The script never pulls: run `tickr-image-pull COMMIT` first.
- `path-not-absolute`: `ETC` or `DATA` is not an absolute path.
- `config-missing`, `compose-missing`: `ETC/bot.toml` or `ETC/compose.yaml` is not a file (copy
  them first, "Server files").
- `service-user-unknown`: the service user (`TICKR_SERVICE_USER`, default `tickr`) has no uid or
  gid on this host (`id -u`, `id -g`).
- `etc-dir-unsafe`: `ETC` is not owned by the service user and its primary group (`tickr:tickr`)
  with mode exactly 700, or is a symbolic link.
- `etc-file-unsafe`: `ETC/bot.toml` or `ETC/compose.yaml` is not owned by `tickr:tickr` with mode
  exactly 644, or is a symbolic link. The script never fixes owners or modes (no `chown`, no
  `chmod`): fix them by hand as in "Server files" (`install -m 644 -o tickr -g tickr`, the
  bootstrap's `install -d -m 700 -o tickr -g tickr /srv/tickr/etc`) and deploy again.

- `data-missing`: `DATA` (default `/srv/tickr/data`) is not an existing folder. It is never
  created here.
- `data-not-mounted`: `DATA` is not a mount point (`mountpoint -q`). The data disk is in fstab
  with `nofail`, so a disk that failed to mount leaves an empty folder on the root disk, and a
  bot started there would have no state. For the same reason every rendered unit that uses the
  data folder (the API, the four jobs, the restart and start paths and services) carries
  `RequiresMountsFor=DATA`: systemd refuses to start them without the mount.
- `image-user-unknown`, `data-owner-unknown` / `data-owner-mismatch`: the owner uid:gid of `DATA` is not the
  release image's runtime user, read with `id -u` and `id -g` in a throwaway container of the
  release image (`docker run --rm --pull never --network none --entrypoint id`: no job runs, no
  broker is called, no file is mounted). The containers could not write the bind-mounted data
  folder.
- `compose-mismatch`: `ETC/compose.yaml` differs from the image's own
  `/app/ops/docker/compose.yaml` (read with `--entrypoint cat`, no network), or the image has none.
- `render-systemd failed in the release image` (run it by hand as above to read the cause, e.g. a
  `bot.toml` the image's code rejects), or the rendered `limits.env` does not name the release
  image. render-systemd runs with no network; `bot.toml` and `compose.yaml` are mounted
  read-only, and only a scratch folder owned by the image's user is writable.
- unless `python -m tickr.runtime require-flat` passes, run in the release image with
  `docker run --rm --pull never --network host`, the image's own entrypoint (it sources the env
  file), `bot.toml` and the env file mounted read-only and the data folder read-write, the same
  mounts and network as the jobs: a fresh read-only broker read (the one `release` uses) shows no
  position and no open order, and the database has no open trade (opened read-only). The data
  folder is not mounted read-only because the broker's token folder is in it (the SDK may
  refresh the token) and SQLite needs the WAL's shared-memory file.
- while a bot restart or start request is waiting in `DATA/control` (its path unit would
  restart or start the bot the moment it starts: read the request, remove it, deploy again).
- when the rendered start request is not in the restart request's folder (render-systemd
  already refuses that; the script checks the files it installs).

It then creates only `DATA/control` (under the verified mount, owned by the image's uid:gid,
mode 700) and, when missing, its compose lock `DATA/control/compose.lock` (same owner, mode
600; never replaced, a unit may hold it), and takes that lock (`flock`, held until the
removal below: the bot, restart and start units wait for it). Still before anything is
installed it refuses:

- `bot-running`: the bot container runs (`docker compose ... -p tickr --profile jobs ps
  --status running -q bot`, with the rendered `limits.env`). Flat is not enough: a running bot
  keeps the image it started from, and after it ends a Start would start that stopped
  container as it is, the previous release. Deploy after the bot has ended.
- `bot-state-unknown`: that `ps` failed; `compose-lock-failed`: the lock could not be taken.

Then it installs the files (all or nothing, below). With the new `limits.env` in place and
still under the compose lock, it removes a stopped bot container: `docker compose --env-file ETC/limits.env -f ETC/compose.yaml -p tickr --profile
jobs rm -f bot`, without `-s`. Compose's `rm` removes only service containers that are not
running: `Remove` in `pkg/compose/remove.go` (2.24.6 and 2.40.3, the versions Ubuntu
24.04 ships, differ there only in names) inspects each container and keeps for removal only those whose
`State.Running` is false; only `-s` (`--stop`) stops containers first. A running bot is never
removed (and was refused above under the same lock); no container at all is "No stopped
containers", exit 0. So the next Start or
scheduled start creates the bot from the new release image, never the stopped container of
the previous one. When the removal fails it refuses with `bot-container-not-removed` (exit 1):
the new files are in place, but systemd is not reloaded and nothing is enabled or restarted;
read the docker line, then deploy again. Then, still under the lock and with no bot container,
it migrates the database to the new release's schema: `python -m tickr.runtime migrate-database`
in the release image (no network, no env file; `bot.toml` read-only, the data folder
read-write). The API and every other reader of a release read only their own schema, so without
this the days an older release wrote answer `503 database schema vN, this API reads vM` until the
bot's next start migrates them (2026-11-28, release cd25eeb). It keeps a verified copy of the old
schema first (`tickr.pre-v<N>.sqlite3` next to the database; it needs free space on the data
disk equal to the database's size), holds the bot's lock (`tickr.lock`: refused while a bot
runs), never creates a database, and refuses a file that is not a Tickr database (schema 0), a
database of another environment or one newer than the release. If the copy was written but a
migration step then failed (or the copy died half way, for example on a full disk), the next
try refuses with `FileExistsError` on that `pre-v<N>` file and deploying again does not help:
check the database's schema and the copy (`PRAGMA user_version`, `PRAGMA integrity_check`),
move the copy aside, free space if needed, then deploy again. The bot's own start behaves the
same. When it fails it refuses
with `database-not-migrated` (exit 1): the new files are in place, systemd is not reloaded and
nothing is enabled or restarted; read the `migrate-database` line, then deploy again. A rollback
to an older image after this finds a newer schema and refuses it (forward-only migrations): put
the `pre-v<N>` copy back only if nothing was written since. Last it reloads systemd and enables the timers,
`tickr-bot-restart.path`, `tickr-bot-start.path` and the API (`TICKR_SERVICE_USER`, default
`tickr`, is the units' `User=`). It never starts or stops the bot. Not yet run on a real
server.

The data folder was a named Docker volume (`tickr-data`) before the restart unit; it is now the
host folder `DATA`. No server ever ran on that volume (this deploy was never run on a real
server), so nothing needs copying. Had one, the copy while flat and with every job stopped would
be: `sudo cp -a "$(docker volume inspect <OLD_VOLUME> --format '{{ .Mountpoint }}')/." /srv/tickr/data/`,
then check the owner is the image's uid:gid.

The script runs `require-flat` inside the release image: an image older than that command
answers it with a usage error (exit 2), which the script treats as "not flat", so it fails
closed (refuses, installs nothing) rather than deploying. The unit templates and the code that
renders them come from the release image, so they always match it; `compose.yaml` is checked
against the image's copy; `bot.toml` must come from the same commit ("Server files").

The install is all or nothing: every file is first copied to a hidden temp file in its target
folder (a failed copy removes them and installs nothing), then renamed into place, and
`systemctl daemon-reload` runs only after every file is there and a stopped bot container
is removed.

### Bot restart on the server

The watchdog starts a down bot job once a day when the database shows an open trade
(`runtime/watchdog.py`). On the Mac its `[runtime] watchdog_restart_command` is a `launchctl
kickstart`. On the server the watchdog runs in its own container and cannot reach the host's
systemd, so the restart goes through a request file on the data folder:

1. The watchdog runs `python -m tickr.runtime request-restart --cause watchdog` in its
   container. It writes `[runtime] server_restart_request_file` (`control/restart-request`
   under the data folder: `/app/data/control/restart-request` in the containers,
   `/srv/tickr/data/control/restart-request` on the host) atomically (temp file, fsync, rename):
   one JSON line with `requested_at` (UTC) and `cause`. It never creates the folder: a missing
   `control` folder is refused with `control-folder-missing` (exit 1, nothing written), which the
   watchdog reports as a failed restart.
2. `tickr-bot-restart.path` (`PathExists=` on that file) starts `tickr-bot-restart.service`
   (`Type=oneshot`, user `tickr`), which runs each step once and stops at the first failure:
   (a) renames the request to `restart-request.taken`, so a request written during the restart
   is a new event; (b) under the compose lock (`flock /srv/tickr/data/control/compose.lock`,
   shared with the start and the scheduled bot unit, "Bot start on the server") `docker compose -f
   /srv/tickr/etc/compose.yaml -p tickr --profile jobs up -d --force-recreate --no-deps --pull never --no-build -t <grace> bot`: the
   same compose definition the scheduled `tickr-bot.service` starts. A running (hung) container
   is stopped within `<grace>` and recreated; a crashed or removed one is created again. The
   image is never pulled or built here: a missing `TICKR_IMAGE` (from `limits.env`) fails the step. `<grace>`
   is the bot's `stop_grace_period` read from `compose.yaml` when the unit is rendered (today
   `[runtime] bot_exit_timeout_seconds` through `limits.env`; render-systemd refuses when
   compose gives the bot none); (c) prints the taken request (time and cause) to the journal.
3. Never retried: no `Restart=`, no loop. Failures:
   - A failed step stays in `systemctl status tickr-bot-restart.service` and `journalctl -u
     tickr-bot-restart.service`; the taken file stays on disk for reading.
   - If the rename (a) itself fails, the request file stays, so the path unit triggers the
     service again until systemd's trigger limit. The path unit is then failed, and **later
     requests are ignored, with no alert,** until someone runs `systemctl reset-failed
     tickr-bot-restart.path` (after removing the cause). The restart (b) never ran in that case.
     The watchdog's own after-restart check (it alerts when the bot still is not running) is the
     backstop.

The watchdog's `restarted` journal line on the server means "request written", not "bot
started": check the unit's status for the restart itself.

### Bot start on the server

The app's Start (`POST /commands/start-bot`, docs/api.md "Start-bot") runs `[api]
start_bot_command` in the API's container. The restart request cannot serve it: its unit
force-recreates a running bot, and the API's container (no host pid namespace) cannot see the
bot's live pid in its lock file, so Start's lock check is blind there. Start has its own request
and unit, which never touch a running bot:

1. The API keeps its checks (a fresh status that says starting, running or stopping is `409`;
   the lock file; the operator stop marker is cleared), then runs `python -m tickr.runtime
   --config /app/config/bot.toml request-start --cause start-bot`. It writes `[runtime]
   server_start_request_file` (`control/start-request`, in the restart request's folder: the
   one folder `deploy-server.sh` creates; config refuses any other folder) the same way as the
   restart request: atomically, one JSON line with `requested_at` (UTC) and `cause`, refused
   with `control-folder-missing` or `cause-invalid` (exit 1, nothing written; the API answers
   `503`).
2. `tickr-bot-start.path` (`PathExists=` on that file) starts `tickr-bot-start.service`
   (`Type=oneshot`, user `tickr`, `After=tickr-bot-restart.service`: a start queued with or
   during a restart waits for it), which runs each step once and stops at the first failure:
   (a) renames the request to `start-request.taken`; (b) under the compose lock (below) `docker
   compose -f /srv/tickr/etc/compose.yaml -p tickr --profile jobs up -d --no-recreate --no-deps --pull
   never --no-build bot` (the same compose file, project and profile as the restart unit, no
   `WorkingDirectory=`); (c) prints the taken request to the journal.
3. `--no-recreate` ("If containers already exist, don't recreate them", `docker compose up
   --help`). Checked in the compose source of the versions Ubuntu 24.04 ships
   (`docker-compose-v2` 2.24.6 in noble, 2.40.3 in noble-updates; `pkg/compose/convergence.go`):
   with this policy `mustRecreate` is always false, so an existing bot container is kept; a
   running one is left as it is (no stop, no restart, no new container), and the start phase
   (`startService`) starts only the bot's containers that are not running. A missing container
   is created and started. A stopped container is started as it was created, with the image it
   was created from (bot.toml and the data folder are bind mounts, so it reads today's files),
   so a stopped container must never outlive its release: `deploy-server.sh` removes a stopped
   bot container once it has installed the new `limits.env` (above, the server deploy), and the
   next Start or scheduled start creates the container from the new release image. The deploy
   refuses while the bot container runs (`bot-running`), so no container of the previous
   release survives a deploy.
4. The compose step of the restart, the start and the scheduled bot unit (`tickr-bot.service`)
   holds one lock, `DATA/control/compose.lock` (`flock`, which waits for it; the file is created
   by `deploy-server.sh`, which holds it from its running-bot check to its removal of a stopped
   container), so a restart that arrives while a start runs, a scheduled start during a deploy,
   and so on, waits instead of running a second compose command on the bot at once.
5. Never retried, and the same failure modes as the restart unit: a failed step stays in
   `systemctl status tickr-bot-start.service`; a failed rename leaves the request, the path unit
   hits its trigger limit and later Starts are ignored until `systemctl reset-failed
   tickr-bot-start.path`. After the API's `202` (request written), a failed unit step, such as a
   missing image, shows only in `systemctl status tickr-bot-start.service` and its journal (the
   app sees a bot that never appears in `/status`; nothing alerts).

**Cutover (the server move release).** `config/bot.toml` itself holds the server's values, so
the server's `/srv/tickr/etc/bot.toml` is the commit's file with no hand edit, and merging that
change ends hosting on the Mac (its launchd jobs no longer work with it):

- `[runtime] watchdog_restart_command = ["python", "-m", "tickr.runtime", "--config",
  "/app/config/bot.toml", "request-restart", "--cause", "watchdog"]`;
- `[api] start_bot_command = ["python", "-m", "tickr.runtime", "--config",
  "/app/config/bot.toml", "request-start", "--cause", "start-bot"]` (above, "Bot start on the
  server");
- `[api] system_services = "none"` with `sleep_check_command = []` (no launchd, no caffeinate);
- `[api] training_progress_file = "training-progress.json"` under the data folder (no training
  job runs on the server: "no training job is reporting").

Then run `deploy-server.sh` (creates `/srv/tickr/data/control`, installs and starts the path
units) and check `systemctl status tickr-bot-restart.path tickr-bot-start.path` shows both
active (waiting).

### Hand-written timers (example, superseded by `render-systemd`)

One service and one timer per job. Times are the `[runtime]` values (03:30, 04:00, 09:05 on
`launch_weekdays` 1-5), written with an explicit zone. `Persistent=false`: a start missed while
the server was down is not caught up; start it by hand after reading the log.

```ini
# /etc/systemd/system/tickr-bot.service
[Unit]
Description=Tickr bot job (preflight, then the bot)
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
User=tickr
EnvironmentFile=/srv/tickr/etc/compose.env
ExecStart=/usr/bin/docker compose -f /srv/tickr/etc/compose.yaml --profile jobs up -d bot

# /etc/systemd/system/tickr-bot.timer
[Timer]
OnCalendar=Mon..Fri 04:00 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
```

`tickr-preflight` uses `run --rm preflight` at 03:30 and `tickr-watchdog` uses
`up -d watchdog` at 09:05, otherwise the same. The `User` must be in the `docker` group.

## Diagnostics bundle

When something is wrong and you cannot open files on the server, build the secret-free bundle
(docs/operations.md, "Diagnostics bundle") inside the container, into the data folder, and copy
it to your computer:

```sh
C="docker compose -f /srv/tickr/etc/compose.yaml --profile jobs"
$C run --rm preflight tickr diagnostics --out /app/data/diagnostics-$(date +%F).zip --days 7
# on the server the file is /srv/tickr/data/diagnostics-<DATE>.zip (TICKR_DATA_HOST)
scp <SERVER_USER>@<SERVER_HOST>:/srv/tickr/data/diagnostics-<DATE>.zip ~/Downloads/
```

The container's logs are Docker's, not files in the data folder, so the zip holds no
`logs/` unless a job writes `*.log` into `<data_dir>/logs`. Add them separately (they are the
bot's own output; never pipe `docker inspect` or the environment):

```sh
docker compose -f /srv/tickr/etc/compose.yaml logs --since 168h bot > /srv/tickr/bot-logs.txt
scp <SERVER_USER>@<SERVER_HOST>:/srv/tickr/bot-logs.txt ~/Downloads/
```

Delete the zip from the data folder after copying it.

## Before real money

- **Dedicated service account.** With `pid: host` the bot and watchdog containers see every
  process on the server. Run Tickr under its own account (for example `tickr`), not a
  personal or admin one: its uid and gid are the image user's (1500 for the release image,
  `TICKR_UID` and `TICKR_GID` for one built by hand), and make it the owner of the env file (`chmod 600`) and of the systemd units'
  `User=`.
- **The local API has no authentication.** With `network_mode: host` it listens on the
  server's own 127.0.0.1, which every local account can reach: it can switch entries and send
  cancel-own-orders and flatten commands. Fine on a single-user box; not on a shared VPS where
  other people or services have accounts.
- **Pin the base image by digest** (`FROM python:3.12-slim-bookworm@sha256:...`), so a rebuild
  cannot pull a different Python or Debian without a reviewed change.
- **Render the times from `[runtime]`.** Use `render-systemd` (above) for the timers; the compose
  `stop_grace_period` (`bot_exit_timeout_seconds`) comes from `bot.toml` through `limits.env`.
- **Without Telegram a failed systemd unit is the only signal.** Preflight and the bot job exit
  1 and the watchdog never exits 0 without a channel. Set the Telegram variables before the
  first run; watch `systemctl list-units --failed` and the container logs as well.

## Open owner decisions

1. **Linux alert channel: decided** (owner 2026-11-25): Telegram now, app push later. The
   removal of iMessage and the Mac notification ships in the same release as the server move, so
   critical alerts are never without a channel. Steps, step 6 proves the send before the first
   start.
2. **Where the server runs** (home box, VPS and its region; latency to Webull; who else has root
   on it, since root can read the env file and the token folder).
3. **How its clock is synced** (chrony or systemd-timesyncd, which servers), and whether
   preflight should check `timedatectl`'s "synchronized" flag (it does not today).

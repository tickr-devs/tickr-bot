# Running Tickr on a Linux server (Docker)

Today Tickr runs on the owner's Mac under launchd (`docs/operations.md`). This page says what
changes on an always-on Linux server, gives a Docker setup for it, and lists what the owner must
decide first.

**Status: not ready to trade.** The only alert channels are the macOS notification and iMessage,
both driven by `osascript`. A Linux host has neither, so preflight refuses the start with
`alert-channel-missing` and the bot job never starts the bot. Everything else below works; the
alert channel is an owner decision (see "Open owner decisions").

## Audit: what is Mac-only

| Area | On the Mac | On Linux / in Docker | Done here |
|---|---|---|---|
| Alert channels | `runtime/alerts.py`: Mac notification and iMessage through `/usr/bin/osascript` and Messages.app. | `default_notifier` returns no channel off macOS. Before this change every alert was journaled `"delivery": "no-notifier"` and preflight still passed: the bot would have run with nobody told. Nothing crashed (no import of `osascript`, it is only a path). | Preflight check `alert-channel` (before `alert-recipient`, so it is the reported cause) refuses with `alert-channel-missing` (blocks the start, exit 1). The watchdog journals `watchdog-alert-channel-refused` with that cause, still runs and journals, and never exits 0 (1 instead of 0; 3 on a closed day), so its unit shows failed. **Blocker.** |
| caffeinate | `/usr/bin/caffeinate` in the bot and watchdog plists' shell line. | Not needed: a server does not sleep. Not called anywhere in Python. | Nothing. |
| launchd | Five plists rendered by `render-launchd` from `[runtime]` times. | No launchd. `render-launchd` still works but its output is useless. | `ops/docker/compose.yaml` plus host systemd timers (below). The times are copied by hand from `[runtime]` into the timers: there is no `render-systemd` yet. |
| Paths | Logs in `~/Library/Logs/Tickr`; env file `~/.config/tickr/env`; repo root = parent of `config/`. | Logs are the container logs (`docker compose logs`), rotated by the json-file driver. Env file: any host path, mounted read-only at `/run/tickr/env`. Repo root is `/app` in the image. Preflight's hint still names `~/.config/tickr/env` (text only). | Compose file. |
| Watchdog schedule | Starts at load and at `[runtime] watchdog_start_local`, **Mac local time**, then runs at every hour (launchd `KeepAlive`); its broker checks come from the calendar (New York time). | Same command. The start time is the host timer's; give the timer the `America/New_York` zone so daylight-saving changes cannot move it. | Timer example below. |
| Clock | macOS syncs time automatically. Code uses UTC and converts with `zoneinfo` (`America/New_York`); `[risk] max_clock_skew_seconds` = 1 s against quote times. | Containers use the host kernel clock: the host must run NTP (chrony or systemd-timesyncd). `zoneinfo` needs tzdata. | `tzdata` installed in the image. NTP is an owner decision. |
| SQLite | `data/` in the repo; `fullfsync` on macOS (`store.py`). | `synchronous=FULL` and `fsync` are durable on a local Linux disk. WAL and the bot's `flock` lock need a local filesystem: never NFS/SMB. | Data folder is a local named volume shared by all jobs on one host. |
| Lock and watchdog | The watchdog checks the pid in the bot's lock file. | Separate containers have separate pid namespaces, so the watchdog would always see "no live pid" and alert `bot-down-with-open-trade`. | `pid: host` on the bot and watchdog services (see "Before real money"). |
| API binding | `[api] host` = 127.0.0.1, enforced by the API (refuses a non-loopback host). | With Docker's bridge network a 127.0.0.1 bind inside the container is unreachable from the host, and publishing a port would need a 0.0.0.0 bind, which the API refuses. | `network_mode: host`: the API is on the server's own loopback only; Docker publishes no ports. Phone access works as on the Mac (`tailscale serve` on the host to `[api] remote_port`). |
| Env file | Sourced by `/bin/sh` (`set -a; . file`) in the plist; Tickr never reads it. | Compose `env_file:` would put the values into `docker inspect`. | Bind-mounted read-only; the entrypoint sources it the same way. Only its path is in the container environment. A set-but-unreadable file refuses the start (exit 2). |
| Stopping | `launchctl kill TERM`; launchd `ExitTimeOut` = `bot_exit_timeout_seconds`. | `docker compose stop bot` sends SIGTERM; `stop_grace_period` is the SIGKILL delay. `init: true` (tini) forwards the signal to the supervisor. | `stop_grace_period: 120s`, kept equal to `[runtime] bot_exit_timeout_seconds` by hand. |
| Token approval | `token-needs-confirmation` alert, approve in the Webull app. | Same, but without an alert channel nobody is asked. | Covered by the blocker. |

## Files

- `Dockerfile`: `python:3.12-slim-bookworm`, non-root user `tickr` (uid from a build arg),
  code and config root-owned and read-only to that user, `/app/data` the only writable folder
  and a volume. `.dockerignore` is an allowlist: no data, env file, token or authorization file
  can enter the image.
- `ops/docker/entrypoint.sh`: sources the env file when `TICKR_ENV_FILE` is set, then `exec`.
- `ops/docker/compose.yaml`: `api` (always on, `restart: unless-stopped`) and, under the `jobs`
  profile, `preflight`, `bot` and `watchdog` (`restart: "no"`, like the launchd jobs). Config and
  env file are read-only bind mounts.

## Steps

1. Server: a local disk, Docker Engine with the compose plugin, NTP running
   (`timedatectl` shows `System clock synchronized: yes`).
2. Clone the repo, e.g. to `/srv/tickr/repo`. Put the config and env file outside it:
   ```sh
   install -d -m 700 /srv/tickr/etc
   cp config/bot.toml /srv/tickr/etc/bot.toml           # edit here, not in the repo
   install -m 600 /dev/null /srv/tickr/etc/env           # NAME='value' lines, as on the Mac
   ```
   Same variable names and quoting as `docs/operations.md`, One-time setup, step 2.
3. A file with **paths only** for compose, e.g. `/srv/tickr/etc/compose.env`:
   ```sh
   TICKR_CONFIG_HOST=/srv/tickr/etc/bot.toml
   TICKR_ENV_FILE_HOST=/srv/tickr/etc/env
   ```
4. Build as the dedicated service account that owns the env file (see "Before real money"):
   ```sh
   cd /srv/tickr/repo
   set -a; . /srv/tickr/etc/compose.env; set +a
   docker compose -f ops/docker/compose.yaml build \
       --build-arg TICKR_UID=$(id -u) --build-arg TICKR_GID=$(id -g) \
       --build-arg TICKR_CODE_VERSION=$(git rev-parse HEAD)$(git diff --quiet HEAD || echo -dirty)
   ```
   `TICKR_CODE_VERSION` is the commit the image is built from (`-dirty` when a tracked file
   differs from it). The image has no `.git`, so this is how every run and trade records the
   code it ran on; without it preflight fails with `code-version-unknown` and the bot refuses
   to start. Rebuild after every `git pull`.
5. Token folder in the data volume (name from `[environments.*] token_dir`), then preflight:
   ```sh
   C="docker compose -f ops/docker/compose.yaml --profile jobs"
   $C run --rm preflight mkdir -m 700 /app/data/webull-token-sandbox
   $C run --rm preflight; echo $?      # today: 1, alert-channel-missing
   ```
6. API: `docker compose -f ops/docker/compose.yaml up -d api`;
   `curl -s http://127.0.0.1:8766/` on the server.
7. Production only: uncomment the authorization bind mount in `compose.yaml`.
8. Schedule the jobs with systemd timers (below). Stop the bot with
   `docker compose -f ops/docker/compose.yaml stop bot`; logs with `... logs -f bot`.
   Before rebooting the server, the same rule as the Mac: wait for the `bot-exit` line.

### systemd timers (example)

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
WorkingDirectory=/srv/tickr/repo
EnvironmentFile=/srv/tickr/etc/compose.env
ExecStart=/usr/bin/docker compose -f ops/docker/compose.yaml --profile jobs up -d bot

# /etc/systemd/system/tickr-bot.timer
[Timer]
OnCalendar=Mon..Fri 04:00 America/New_York
Persistent=false

[Install]
WantedBy=timers.target
```

`tickr-preflight` uses `run --rm preflight` at 03:30 and `tickr-watchdog` uses
`up -d watchdog` at 09:05, otherwise the same. The `User` must be in the `docker` group.

## Before real money

- **Dedicated service account.** With `pid: host` the bot and watchdog containers see every
  process on the server. Run Tickr under its own account (for example `tickr`), not a
  personal or admin one: build with that account's uid and gid as `TICKR_UID` and
  `TICKR_GID`, and make it the owner of the env file (`chmod 600`) and of the systemd units'
  `User=`.
- **The local API has no authentication.** With `network_mode: host` it listens on the
  server's own 127.0.0.1, which every local account can reach: it can switch entries and send
  cancel-own-orders and flatten commands. Fine on a single-user box; not on a shared VPS where
  other people or services have accounts.
- **Pin the base image by digest** (`FROM python:3.12-slim-bookworm@sha256:...`), so a rebuild
  cannot pull a different Python or Debian without a reviewed change.
- **Render the times from `[runtime]`.** `stop_grace_period` (`bot_exit_timeout_seconds`) and the
  timer times (`preflight_start_local`, `bot_start_local`, `watchdog_start_local`,
  `launch_weekdays`) are copied by hand today. Add a `render-systemd` command that writes them
  from `bot.toml`, as `render-launchd` does for the plists, before first use.
- **Until an alert channel exists, a failed systemd unit is the only signal.** Preflight and the
  bot job exit 1 and the watchdog never exits 0 without a channel; nothing reaches the phone.
  Watch `systemctl list-units --failed` and the container logs yourself.

## Open owner decisions

1. **Linux alert channel (blocker).** iMessage needs Messages.app on a signed-in Mac. Choices
   include keeping a Mac as the iMessage sender, or a channel that works from Linux. A new
   channel is an owner decision; nothing is implemented. Until then preflight refuses on Linux.
2. **Where the server runs** (home box, VPS and its region; latency to Webull; who else has root
   on it, since root can read the env file and the token folder).
3. **How its clock is synced** (chrony or systemd-timesyncd, which servers), and whether
   preflight should check `timedatectl`'s "synchronized" flag (it does not today).
4. The `render-systemd` command (see "Before real money"): its shape, and whether it should also
   write the compose `stop_grace_period`.

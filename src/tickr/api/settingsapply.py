"""Settings API, writes (backlog S4 apply, history, rollback; apply rules S5).

- POST /settings/apply: a draft (as POST /settings/validate takes it) with the digest of each
  owner file it was made against and whether the app confirmed the owner with Face ID.
- POST /settings/rollback: the values as they were set at an earlier version, applied as a draft
  through the same rules.
- GET /settings/history: every version, oldest first.

Apply rules (owner 2026-11-15 and 2026-11-19, backlog S5), per changed key, from
`settings_schema.change_effect`:
- a change that tightens protection (`at_once`) is written to its file now, and a running bot
  adopts it for new entries at its next loop pass (runtime/liveprotection.py);
- a change that loosens protection (`next_session`) needs `biometric_confirmed` and waits: it is
  journaled `pending`, and the bot writes it at its first start on a later New York date
  (`promote_at_start`), so a restart on the same day never picks it up;
- a model or strategy setting (`when_flat`) is written only while the bot is flat (its status
  shows no position and no open reservation); it reaches the bot through a safe restart (stop-bot,
  start-bot: normal startup and reconciliation). Nothing is reloaded in a running bot;
- a setting that needs retraining is refused (it applies only with a newly trained model).
A later change of a key replaces its pending change (`superseded`).

The server cannot check Face ID: `biometric_confirmed` is the app's word that the owner passed
it, and the protection is the paired device token (remote port) plus that flag.

Writes are atomic and journaled first. Under an exclusive lock (`<history>.lock`, refused at
once when held: a concurrent edit), each file's digest must still be the one the draft was made
against and each candidate file must load with the bot's own loaders (`_check_loads`); the
history file records the version as `writing`, each file is replaced in one rename
(its other lines and comments untouched), then the version is `done`. A writer that stopped in
between is settled by the next one from the files' digests (`done`, `not_written`, `unknown`).
"""

from __future__ import annotations

import fcntl
import hashlib
import re
import shutil
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from tickr import config
from tickr import settings_schema as schema
from tickr.api import contract
from tickr.api.causes import safe_cause
from tickr.api.contract import (
    BotState,
    SettingChangeState,
    SettingsAppliedV1,
    SettingsDevice,
    SettingsHistoryV1,
    SettingsVersionChangeV1,
    SettingsVersionFileV1,
    SettingsVersionKind,
    SettingsVersionState,
    SettingsVersionV1,
    StatusResponseV1,
)
from tickr.api.settingsview import (
    BadDraft,
    SettingsUnavailable,
    draft_document,
    load_classes,
    load_schema,
    owner_path,
    owners_of,
    parse_changes,
    read_owner,
)
from tickr.calendar import ny_date_of
from tickr.config import Config
from tickr.lock import recorded_holder
from tickr.observers.errors import ObserverError
from tickr.observers.loader import (
    STRATEGIES_DIR,
    STRATEGY_FILE,
    check_history,
    load_strategy,
    parse_modules,
)
from tickr.settings_schema import Applies

DIGEST = re.compile(r"^[0-9a-f]{64}$")  # SHA-256 in lowercase hex (protocol constant)
_TABLE = re.compile(r"^\s*\[\s*([A-Za-z0-9_.\-]+)\s*\]\s*(#.*)?$")
_APPLY_FIELDS = frozenset({"changes", "base", "biometric_confirmed"})
_ROLLBACK_FIELDS = frozenset({"version", "base", "biometric_confirmed"})
APPLY_SHAPE = ('body must be {"changes": [{"owner", "key", "value"}, ...], "base": [{"owner", '
               '"digest"}, ...], "biometric_confirmed": true|false}')
ROLLBACK_SHAPE = ('body must be {"version": <number>, "base": [{"owner", "digest"}, ...], '
                  '"biometric_confirmed": true|false}')

Change = tuple[str, str, str]  # owner, dotted key, value as TOML text


class ApplyRefused(Exception):
    """A typed refusal: `error` is the ErrorV1 name, the message the one cause."""

    def __init__(self, error: str, cause: str) -> None:
        super().__init__(cause)
        self.error = error
        self.cause = cause


@dataclass(frozen=True)
class SettingsPaths:
    schema_file: Path
    config_file: Path  # owner `platform`
    root: Path  # holds strategies/<folder>/strategy.toml
    history: Path  # <data_dir>/<[api] settings_history_file>

    @property
    def lock(self) -> Path:
        return self.history.with_name(self.history.name + ".lock")

    @classmethod
    def from_config(cls, cfg: Config, config_file: Path) -> SettingsPaths:
        return cls(cfg.root / cfg.api.settings_schema_file, config_file, cfg.root,
                   cfg.root / cfg.run.data_dir / cfg.api.settings_history_file)


@dataclass(frozen=True)
class Request:
    changes: tuple[Change, ...]  # empty for a rollback
    rollback_to: int | None
    base: Mapping[str, str]  # owner -> digest the draft was made against
    biometric_confirmed: bool


# ---------------------------------------------------------------- request bodies


def _base(raw: Any, shape: str) -> dict[str, str]:
    if not isinstance(raw, list):
        raise BadDraft(shape)
    base: dict[str, str] = {}
    for item in raw:
        if (not isinstance(item, dict) or set(item) != {"owner", "digest"}
                or not isinstance(item["owner"], str) or not isinstance(item["digest"], str)
                or not DIGEST.match(item["digest"])):
            raise BadDraft(f"{shape}; each digest 64 lowercase hex characters")
        if item["owner"] in base:
            raise BadDraft(f"base names {item['owner']} twice")
        base[item["owner"]] = item["digest"]
    return base


def parse_apply(body: Mapping[str, Any]) -> Request:
    if set(body) != _APPLY_FIELDS or not isinstance(body["biometric_confirmed"], bool):
        raise BadDraft(APPLY_SHAPE)
    return Request(parse_changes(body["changes"], APPLY_SHAPE), None,
                   _base(body["base"], APPLY_SHAPE), body["biometric_confirmed"])


def parse_rollback(body: Mapping[str, Any]) -> Request:
    version = body.get("version")
    if (set(body) != _ROLLBACK_FIELDS or not isinstance(body["biometric_confirmed"], bool)
            or isinstance(version, bool) or not isinstance(version, int) or version < 0):
        raise BadDraft(ROLLBACK_SHAPE)
    return Request((), version, _base(body["base"], ROLLBACK_SHAPE), body["biometric_confirmed"])


# ---------------------------------------------------------------- the bot's state


def bot_may_run(state: StatusResponseV1, lock_file: Path) -> bool:
    """False only when the bot's last status says stopped and no process holds its lock."""
    status = state.status
    if status is None or status.bot_state is not BotState.STOPPED:
        return True
    try:
        return recorded_holder(lock_file) is not None
    except OSError:
        return True


def not_flat(state: StatusResponseV1, lock_file: Path) -> str | None:
    """Why the bot cannot be shown flat now, or None. Flat: its status shows no position and
    no open reservation (a working entry reserves its planned loss), and it is fresh or the bot
    is stopped with no process holding its lock."""
    status = state.status
    if status is None:
        return f"no readable bot status ({state.unavailable_cause}): flat is unknown"
    if state.stale and bot_may_run(state, lock_file):
        return "the bot's status is stale while it may be running: flat is unknown"
    if status.positions:
        return f"{len(status.positions)} position(s) open"
    if status.open_reservations_usd is None:
        return "the bot's open reservations are unknown"
    if status.open_reservations_usd > 0:
        return "an entry or trade is open (planned loss reserved)"
    return None


# ---------------------------------------------------------------- history file


def read_history(path: Path) -> tuple[SettingsVersionV1, ...]:
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return ()
    except OSError as exc:
        raise SettingsUnavailable(f"settings history unreadable: {type(exc).__name__}") from exc
    try:
        return contract.loads(SettingsHistoryV1, raw).versions
    except (UnicodeDecodeError, ValueError) as exc:
        raise SettingsUnavailable(f"settings history unreadable: {safe_cause(exc)}") from exc


def _save(path: Path, versions: tuple[SettingsVersionV1, ...]) -> None:
    contract.write_atomic(path, contract.dumps(SettingsHistoryV1(versions)))


@contextmanager
def _locked(paths: SettingsPaths) -> Iterator[None]:
    paths.lock.parent.mkdir(parents=True, exist_ok=True)
    with open(paths.lock, "a") as handle:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ApplyRefused("conflict", "another settings change is being written now: "
                               "read GET /settings/values again and retry") from None
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _settle(paths: SettingsPaths,
            versions: tuple[SettingsVersionV1, ...]) -> tuple[SettingsVersionV1, ...]:
    """Settle a last version left `writing` from its files' digests, and save it."""
    if not versions or versions[-1].state is not SettingsVersionState.WRITING:
        return versions
    last = versions[-1]
    found = set()
    for f in last.files:
        try:
            now = _digest(owner_path(f.owner, paths.config_file, paths.root))
        except OSError as exc:
            raise SettingsUnavailable(f"settings file of {f.owner} unreadable: "
                                      f"{type(exc).__name__}") from exc
        found.add(SettingsVersionState.DONE if now == f.digest_after else
                  SettingsVersionState.NOT_WRITTEN if now == f.digest_before else
                  SettingsVersionState.UNKNOWN)
    state = found.pop() if len(found) == 1 else SettingsVersionState.UNKNOWN
    settled = versions[:-1]
    if state is SettingsVersionState.NOT_WRITTEN:  # its promotions and supersessions undone
        settled = tuple(replace(v, changes=tuple(
            replace(c, state=SettingChangeState.PENDING, settled_by=None)
            if c.settled_by == last.version else c for c in v.changes)) for v in settled)
    versions = (*settled, replace(last, state=state))
    _save(paths.history, versions)
    return versions


def _live_pending(versions: tuple[SettingsVersionV1, ...]) -> dict[tuple[str, str],
                                                                   tuple[int, int]]:
    """(owner, key) -> (version, change index) of every pending change still to be written."""
    return {(c.owner, c.key): (v.version, i) for v in versions
            if v.state is SettingsVersionState.DONE
            for i, c in enumerate(v.changes) if c.state is SettingChangeState.PENDING}


# ---------------------------------------------------------------- file edits


def _toml_value(text: str) -> Any:
    return tomllib.loads(f"v = {text}", parse_float=Decimal)["v"]


def edited(text: str, key: str, value: str) -> str:
    """`text` (a TOML file) with the one line of dotted `key` holding `value` instead, its
    comment and every other line untouched. Refuses (SettingsUnavailable) a key that is not
    exactly one `name = scalar` line of its table, and a result that parses to anything but the
    old document with that one value replaced."""
    table, _, name = key.rpartition(".")
    line_of = re.compile(rf"^(\s*{re.escape(name)}\s*=\s*)([^#\s][^#]*?)(\s*(#.*)?)$")
    lines = text.splitlines(keepends=True)
    current: str | None = ""
    hits: list[int] = []
    for i, line in enumerate(lines):
        if line.lstrip().startswith("[["):
            current = None
            continue
        header = _TABLE.match(line)
        if header:
            current = header.group(1)
            continue
        if current == table and line_of.match(line.rstrip("\r\n")):
            hits.append(i)
    line = lines[hits[0]] if len(hits) == 1 else ""
    body = line.rstrip("\r\n")
    match = line_of.match(body)
    if match is None:
        raise SettingsUnavailable(f"{key}: not exactly one `{name} = value` line in [{table}]")
    lines[hits[0]] = match.group(1) + value + match.group(3) + line[len(body):]
    new = "".join(lines)
    try:
        before = schema.flatten(tomllib.loads(text, parse_float=Decimal))
        after = schema.flatten(tomllib.loads(new, parse_float=Decimal))
        expected = {**before, key: _toml_value(value)}
    except tomllib.TOMLDecodeError as exc:
        raise SettingsUnavailable(
            f"{key}: the edited file does not parse ({type(exc).__name__})") from exc
    if after != expected:
        raise SettingsUnavailable(f"{key}: the edit would change more than its value")
    return new


# ---------------------------------------------------------------- versions


@dataclass(frozen=True)
class _Write:
    owner: str
    path: Path
    shown: str
    before: str
    text: str

    @property
    def after(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def _commit(paths: SettingsPaths, versions: tuple[SettingsVersionV1, ...],
            version: SettingsVersionV1, writes: list[_Write],
            settles: Mapping[tuple[int, int], SettingChangeState]) -> SettingsVersionV1:
    """Journal `version` (and the pending changes it settles), write its files, mark it done."""
    earlier = tuple(replace(v, changes=tuple(
        replace(c, state=settles[(v.version, i)], settled_by=version.version)
        if (v.version, i) in settles else c for i, c in enumerate(v.changes)))
        for v in versions)
    files = tuple(SettingsVersionFileV1(w.owner, w.shown, w.before, w.after) for w in writes)
    state = SettingsVersionState.WRITING if writes else SettingsVersionState.DONE
    version = replace(version, files=files, state=state)
    _save(paths.history, (*earlier, version))
    if not writes:
        return version
    try:
        for w in writes:
            contract.write_atomic(w.path, w.text)
    except OSError as exc:
        _settle(paths, (*earlier, version))
        raise SettingsUnavailable(f"settings file not written: {type(exc).__name__}") from exc
    version = replace(version, state=SettingsVersionState.DONE)
    _save(paths.history, (*earlier, version))
    return version


def _writes(paths: SettingsPaths, entries: list[SettingsVersionChangeV1]) -> list[_Write]:
    writes: list[_Write] = []
    for owner in dict.fromkeys(e.owner for e in entries if e.state is SettingChangeState.WRITTEN):
        found = read_owner(owner, paths.config_file, paths.root)
        try:
            text = found.path.read_text("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise SettingsUnavailable(f"settings file {found.shown} unreadable: "
                                      f"{type(exc).__name__}") from exc
        if hashlib.sha256(text.encode("utf-8")).hexdigest() != found.digest:
            raise ApplyRefused("conflict", f"{found.shown} changed while it was read: retry")
        new = text
        for e in entries:
            if e.owner == owner and e.state is SettingChangeState.WRITTEN:
                new = edited(new, e.key, e.after)
        writes.append(_Write(owner, found.path, found.shown, found.digest, new))
    if writes:
        _check_loads(paths, writes)
    return writes


def _refused_load(shown: str, exc: Exception, *names: str) -> ApplyRefused:
    cause = str(exc) or type(exc).__name__
    for name in names:
        cause = cause.replace(name, shown)
    return ApplyRefused("bad_request", f"{shown} would not load: {cause}")


def _check_loads(paths: SettingsPaths, writes: list[_Write]) -> None:
    """Load every candidate file with the bot's own loaders before anything is written: the
    config file with `config.load` from a temp file beside it, a strategy file with
    `load_strategy` and `check_history` from a temp folder beside its folder. A value the schema
    passes but a loader refuses is refused here, with the loader's cause, and nothing is
    written or journaled."""
    platform = next((w for w in writes if w.owner == schema.PLATFORM), None)
    path = paths.config_file
    candidate = path.with_name(f".{path.name}.candidate-{uuid.uuid4().hex}.toml")
    try:
        if platform is None:
            cfg = config.load(path)
        else:
            candidate.write_text(platform.text, "utf-8")
            cfg = config.load(candidate)
    except (ValueError, OSError) as exc:
        shown = platform.shown if platform is not None else path.name
        raise _refused_load(shown, exc, str(candidate), candidate.name) from exc
    finally:
        candidate.unlink(missing_ok=True)
    for w in writes:
        if w.owner == schema.PLATFORM:
            continue
        folder = Path(tempfile.mkdtemp(prefix=f".{w.owner}.candidate-", dir=w.path.parent.parent))
        try:
            target = folder / STRATEGIES_DIR / w.owner / STRATEGY_FILE
            target.parent.mkdir(parents=True)
            target.write_text(w.text, "utf-8")
            loaded = load_strategy(folder, w.owner, parse_modules(cfg.observers.strategy_modules))
            check_history(loaded, cfg.market_data.history_sessions)
        except (ObserverError, OSError) as exc:
            raise _refused_load(w.shown, exc, str(target)) from exc
        finally:
            shutil.rmtree(folder, ignore_errors=True)


def _rollback_changes(versions: tuple[SettingsVersionV1, ...], target: int) -> tuple[Change, ...]:
    """The value of every key changed after version `target` as it was set at `target`: the
    last change at or before it, else the value before the first later change."""
    if target > len(versions):
        raise ApplyRefused("not_found", f"no version {target}: the latest is {len(versions)}")
    if target == len(versions):
        raise ApplyRefused("conflict", f"version {target} is the latest: nothing to roll back")
    if target and versions[target - 1].state is not SettingsVersionState.DONE:
        raise ApplyRefused("conflict", f"version {target} is "
                           f"{versions[target - 1].state.value}: it never applied")
    landed = [v for v in versions if v.state is not SettingsVersionState.NOT_WRITTEN]
    wanted: dict[tuple[str, str], str] = {}
    for v in landed:
        if v.version > target:
            for c in v.changes:
                wanted.setdefault((c.owner, c.key), c.before)
    for v in landed:
        if v.version <= target:
            for c in v.changes:
                if (c.owner, c.key) in wanted:
                    wanted[(c.owner, c.key)] = c.after
    if not wanted:
        raise ApplyRefused("conflict", f"nothing changed after version {target}")
    return tuple((owner, key, value) for (owner, key), value in wanted.items())


def apply(paths: SettingsPaths, request: Request, device: SettingsDevice, now: datetime,
          flat: Callable[[], str | None]) -> SettingsVersionV1:
    """Apply a draft or a rollback under the S5 rules, or raise ApplyRefused /
    SettingsUnavailable with nothing changed."""
    with _locked(paths):
        versions = _settle(paths, read_history(paths.history))
        changes = request.changes
        if request.rollback_to is not None:
            changes = _rollback_changes(versions, request.rollback_to)
        specs = load_schema(paths.schema_file)
        known = owners_of(specs)
        for owner in dict.fromkeys(c[0] for c in changes):
            if owner not in known:
                continue  # the draft refuses it as UNKNOWN_KEY
            if owner not in request.base:
                raise ApplyRefused("conflict", f"no base digest for {owner}: send the digests "
                                   "GET /settings/values answered")
            found = read_owner(owner, paths.config_file, paths.root)
            if found.digest != request.base[owner]:
                raise ApplyRefused("conflict", f"{found.shown} changed since it was read: read "
                                   "GET /settings/values again")
        draft = draft_document(specs, changes, paths.config_file, paths.root,
                               load_classes(paths.schema_file))
        if not draft.valid:
            raise ApplyRefused("bad_request", "draft refused: " + "; ".join(
                f"{r.owner} {r.key} {r.cause.value} {r.detail}" for r in draft.refusals))
        pending = _live_pending(versions)
        settles = {pending[(c.owner, c.key)]: SettingChangeState.SUPERSEDED
                   for c in draft.changes if (c.owner, c.key) in pending}
        moving = [c for c in draft.changes if c.changed]
        if not moving and not settles:
            raise ApplyRefused("conflict", "nothing to change: every value is the present one")
        if retrain := [c.key for c in moving if c.requires_retraining]:
            raise ApplyRefused("conflict", f"{', '.join(retrain)} applies only with a newly "
                               "trained model")
        if not request.biometric_confirmed and (loose := [c.key for c in moving
                                                          if c.needs_face_id]):
            raise ApplyRefused("biometric_required", f"{', '.join(loose)} loosens protection: "
                               "confirm with Face ID in the app")
        if flat_keys := [c.key for c in moving if c.applies is Applies.WHEN_FLAT]:
            cause = flat()
            if cause is not None:
                raise ApplyRefused("conflict", f"{', '.join(flat_keys)} applies only while the "
                                   f"bot is flat: {cause}")
        today = ny_date_of(now)
        entries = [SettingsVersionChangeV1(
            c.owner, c.key, c.current, c.draft, c.applies,
            SettingChangeState.PENDING if c.applies is Applies.NEXT_SESSION
            else SettingChangeState.WRITTEN,
            today if c.applies is Applies.NEXT_SESSION else None, None) for c in moving]
        kind = (SettingsVersionKind.APPLY if request.rollback_to is None
                else SettingsVersionKind.ROLLBACK)
        version = SettingsVersionV1(len(versions) + 1, kind, now, device,
                                    request.biometric_confirmed, request.rollback_to,
                                    SettingsVersionState.WRITING, (), tuple(entries))
        return _commit(paths, versions, version, _writes(paths, entries), settles)


def promote(paths: SettingsPaths, now: datetime) -> SettingsVersionV1 | None:
    """Write every pending change journaled on an earlier New York date than `now`'s (the bot's
    start: the next session has come). None when there is none. Re-checked against the schema
    and the files now; a refusal leaves every pending change as it is."""
    with _locked(paths):
        versions = _settle(paths, read_history(paths.history))
        today = ny_date_of(now)
        due = {at: versions[at[0] - 1].changes[at[1]]
               for at in _live_pending(versions).values()}
        due = {at: c for at, c in due.items()
               if c.effective_after is not None and c.effective_after < today}
        if not due:
            return None
        specs = load_schema(paths.schema_file)
        draft = draft_document(specs, tuple((c.owner, c.key, c.after) for c in due.values()),
                               paths.config_file, paths.root, load_classes(paths.schema_file))
        if not draft.valid:
            raise ApplyRefused("conflict", "pending changes refused now: " + "; ".join(
                f"{r.owner} {r.key} {r.cause.value} {r.detail}" for r in draft.refusals))
        entries = [SettingsVersionChangeV1(c.owner, c.key, c.current, c.draft,
                                           Applies.NEXT_SESSION, SettingChangeState.WRITTEN,
                                           None, None) for c in draft.changes if c.changed]
        version = SettingsVersionV1(len(versions) + 1, SettingsVersionKind.PROMOTE, now,
                                    SettingsDevice.BOT, False, None,
                                    SettingsVersionState.WRITING, (), tuple(entries))
        return _commit(paths, versions, version, _writes(paths, entries),
                       dict.fromkeys(due, SettingChangeState.PROMOTED))


def promote_at_start(cfg: Config, config_file: Path, now: datetime) -> tuple[str | None, bool]:
    """The bot's start: write the pending settings changes whose session has come. Returns a
    line for the bot's log (None when there was nothing to do) and whether a file changed, so
    the caller loads its config again. Never stops the start: on any refusal the files stay as
    they are (the tighter values keep running) and the line says why."""
    try:
        version = promote(SettingsPaths.from_config(cfg, config_file), now)
    except (ApplyRefused, SettingsUnavailable, OSError) as exc:
        cause = exc.cause if isinstance(exc, ApplyRefused) else safe_cause(exc)
        return f"settings: pending changes not written: {cause}", False
    if version is None:
        return None, False
    keys = ", ".join(f"{c.owner} {c.key} {c.before} -> {c.after}" for c in version.changes)
    return (f"settings: version {version.version} wrote pending changes: {keys or 'none'}",
            bool(version.files))


def history_document(paths: SettingsPaths) -> SettingsHistoryV1:
    return SettingsHistoryV1(read_history(paths.history))


def applied_document(version: SettingsVersionV1, bot_running: bool) -> SettingsAppliedV1:
    """A tightening of protection (`at_once`) needs no restart: the running bot adopts it at
    its next loop pass (runtime/liveprotection.py). Any other written change does."""
    waits = any(c.state is SettingChangeState.WRITTEN and c.applies is not Applies.AT_ONCE
                for c in version.changes)
    return SettingsAppliedV1(version, restart_needed=waits and bot_running)

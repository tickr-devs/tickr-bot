"""Build observer strategies from configuration.

`[strategy] observers` lists folders under `strategies/`. Each folder's `strategy.toml` names
its strategy in `[strategy] name`; `[observers] strategy_modules` maps that name to the module
whose `create(values)` builds it. Several folders may share one strategy name with different
values and versions (for example a wider-stop variant of price-action-v1 as version 2).

Every identity (name + version) must be distinct, the active strategy included, so recorded
decisions can always be told apart. Any problem is an ObserverError with one cause.
"""

from __future__ import annotations

import importlib
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aperture.config import Config
from aperture.observers.errors import ObserverError
from aperture.strategy.base import Strategy

STRATEGIES_DIR = "strategies"
STRATEGY_FILE = "strategy.toml"


@dataclass(frozen=True)
class LoadedStrategy:
    folder: str
    strategy: Strategy

    @property
    def identity(self) -> str:
        return f"{self.strategy.name} v{self.strategy.version}"


def parse_modules(entries: Sequence[str]) -> dict[str, str]:
    """`["name=module", ...]` -> {name: module}; malformed or duplicate entries are refused."""
    modules: dict[str, str] = {}
    for entry in entries:
        name, sep, module = entry.partition("=")
        name, module = name.strip(), module.strip()
        if not sep or not name or not module:
            raise ObserverError(f"[observers] strategy_modules entry is not name=module: {entry!r}")
        if name in modules:
            raise ObserverError(f"[observers] strategy_modules lists {name!r} twice")
        modules[name] = module
    return modules


def _read_values(root: Path, folder: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """The folder's strategy.toml, its [strategy] table and name; ObserverError otherwise."""
    path = root / STRATEGIES_DIR / folder / STRATEGY_FILE
    try:
        with open(path, "rb") as handle:
            values = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ObserverError(f"{folder}: cannot read {path}: {exc}") from exc
    table = values.get("strategy")
    name = table.get("name") if isinstance(table, dict) else None
    if not isinstance(name, str):
        raise ObserverError(f"{folder}: {STRATEGY_FILE} has no [strategy] name")
    return values, table, name


def read_identity(root: Path, folder: str) -> tuple[str, str]:
    """(name, version) a strategy folder declares, read from its file only (nothing imported
    or built): what its recorded decisions are tagged with. ObserverError when unreadable."""
    _, table, name = _read_values(root, folder)
    version = table.get("version")
    if not isinstance(version, str) or not version:
        raise ObserverError(f"{folder}: {STRATEGY_FILE} has no [strategy] version string")
    return name, version


def load_strategy(root: Path, folder: str, modules: Mapping[str, str]) -> LoadedStrategy:
    values, table, name = _read_values(root, folder)
    module_name = modules.get(name)
    if module_name is None:
        raise ObserverError(f"{folder}: no module for strategy {name!r} in strategy_modules")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ObserverError(f"{folder}: module {module_name} cannot be imported: {exc}") from exc
    create = getattr(module, "create", None)
    if not callable(create):
        raise ObserverError(f"{folder}: module {module_name} has no create(values)")
    try:
        strategy = create(values)
    except Exception as exc:  # any failure to build is a refusal, never a crash of the caller
        raise ObserverError(f"{folder}: values refused by {module_name}: {exc}") from exc
    reported = (getattr(strategy, "name", None), getattr(strategy, "version", None))
    if reported != (name, table.get("version")):
        raise ObserverError(
            f"{folder}: strategy reports {reported[0]} v{reported[1]}, "
            f"file says {name} v{table.get('version')}"
        )
    return LoadedStrategy(folder, strategy)


def check_history(loaded: LoadedStrategy, history_sessions: int) -> None:
    """Refuse a strategy that needs more prior sessions than the platform fetches: it would
    refuse every evaluation for missing history all day."""
    need = getattr(loaded.strategy, "prior_sessions_needed", None)
    if isinstance(need, bool) or not isinstance(need, int) or need < 0:
        raise ObserverError(
            f"{loaded.folder}: {loaded.identity} declares no valid prior_sessions_needed "
            f"(got {need!r})"
        )
    if need > history_sessions:
        raise ObserverError(
            f"{loaded.folder}: {loaded.identity} needs {need} prior sessions, "
            f"[market_data] history_sessions = {history_sessions}"
        )


def load_active(config: Config) -> LoadedStrategy:
    """The active strategy, refused (ObserverError) when it cannot be built or when the
    configured history is shorter than it needs."""
    loaded = load_strategy(config.root, config.strategy.active,
                           parse_modules(config.observers.strategy_modules))
    check_history(loaded, config.market_data.history_sessions)
    return loaded


def load_observers(config: Config) -> tuple[LoadedStrategy, ...]:
    """The configured observers, in configuration order. Empty list = no observers."""
    folders = config.strategy.observers
    settings = config.observers
    if len(folders) > settings.max_observers:
        raise ObserverError(
            f"{len(folders)} observers configured, at most {settings.max_observers} allowed"
        )
    if len(set(folders)) != len(folders):
        raise ObserverError("[strategy] observers lists a folder twice")
    if not folders:
        return ()
    modules = parse_modules(settings.strategy_modules)
    active = load_strategy(config.root, config.strategy.active, modules)
    seen = {active.identity: active.folder}
    loaded = []
    for folder in folders:
        observer = load_strategy(config.root, folder, modules)
        check_history(observer, config.market_data.history_sessions)
        if observer.identity in seen:
            raise ObserverError(
                f"{folder}: {observer.identity} is the same identity as {seen[observer.identity]}"
            )
        seen[observer.identity] = folder
        loaded.append(observer)
    return tuple(loaded)

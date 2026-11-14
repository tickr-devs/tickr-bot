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


def load_strategy(root: Path, folder: str, modules: Mapping[str, str]) -> LoadedStrategy:
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
    except ValueError as exc:
        raise ObserverError(f"{folder}: values refused by {module_name}: {exc}") from exc
    if strategy.name != name or strategy.version != table.get("version"):
        raise ObserverError(
            f"{folder}: strategy reports {strategy.name} v{strategy.version}, "
            f"file says {name} v{table.get('version')}"
        )
    return LoadedStrategy(folder, strategy)


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
        if observer.identity in seen:
            raise ObserverError(
                f"{folder}: {observer.identity} is the same identity as {seen[observer.identity]}"
            )
        seen[observer.identity] = folder
        loaded.append(observer)
    return tuple(loaded)

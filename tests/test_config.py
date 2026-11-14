from __future__ import annotations

import re
import shutil
from pathlib import Path

import pytest

from aperture import config
from aperture.cli import main
from aperture.risk.admission import AdmissionLimits

ROOT = Path(__file__).resolve().parent.parent
BOT = ROOT / "config" / "bot.toml"


def _copy(tmp_path: Path, text: str | None = None) -> Path:
    (tmp_path / "config").mkdir()
    target = tmp_path / "config" / "bot.toml"
    target.write_text(text if text is not None else BOT.read_text("utf-8"), "utf-8")
    return target


def test_bot_toml_loads():
    cfg = config.load(BOT)
    assert cfg.environment_name in cfg.environments
    assert cfg.root == ROOT
    assert len(cfg.digest) == 64


COMMENTED_TOML = [
    BOT,
    ROOT / "config" / "replay.toml",
    *sorted((ROOT / "strategies").glob("*/strategy.toml")),
]


@pytest.mark.parametrize("path", COMMENTED_TOML,
                         ids=lambda p: str(p.relative_to(ROOT)))
def test_every_key_is_commented(path: Path):
    assert config.uncommented_keys(path.read_text("utf-8")) == []


def test_uncommented_key_is_detected():
    text = "# a\nx = 1\n\ny = 2\nz = 3 # inline\n"
    assert config.uncommented_keys(text) == [(4, "y")]


def test_hash_inside_string_is_not_a_comment():
    text = '# ok\na = "x"\nprefix = "ap-#"\nb = \'it#\' # real\nc = "q\\"#"\n'
    assert config.uncommented_keys(text) == [(3, "prefix"), (5, "c")]


def test_environments_have_identical_keys():
    import tomllib
    envs = tomllib.loads(BOT.read_text("utf-8"))["environments"]
    key_sets = {name: set(table) for name, table in envs.items()}
    assert len({frozenset(k) for k in key_sets.values()}) == 1, key_sets


def test_unknown_key_refused(tmp_path):
    text = BOT.read_text("utf-8").replace("[risk]\n", "[risk]\n# x\nsurprise = 1\n", 1)
    with pytest.raises(config.ConfigError, match="unknown keys: surprise"):
        config.load(_copy(tmp_path, text))


def test_missing_key_refused(tmp_path):
    text = re.sub(r"(?m)^max_open_positions = .*$", "", BOT.read_text("utf-8"))
    with pytest.raises(config.ConfigError, match="missing keys: max_open_positions"):
        config.load(_copy(tmp_path, text))


def test_wrong_type_refused(tmp_path):
    text = re.sub(r"(?m)^max_open_positions = \d+", 'max_open_positions = "1"',
                  BOT.read_text("utf-8"))
    with pytest.raises(config.ConfigError, match="expected an integer"):
        config.load(_copy(tmp_path, text))


def test_cooldown_defaults():
    risk = config.load(BOT).risk
    assert (risk.symbol_cooldown_after_loss_enabled, risk.symbol_cooldown_minutes_after_loss) \
        == (True, 30)


@pytest.mark.parametrize("enabled", ["true", "false"])
def test_cooldown_below_one_minute_refused(tmp_path, enabled):
    # Zero is never a hidden "off": the _enabled key is the only off switch.
    text = re.sub(r"(?m)^symbol_cooldown_minutes_after_loss = \d+",
                  "symbol_cooldown_minutes_after_loss = 0", BOT.read_text("utf-8"))
    text = re.sub(r"(?m)^symbol_cooldown_after_loss_enabled = \w+",
                  f"symbol_cooldown_after_loss_enabled = {enabled}", text)
    with pytest.raises(config.ConfigError, match="must be at least 1"):
        config.load(_copy(tmp_path, text))


def test_cooldown_turned_off_loads(tmp_path):
    text = re.sub(r"(?m)^symbol_cooldown_after_loss_enabled = \w+",
                  "symbol_cooldown_after_loss_enabled = false", BOT.read_text("utf-8"))
    cfg = config.load(_copy(tmp_path, text))
    assert cfg.risk.symbol_cooldown_after_loss_enabled is False
    assert AdmissionLimits.from_config(cfg).symbol_cooldown_minutes_after_loss is None


@pytest.mark.parametrize("value", ["-1", "300", "301"])
def test_min_daily_room_must_sit_below_the_ceiling(tmp_path, value):
    text = re.sub(r"(?m)^min_daily_room_usd = \S+", f"min_daily_room_usd = {value}",
                  BOT.read_text("utf-8"))
    with pytest.raises(config.ConfigError, match="min_daily_room_usd"):
        config.load(_copy(tmp_path, text))


def test_unknown_environment_refused(tmp_path):
    text = re.sub(r'(?m)^environment = ".*"', 'environment = "staging"', BOT.read_text("utf-8"))
    with pytest.raises(config.ConfigError, match="not one of"):
        config.load(_copy(tmp_path, text))


def _with_environment(tmp_path: Path, name: str) -> Path:
    text = re.sub(r'(?m)^environment = ".*"', f'environment = "{name}"', BOT.read_text("utf-8"))
    return _copy(tmp_path, text)


def _gated_name(cfg: config.Config) -> str:
    """The environment that requires owner authorization, found from config, not by name."""
    return next(n for n, e in cfg.environments.items() if e.requires_owner_authorization)


def _open_name(cfg: config.Config) -> str:
    return next(n for n, e in cfg.environments.items()
                if e.trading_enabled and not e.requires_owner_authorization)


def test_gate_open_for_environment_without_authorization(tmp_path):
    cfg = config.load(BOT)
    loaded = config.load(_with_environment(tmp_path, _open_name(cfg)))
    assert config.trading_gate(loaded).open


def test_gate_closed_when_trading_disabled(tmp_path):
    cfg = config.load(BOT)
    loaded = config.load(_with_environment(tmp_path, _gated_name(cfg)))
    gate = config.trading_gate(loaded)
    assert not gate.open
    assert "disabled" in gate.cause


def _enabled_gated(tmp_path: Path) -> config.Config:
    cfg = config.load(BOT)
    name = _gated_name(cfg)
    text = _with_environment(tmp_path, name).read_text("utf-8")
    header = f"[environments.{name}]"
    head, tail = text.split(header, 1)
    tail = tail.replace("trading_enabled = false", "trading_enabled = true", 1)
    path = tmp_path / "config" / "bot.toml"
    path.write_text(head + header + tail, "utf-8")
    return config.load(path)


def _authorize(cfg: config.Config, **overrides: str) -> None:
    record = {"environment": cfg.environment_name, "config_digest": cfg.digest,
              "approved_on": "2026-11-14", "statement": "I approve."}
    record.update(overrides)
    path = cfg.root / cfg.environment.authorization_file
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f'{k} = "{v}"\n' for k, v in record.items()), "utf-8")


def test_gate_requires_authorization_file(tmp_path):
    cfg = _enabled_gated(tmp_path)
    gate = config.trading_gate(cfg)
    assert not gate.open and "not found" in gate.cause


def test_gate_opens_with_matching_authorization(tmp_path):
    cfg = _enabled_gated(tmp_path)
    _authorize(cfg)
    assert config.trading_gate(cfg).open


def test_gate_refuses_stale_digest(tmp_path):
    cfg = _enabled_gated(tmp_path)
    _authorize(cfg, config_digest="0" * 64)
    gate = config.trading_gate(cfg)
    assert not gate.open and "digest" in gate.cause


def test_gate_refuses_other_environment(tmp_path):
    cfg = _enabled_gated(tmp_path)
    _authorize(cfg, environment="elsewhere")
    assert not config.trading_gate(cfg).open


def test_gate_refuses_extra_fields(tmp_path):
    cfg = _enabled_gated(tmp_path)
    path = cfg.root / cfg.environment.authorization_file
    _authorize(cfg)
    path.write_text(path.read_text("utf-8") + 'extra = "x"\n', "utf-8")
    assert not config.trading_gate(cfg).open


def test_source_never_names_environments():
    cfg = config.load(BOT)
    pattern = re.compile("|".join(re.escape(n) for n in cfg.environments), re.IGNORECASE)
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
                 if pattern.search(p.read_text("utf-8"))]
    assert offenders == []


def test_every_config_field_has_a_reader():
    """A key nobody reads is a number that looks configurable but changes nothing."""
    import dataclasses
    import inspect

    source = "".join(p.read_text("utf-8") for p in (ROOT / "src").rglob("*.py")
                     if p.name != "config.py")
    # Readers inside config.py too (the gate, helpers); a declaration `name: type` never
    # matches `.name`.
    own = (ROOT / "src" / "aperture" / "config.py").read_text("utf-8")
    unread = [f"{cls.__name__}.{field.name}"
              for _, cls in inspect.getmembers(config, dataclasses.is_dataclass)
              if cls.__module__ == config.__name__
              for field in dataclasses.fields(cls)
              if not re.search(rf"\.{field.name}\b", source + own)]
    assert unread == []


def test_only_webull_adapter_imports_sdk():
    pattern = re.compile(r"^\s*(from|import)\s+webull", re.MULTILINE)
    allowed = ROOT / "src" / "aperture" / "broker" / "webull.py"
    offenders = [str(p.relative_to(ROOT)) for p in (ROOT / "src").rglob("*.py")
                 if p != allowed and pattern.search(p.read_text("utf-8"))]
    assert offenders == []


def test_cli_check_config(capsys):
    assert main(["--config", str(BOT), "check-config"]) == 0
    assert "trading gate" in capsys.readouterr().out


def test_cli_check_config_reports_too_little_history(tmp_path, capsys):
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    path = _copy(tmp_path, BOT.read_text("utf-8").replace("history_sessions = 5",
                                                          "history_sessions = 4", 1))
    assert main(["--config", str(path), "check-config"]) == 1
    assert "needs 5 prior sessions, [market_data] history_sessions = 4" in capsys.readouterr().out


def test_cli_reports_config_error(tmp_path, capsys):
    _copy(tmp_path, "[run]\n")
    assert main(["--config", str(tmp_path / "config" / "bot.toml"), "check-config"]) == 2


def test_cli_status_without_database(tmp_path, capsys):
    path = _copy(tmp_path)
    assert main(["--config", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "bot: not running" in out and "not created yet" in out


def test_cli_status_with_run(tmp_path, capsys):
    from aperture.cli import data_paths
    from aperture.store import Store
    path = _copy(tmp_path)
    cfg = config.load(path)
    database, _ = data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    store.start_run(environment=cfg.environment_name, config_digest=cfg.digest,
                    strategy="s", strategy_version="1", code_version="x")
    store.close()
    assert main(["--config", str(path), "status"]) == 0
    out = capsys.readouterr().out
    assert "1 runs" in out and "still open" in out


def test_route_limits_parsed():
    cfg = config.load(BOT)
    gated = next(e for e in cfg.environments.values() if e.requires_owner_authorization)
    assert gated.route_limits["/trading/orders/get"] == (2, config.Decimal(2))


@pytest.mark.parametrize("bad", ["[0, 30]", "[10]", '["10", 30]', "[10, -1]", "5"])
def test_bad_route_limit_refused(tmp_path, bad):
    new = f'route_limits = {{ "/x" = {bad} }}'
    text = BOT.read_text("utf-8").replace("route_limits = {}", new, 1)
    with pytest.raises(config.ConfigError, match="route_limits"):
        config.load(_copy(tmp_path, text))


def test_cli_status_is_read_only(tmp_path, capsys):
    from aperture.cli import data_paths
    from aperture.lock import SingleInstanceLock
    from aperture.store import Store
    path = _copy(tmp_path)
    cfg = config.load(path)
    database, lock_path = data_paths(cfg)
    Store.open(database, busy_timeout_seconds=0).close()
    before = database.read_bytes()
    with SingleInstanceLock(lock_path):
        assert main(["--config", str(path), "status"]) == 0
        assert "process exists (lock not verified)" in capsys.readouterr().out
    assert database.read_bytes() == before


def test_cli_status_refuses_schema_mismatch(tmp_path, capsys):
    import sqlite3

    from aperture.cli import data_paths
    path = _copy(tmp_path)
    database, _ = data_paths(config.load(path))
    database.parent.mkdir(parents=True)
    db = sqlite3.connect(database)
    db.execute("PRAGMA user_version = 99")
    db.close()
    assert main(["--config", str(path), "status"]) == 1
    out = capsys.readouterr().out
    assert "expects" in out
    db = sqlite3.connect(database)
    assert db.execute("PRAGMA user_version").fetchone()[0] == 99
    db.close()


def test_candidates_must_leave_the_reserved_stream_slots(tmp_path):
    text = BOT.read_text("utf-8").replace("candidate_subscriptions = 5",
                                          "candidate_subscriptions = 81")
    path = _copy(tmp_path, text)
    with pytest.raises(config.ConfigError, match="stream_reserved_symbols"):
        config.load(path)

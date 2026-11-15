"""The adapter is built only from config/bot.toml values, for every environment."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tickr import config
from tickr.broker.base import Refused
from tickr.broker.webull import (
    GATE_CLOSED,
    Route,
    broker_from_config,
    budget_from_config,
    settings_from_config,
)

from .test_webull_broker import ScriptedTransport, limit_buy

BOT = Path(__file__).resolve().parents[2] / "config" / "bot.toml"
TEXT = BOT.read_text("utf-8")
ENVIRONMENTS = list(config.load(BOT).environments)


def load_env(tmp_path: Path, name: str) -> config.Config:
    (tmp_path / "config").mkdir()
    target = tmp_path / "config" / "bot.toml"
    target.write_text(
        re.sub(r'(?m)^environment = ".*"$', f'environment = "{name}"', TEXT, count=1), "utf-8"
    )
    return config.load(target)


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_settings_come_from_the_environment(tmp_path, name):
    cfg = load_env(tmp_path, name)
    env = cfg.environment
    s = settings_from_config(cfg)
    assert s.trading_host == env.http_host and s.trading_host in s.allowed_hosts
    assert s.allowed_hosts == env.hosts
    assert (s.connect_timeout_seconds, s.read_timeout_seconds) == (
        env.http_connect_timeout_seconds, env.http_read_timeout_seconds)
    assert s.account_id_env == env.account_variable
    assert s.token_dir.endswith(env.token_dir) and cfg.run.data_dir in s.token_dir


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_budget_uses_default_and_route_limits(tmp_path, name):
    cfg = load_env(tmp_path, name)
    env = cfg.environment
    budget = budget_from_config(cfg, clock=lambda: 0.0)
    for route in Route:
        limit = budget.limit_for(route.path)
        if route.path in env.route_limits:
            count, window = env.route_limits[route.path]
            assert (limit.max_requests, limit.window_seconds) == (count, float(window))
        else:
            assert limit.max_requests == env.requests_per_route_per_minute
        assert limit.pause_after_429_seconds == float(env.rate_limit_cooldown_seconds)


@pytest.mark.parametrize("name", ENVIRONMENTS)
def test_gate_follows_config(tmp_path, name):
    cfg = load_env(tmp_path, name)
    transport = ScriptedTransport()
    broker = broker_from_config(cfg, transport)
    if config.trading_gate(cfg).open:
        return  # an open gate is exercised by the broker tests
    with pytest.raises(Refused) as exc:
        broker.place(limit_buy())
    assert exc.value.code == GATE_CLOSED
    assert transport.sent == []

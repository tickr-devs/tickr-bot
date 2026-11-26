"""Spec-v1 section 13.4: the four invariants, each checked across the whole bot rather than
one module (the module-level checks live next to their modules and are not repeated here):

(a) no request to a host outside the allowlist;
(b) no secret value in logs, output files or the database (planted canary);
(c) the trading gate stays closed without a valid owner authorization, on every path;
(d) no code path sends an entry without proven protection.

The Webull SDK runs for real where the SDK is installed, with the network replaced at
`requests.Session.send` by `FakeWebull`, a small stateful stand-in for the routes we use.
"""

from __future__ import annotations

import ast
import functools
import json
import logging
import os
import random
import shutil
import signal
from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

from tickr import cli, config
from tickr.broker.base import BrokerError, Refused
from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK, SessionCalendar
from tickr.config import GateResult
from tickr.data.base import FakeMarketData, ScreenerKind, ScreenerRow
from tickr.engine.core import Engine, EntryRefused, Fence, Leg, SendState, TradeState
from tickr.observers.log import DecisionLog
from tickr.runtime.entries import EntryDesk
from tickr.store import Store
from tickr.strategy.base import Check, Decision, Verdict
from tickr.types import OrderRequest, OrderType, Quote, Side, TimeInForce, TradingSession

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "tickr"
CFG = config.load(ROOT / "config" / "bot.toml")
# The `[strategy] observers` line config/bot.toml ships with.
SHIPPED_OBSERVERS = "observers = " + json.dumps(list(CFG.strategy.observers))
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)  # a regular session, in the probe window


# --- shared helpers ------------------------------------------------------------------------------


def repo_copy(tmp_path: Path, environment: str | None = None,
              replacements: tuple[tuple[str, str], ...] = ()) -> Path:
    """Copy config, strategies and calendars into `tmp_path` (so data_dir lands there);
    optionally switch the active environment and apply text replacements to bot.toml."""
    for part in ("config", "strategies"):
        shutil.copytree(ROOT / part, tmp_path / part)
    path = tmp_path / "config" / "bot.toml"
    text = path.read_text("utf-8")
    if environment is not None:
        text = text.replace(f'environment = "{CFG.environment_name}"',
                            f'environment = "{environment}"', 1)
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new, 1)
    path.write_text(text, "utf-8")
    return path


def gated_environment() -> str:
    """The environment that requires an owner authorization (Production in v1)."""
    return next(n for n, e in CFG.environments.items() if e.requires_owner_authorization)


def enable_trading(name: str) -> tuple[str, str]:
    """A bot.toml replacement that sets trading_enabled = true in environment `name` only."""
    header = f"[environments.{name}]"
    table = (ROOT / "config" / "bot.toml").read_text("utf-8").split(header, 1)[1]
    line = next(x for x in table.splitlines() if x.startswith("trading_enabled"))
    return header + table.split(line, 1)[0] + line, (
        header + table.split(line, 1)[0] + "trading_enabled = true")


# --- a stateful stand-in for the Webull HTTP API -------------------------------------------------


class FakeWebull:
    """Answers the routes the adapter uses from an in-memory account. Entries and exits fill
    at once at `fill_price`; stops rest. Every row names the account, like Webull does, and
    `echo_account_in_errors` puts the account id into error messages to test scrubbing."""

    def __init__(self, account: str, fill_price: D = D("10.00")) -> None:
        self.account = account
        self.fill_price = fill_price
        self.orders: dict[str, dict[str, Any]] = {}
        self.position = D(0)
        self.symbol = ""
        self.calls: list[tuple[str, str, str, str]] = []  # (method, host, path, body+query)
        self.scripted: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        self.ids = 0

    # -- wire -----------------------------------------------------------------------------

    def send(self, session: Any, prepared: Any, **kwargs: Any) -> Any:
        import requests

        parts = urlsplit(prepared.url)
        raw = prepared.body.decode() if isinstance(prepared.body, bytes) else (prepared.body or "")
        self.calls.append((prepared.method, parts.hostname or "", parts.path,
                           raw + parts.query))
        query = {k: v[0] for k, v in parse_qs(parts.query).items()}
        body = json.loads(raw) if raw else {}
        queue = self.scripted.get(parts.path)
        status, answer = queue.pop(0) if queue else self.answer(parts.path, query, body)
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(answer).encode()
        response.headers["Content-Type"] = "application/json"
        response.url = prepared.url
        return response

    def error(self, status: int, code: str, message: str) -> tuple[int, dict[str, Any]]:
        return status, {"error_code": code, "message": f"{message} (account {self.account})"}

    def row(self, order: dict[str, Any]) -> dict[str, Any]:
        return {"account_id": self.account, **order}

    def answer(self, path: str, query: dict[str, str],
               body: dict[str, Any]) -> tuple[int, Any]:
        if path == "/openapi/config":
            return 200, {"token_check_enabled": False}
        if path == "/trading/accounts/list":
            return 200, [{"account_id": self.account, "account_type": "MARGIN"}]
        if path == "/trading/assets/positions/list":
            return 200, ([] if self.position == 0 else [
                {"account_id": self.account, "symbol": self.symbol,
                 "quantity": str(self.position), "cost_price": str(self.fill_price)}])
        if path == "/trading/orders/open-orders/list":
            working = [self.row(o) for o in self.orders.values() if o["status"] == "SUBMITTED"]
            return 200, ([] if not working else [{"account_id": self.account,
                                                   "orders": working}])
        if path == "/trading/orders/get":
            order = self.orders.get(query.get("client_order_id", ""))
            if order is None:
                return self.error(417, "OPENAPI_PARAM_ERR", "Order not present")
            return 200, {"account_id": self.account, "orders": [self.row(order)]}
        if path == "/trading/orders/place":
            assert body["account_id"] == self.account
            return self.place(body["new_orders"][0])
        if path == "/trading/orders/cancel":
            order = self.orders.get(body.get("client_order_id", ""))
            if order is None or order["status"] != "SUBMITTED":
                return self.error(417, "OPENAPI_PARAM_ERR", "Order not present")
            order["status"] = "CANCELLED"
            return 200, {"client_order_id": order["client_order_id"]}
        return self.error(404, "NOT_FOUND", f"no such route {path}")

    def place(self, new: dict[str, Any]) -> tuple[int, Any]:
        cid, quantity = new["client_order_id"], D(new["quantity"])
        self.ids += 1
        order = {"client_order_id": cid, "symbol": new["symbol"], "side": new["side"],
                 "order_type": new["order_type"], "total_quantity": str(quantity),
                 "filled_quantity": "0", "status": "SUBMITTED"}
        self.orders[cid] = order
        if new["order_type"] != OrderType.STOP_LOSS:
            order.update(status="FILLED", filled_quantity=str(quantity),
                         filled_price=str(self.fill_price))
            self.symbol = new["symbol"]
            self.position += quantity if new["side"] == Side.BUY else -quantity
        return 200, {"client_order_id": cid, "order_id": f"ORDERID{self.ids}"}

    # -- what was sent --------------------------------------------------------------------

    def hosts(self) -> set[str]:
        return {host for _, host, _, _ in self.calls}

    def trading_requests(self) -> list[str]:
        return [path for method, _, path, _ in self.calls
                if path in ("/trading/orders/place", "/trading/orders/cancel")]


def install_fake_webull(monkeypatch: pytest.MonkeyPatch, account: str) -> FakeWebull:
    requests = pytest.importorskip("requests")
    pytest.importorskip("webull")
    wire = FakeWebull(account)
    monkeypatch.setattr(requests.Session, "send", lambda s, p, **kw: wire.send(s, p, **kw))
    return wire


def plant_canaries(monkeypatch: pytest.MonkeyPatch, cfg: config.Config) -> dict[str, str]:
    """A unique canary value in every credential variable the config names."""
    names = {cfg.advisor.api_key_variable}
    for env in cfg.environments.values():
        names |= {env.app_key_variable, env.app_secret_variable, env.account_variable}
    values = {name: f"CANARY{i}Z{name.lower()[:6]}Q7" for i, name in enumerate(sorted(names))}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def files_with(root: Path, needles: list[str]) -> list[str]:
    hits = []
    for path in sorted(root.rglob("*")):
        if path.is_file():
            data = path.read_bytes()
            hits += [f"{path.relative_to(root)}: {n}" for n in needles if n.encode() in data]
    return hits


# --- (a) outbound hosts --------------------------------------------------------------------------

NETWORK_MODULES = {"requests", "urllib3", "httpx", "aiohttp", "socket", "ssl", "paho", "grpc",
                   "websocket", "websockets", "http.client", "urllib.request", "ftplib",
                   "smtplib", "webull", "anthropic", "h2"}
# Who may import an outbound-network module, and why.
NETWORK_IMPORTERS = {
    "broker/webull.py": "the Webull adapter: every host comes from the allowlist (rule 9)",
    "advisor/client.py": "the owner's offline advisor tool calls Anthropic; never imported "
                         "by the bot (checked below)",
    "model/massive.py": "the model's market data (Massive REST, flat files, stream): every host "
                        "comes from [massive] in config, credentials by variable name only",
    "model/ticks.py": "the model's trades and quotes (Massive stream T/Q and REST backfill): "
                      "every host comes from [massive] in config, credentials by variable name "
                      "only",
    "model/stream.py": "the model's one Massive stocks connection (AM, T, Q on one socket): "
                       "host from [massive] in config, credentials by variable name only",
    "demo/record.py": "`tickr demo --record`: reads the demo's own API on the loopback port it "
                      "opened itself (127.0.0.1, port 0), never another host",
    "runtime/alerts.py": "the Telegram alert channel: one HTTPS POST to [alerts] telegram_host "
                         "from config, token and chat id by variable name only, never retried; "
                         "the apps' APNs push: one HTTP/2 POST per registered device to the "
                         "[alerts] apns_hosts host of its environment, key by variable name "
                         "only, never retried",
    "model/features/pinned/l1labels.py": "byte copy of the training's label builder (md5 "
                                         "0a22137f, never edited): socket only for "
                                         "gethostname() in its build claim files, which the "
                                         "feature path never calls; no connection is opened",
}


@functools.cache
def parsed(path: Path) -> ast.Module:
    return ast.parse(path.read_text("utf-8"))


def imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(parsed(path)):
        if isinstance(node, ast.Import):
            names |= {a.name for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module)
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute | ast.Name)
              and getattr(node.func, "attr", getattr(node.func, "id", "")) in (
                  "import_module", "__import__")
              and node.args and isinstance(node.args[0], ast.Constant)):
            names.add(str(node.args[0].value))
    return names


def is_network(name: str) -> bool:
    return any(name == m or name.startswith(m + ".") for m in NETWORK_MODULES)


def test_only_the_webull_adapter_and_the_advisor_import_network_libraries():
    offenders = {}
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        found = sorted(n for n in imports(path) if is_network(n))
        if found and rel not in NETWORK_IMPORTERS:
            offenders[rel] = found
    assert offenders == {}
    sdk = {path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
           if any(n == "webull" or n.startswith("webull.") for n in imports(path))}
    assert sdk == {"broker/webull.py"}  # hard rule 9


def test_the_bot_never_imports_the_advisor():
    offenders = [path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                 if path.relative_to(SRC).parts[0] != "advisor"
                 and any(n == "tickr.advisor" or n.startswith("tickr.advisor.")
                         for n in imports(path))]
    assert offenders == []


def test_no_host_name_is_written_in_source():
    """Hosts live in config; a literal host in code could bypass the allowlist."""
    hosts = {h for env in CFG.environments.values() for h in env.hosts}
    domains = {h.split(".", 1)[1] for h in hosts} | {".".join(h.split(".")[-2:]) for h in hosts}
    offenders = [f"{p.relative_to(SRC)}: {d}" for p in SRC.rglob("*.py")
                 for d in sorted(domains) if d in p.read_text("utf-8")]
    assert offenders == []


@pytest.mark.parametrize("environment", sorted(CFG.environments))
def test_every_adapter_request_goes_to_an_allowlisted_host(tmp_path, monkeypatch, environment):
    """The whole adapter stack, built from config as `tickr run` builds it, in every
    environment: token setup, account reads, market data and (where the gate allows) orders."""
    from tickr.broker.webull import bot_adapters, stream_settings_from_config

    cfg = config.load(repo_copy(tmp_path, environment))
    canaries = plant_canaries(monkeypatch, cfg)
    wire = install_fake_webull(monkeypatch, canaries[cfg.environment.account_variable])
    monkeypatch.chdir(tmp_path)
    broker, market, stream = bot_adapters(cfg, lambda kind, exc: None)
    assert stream_settings_from_config(cfg).mqtt_host in cfg.environment.hosts  # not started
    broker.account()
    broker.positions()
    broker.open_orders()
    for kind in ScreenerKind:
        try:
            market.screener(kind)
        except Exception:  # noqa: BLE001 - the stand-in has no screener; only the host matters
            pass
    gate = config.trading_gate(cfg)
    try:
        broker.place(OrderRequest("ap-allow-e1", "ABC", Side.BUY, OrderType.LIMIT, D(1),
                                  TimeInForce.DAY, TradingSession.CORE, limit_price=D("10.00")))
    except Exception:  # noqa: BLE001 - a closed gate refuses; only the hosts matter
        assert not gate.open
    assert wire.calls and wire.hosts() <= cfg.environment.hosts
    assert wire.hosts() == {cfg.environment.http_host}
    assert bool(wire.trading_requests()) == gate.open


# --- (b) planted canary --------------------------------------------------------------------------


def test_no_credential_value_reaches_output_logs_files_or_the_database(
        tmp_path, monkeypatch, capsys, caplog):
    """Every credential variable holds a canary. The real SDK transport carries the account
    canary on every account-scoped request and the stand-in echoes it in rows and error
    messages. Then the bot does what it does in a day: check-config, status, preflight, a
    refused entry, a protected trade with a failed read, an exit, a supervised `run` with an
    alert, status file, capture, decision log and the daily backup. No canary may appear in
    stdout, stderr, log records or ANY file written under the repo copy."""
    from tickr.broker.webull import broker_from_config
    from tickr.runtime import __main__ as runtime_main
    from tickr.runtime import service

    caplog.set_level(logging.DEBUG)
    bot_toml = repo_copy(tmp_path, replacements=(
        (SHIPPED_OBSERVERS, 'observers = ["missing-observer"]'),))
    cfg = config.load(bot_toml)
    if not config.trading_gate(cfg).open:
        pytest.skip("the configured environment's trading gate is closed")
    canaries = plant_canaries(monkeypatch, cfg)
    account = canaries[cfg.environment.account_variable]
    wire = install_fake_webull(monkeypatch, account)
    monkeypatch.chdir(tmp_path)  # anything written to the working folder is scanned too
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "NOTIFIER", lambda cfg: None)

    assert cli.main(["--config", str(bot_toml), "check-config"]) == 0
    (cfg.root / cfg.run.data_dir / cfg.environment.token_dir).mkdir(parents=True, mode=0o700)
    runtime_main.main(["--config", str(bot_toml), "preflight"])

    # A protected trade through the real adapter, in the bot's own database.
    database, _ = cli.data_paths(cfg)
    store = Store.open(database, busy_timeout_seconds=0)
    run_id = store.start_run(environment=cfg.environment_name, config_digest=cfg.digest,
                             strategy="invariants", strategy_version="1", code_version="-")
    clock = [1_000_000.0]
    engine = Engine(store, broker_from_config(cfg), cfg, run_id,
                    lambda: config.trading_gate(cfg), clock=lambda: clock[0])
    engine.recover()

    def steps(n: int) -> None:
        for _ in range(n):
            clock[0] += float(cfg.orders.detail_poll_seconds)
            engine.step()

    wire.scripted["/trading/orders/place"] = [wire.error(
        417, "OPENAPI_CAN_NOT_TRADING_FOR_NON_TRADING_HOURS", "Orders cannot be placed")]
    refused = engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D("0.40"))
    steps(2)
    assert engine.trade(refused).state is TradeState.CLOSED
    trade = engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D("0.40"))
    wire.scripted["/trading/orders/get"] = [wire.error(500, "INTERNAL_ERROR", "try later")]
    steps(4)
    stops = [o for o in engine.orders(trade) if o.leg is Leg.STOP]
    assert [o.verified for o in stops] == [True]
    engine.request_exit(trade, "invariants")
    steps(4)
    assert engine.trade(trade).state is TradeState.CLOSED and wire.position == 0
    store.end_run(run_id, "finished")
    store.close()

    # The supervised bot service, with a screener for capture and a broken observer (alert).
    market = FakeMarketData()
    market.set_screener(ScreenerKind.TOP_ACTIVES, [ScreenerRow(
        ScreenerKind.TOP_ACTIVES, 1, "id-ABC", "ABC", "NSQ", "USD", "COMMON_STOCK",
        {"symbol": "ABC", "close": "10.00"})])
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda c, on_error, on_token: cli.RunAdapters(
        broker_from_config(c), market, lambda: None, lambda: None))
    sleeps = [0]

    def sleep(seconds: float) -> None:
        sleeps[0] += 1
        if sleeps[0] == 3:
            os.kill(os.getpid(), signal.SIGTERM)

    monkeypatch.setattr(cli, "SLEEP", sleep)
    assert cli.main(["--config", str(bot_toml), "run"]) == 0
    assert cli.main(["--config", str(bot_toml), "status"]) == 0
    DecisionLog.from_config(cfg).close()

    # The run really happened and really carried the canary...
    paths = service.ServicePaths.from_config(cfg)
    assert paths.status.is_file()
    data = cfg.root / cfg.run.data_dir
    assert list((data / cfg.run.backups_dir).iterdir())
    store = Store.open_readonly(database)
    assert [a.body["kind"] for a in store.journal("alert")] == ["observers-stopped"]
    assert store.journal("order-refused")
    store.close()
    assert any(account in body for _, _, path, body in wire.calls
               if path == "/trading/orders/place")
    # ...and no canary came out anywhere.
    out = capsys.readouterr()
    text = out.out + out.err + caplog.text + "".join(
        str(r.args) for r in caplog.records)
    assert [n for n, v in canaries.items() if v in text] == []
    assert files_with(tmp_path, list(canaries.values())) == []
    assert wire.hosts() == {cfg.environment.http_host}


# --- (c) the trading gate ------------------------------------------------------------------------


GATED = gated_environment()


def gated_repo(tmp_path: Path, authorization: str | None) -> Path:
    """The gated environment active, trading enabled, and the given authorization file text
    (None = no file). The file is never valid: see each test."""
    bot_toml = repo_copy(tmp_path, GATED, replacements=(enable_trading(GATED),))
    cfg = config.load(bot_toml)
    if authorization is not None:
        path = cfg.root / cfg.environment.authorization_file
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(authorization.replace("{digest}", cfg.digest)
                        .replace("{env}", cfg.environment_name), "utf-8")
    return bot_toml


INVALID_AUTHORIZATIONS = {
    "missing": None,
    "stale-digest": ('environment = "{env}"\nconfig_digest = "' + "0" * 64 + '"\n'
                     'approved_on = "2026-11-14"\nstatement = "I approve."\n'),
    "other-environment": ('environment = "elsewhere"\nconfig_digest = "{digest}"\n'
                          'approved_on = "2026-11-14"\nstatement = "I approve."\n'),
    "not-toml": "this is not toml = = =",
}


@pytest.mark.parametrize("case", sorted(INVALID_AUTHORIZATIONS))
def test_without_a_valid_authorization_no_path_sends_a_trading_request(
        tmp_path, monkeypatch, case):
    """Engine, entry desk, the manual trade command and the bot service, all over the real
    adapter: zero place or cancel requests reach the wire, while reads still work."""
    from tickr.broker.webull import broker_from_config

    bot_toml = gated_repo(tmp_path, INVALID_AUTHORIZATIONS[case])
    cfg = config.load(bot_toml)
    assert cfg.environment.trading_enabled and not config.trading_gate(cfg).open
    canaries = plant_canaries(monkeypatch, cfg)
    wire = install_fake_webull(monkeypatch, canaries[cfg.environment.account_variable])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cli, "NOW", lambda: MONDAY_11)
    monkeypatch.setattr(cli, "NOTIFIER", lambda cfg: None)

    # The manual trade command: refused before anything is sent.
    assert cli.main(["--config", str(bot_toml), "trade", "ABC", "--limit", "10.00"]) == 3

    # The bot service: protects and reads, never trades.
    monkeypatch.setattr(cli, "RUN_ADAPTERS", lambda c, on_error, on_token: cli.RunAdapters(
        broker_from_config(c), FakeMarketData(), lambda: None, lambda: None))
    monkeypatch.setattr(cli, "SLEEP", lambda seconds: os.kill(os.getpid(), signal.SIGTERM))
    assert cli.main(["--config", str(bot_toml), "run"]) == 0

    # The entry desk and the engine, even if admission were bypassed.
    store = Store.open(tmp_path / "desk.sqlite3", busy_timeout_seconds=0)
    engine = Engine(store, broker_from_config(cfg), cfg, "gate0001",
                    lambda: config.trading_gate(cfg))
    engine.recover()
    desk = EntryDesk(engine, cfg, calendar=SessionCalendar.load(
        (cfg.root / cfg.session.calendar_file).parent),
        last_broker_read_at=lambda: MONDAY_11, log=DecisionLog(tmp_path / "decisions"))
    record = desk.on_decision(enter_decision(), bar_start=MONDAY_11 - timedelta(minutes=1),
                              quote=Quote("ABC", D("10.04"), D("10.05"), D(100), D(100),
                                          MONDAY_11, MONDAY_11), now=MONDAY_11)
    assert record.admission == "refused" and "GATE" in (record.refusal_cause or "").upper()
    with pytest.raises(EntryRefused, match="gate"):
        engine.open_trade("ABC", D(1), D("10.00"), D("9.80"), D("0.20"))
    store.close()

    assert wire.trading_requests() == []
    assert any(path == "/trading/orders/open-orders/list" for _, _, path, _ in wire.calls)
    assert wire.hosts() <= cfg.environment.hosts


def enter_decision() -> Decision:
    return Decision(strategy=CFG.strategy.active, version="1", symbol="ABC",
                    verdict=Verdict.ENTER, checks=(Check("rule", D(1), ">=", D(1), True),),
                    setup="breakout", trigger=D("10.00"), invalidation=D("9.50"),
                    entry_low=D("10.00"), entry_high=D("10.10"), stop=D("9.80"),
                    quantity=D(1))


def test_every_engine_and_adapter_in_source_is_built_with_the_trading_gate():
    """The gate cannot be left out by accident: each Engine(...) and WebullBroker(...) call
    in product code passes `trading_gate` (directly or in a lambda)."""
    missing = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(parsed(path)):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in ("Engine", "WebullBroker")):
                if "trading_gate" not in ast.unparse(node):
                    missing.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert missing == []


# --- (d) no entry without proven protection ------------------------------------------------------


def test_only_engine_open_trade_creates_entries_and_only_the_engine_sends():
    """Structural: the one entry intent is written in Engine.open_trade; only the engine
    calls broker.place/cancel (besides the F7 probe); only the entry desk (admission) and the
    manual command (probe limits) call open_trade."""
    entry_writers, senders, openers = [], [], []
    for path in SRC.rglob("*.py"):
        rel = path.relative_to(SRC).as_posix()
        functions = [n for n in ast.walk(parsed(path)) if isinstance(n, ast.FunctionDef)]
        for fn in functions:
            for node in ast.walk(fn):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                name = node.func.attr
                if name == "_write_intent" and "Leg.ENTRY" in ast.unparse(node):
                    entry_writers.append(f"{rel}:{fn.name}")
                elif name in ("place", "cancel") and "broker" in ast.unparse(node.func):
                    senders.append(f"{rel}:{fn.name}")
                elif name == "open_trade":
                    openers.append(f"{rel}:{fn.name}")
    assert entry_writers == ["engine/core.py:open_trade"]
    # The one other sender: the owner-run probes' shared plumbing (F7, and the trailing probe
    # through it), which hold the bot's lock (no engine runs next to them), record every order
    # before sending it and never resend.
    assert sorted(set(senders)) == ["engine/core.py:_cancel", "engine/core.py:_send",
                                    "engine/session_probe.py:finish",
                                    "engine/session_probe.py:place"]
    # Combo orders (an entry with attached legs): only the owner-run F8 probe sends them, under
    # the same rules as the F7 probe (whose plumbing it reuses).
    combo_senders = sorted({f"{path.relative_to(SRC).as_posix()}:{fn.name}"
                            for path in SRC.rglob("*.py")
                            for fn in ast.walk(parsed(path)) if isinstance(fn, ast.FunctionDef)
                            for node in ast.walk(fn)
                            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                            and node.func.attr == "place_combo"
                            and "broker" in ast.unparse(node.func)})
    assert combo_senders == ["engine/attached_probe.py:place_combo"]
    assert sorted(openers) == ["cli.py:_trade", "runtime/entries.py:on_decision"]


class AuditedBroker(FakeBroker):
    """Fills at random and checks, at every BUY that reaches the broker, that each position
    the broker holds is covered by working stops (broker truth)."""

    def __init__(self, rng: random.Random, read_failure_rate: float = 0.0,
                 refuse_stops: bool = False) -> None:
        super().__init__()
        self.rng = rng
        self.read_failure_rate = read_failure_rate
        self.refuse_stops = refuse_stops
        self.violations: list[str] = []
        self.entries = 0

    def order_detail(self, client_order_id: str):  # type: ignore[override]
        if self.rng.random() < self.read_failure_rate:
            raise BrokerError("scripted read failure")
        return self.orders_detail(client_order_id)

    def orders_detail(self, client_order_id: str):
        return super().order_detail(client_order_id)

    def fill(self, client_order_id: str, quantity: D, price: D) -> None:
        rate, self.read_failure_rate = self.read_failure_rate, 0.0  # fill reads its own book
        try:
            super().fill(client_order_id, quantity, price)
        finally:
            self.read_failure_rate = rate

    def place(self, request: OrderRequest):  # type: ignore[override]
        if self.refuse_stops and request.order_type == OrderType.STOP_LOSS:
            self.sent.append(request)
            raise Refused("REFUSED", "scripted stop refusal")
        if request.side is Side.BUY:
            self.entries += 1
            for position in self.positions():
                covered = sum((o.quantity - o.filled_quantity for o in self.open_orders()
                               if o.symbol == position.symbol
                               and o.order_type == OrderType.STOP_LOSS), D(0))
                if covered < position.quantity:
                    self.violations.append(
                        f"entry {request.client_order_id} sent while {position.quantity} "
                        f"{position.symbol} held with {covered} under stops")
        return super().place(request)

    def market_moves(self) -> None:
        """Working entries and exits fill (partly or fully), sometimes a stop triggers."""
        for cid, order in list(self.orders.items()):
            if order.state.terminal or self.rng.random() < 0.5:
                continue
            left = order.quantity - order.filled_quantity
            if order.order_type == OrderType.STOP_LOSS:
                if self.rng.random() < 0.1 and self.holdings.get(order.symbol):
                    held = self.holdings[order.symbol].quantity
                    self.fill(cid, min(left, held), D("9.80"))
                continue
            self.fill(cid, self.rng.choice([D(1), left]) if left > 1 else left, D(10))


OUTCOMES = ["ack"] * 6 + ["refuse", "unknown", "unknown-lost", "rate-limited"]


SEEDS = range(4)
STEPS = 120  # engine steps per randomised day


def single_sends(broker: FakeBroker) -> bool:
    ids = [r.client_order_id for r in broker.sent]
    return len(ids) == len(set(ids))


def random_day(path: Path, seed: int) -> AuditedBroker:
    rng = random.Random(seed)
    broker = AuditedBroker(rng, read_failure_rate=0.1)
    store = Store.open(path, busy_timeout_seconds=0)
    clock = [1_000_000.0]
    engine = Engine(store, broker, CFG, f"inv{seed:05d}", lambda: GateResult(True, "t"),
                    clock=lambda: clock[0])
    engine.recover()
    for _ in range(STEPS):
        broker.script(rng.choice(OUTCOMES))
        if rng.random() < 0.3:
            try:
                engine.open_trade("ABC", D(rng.randint(1, 3)), D("10.00"), D("9.80"), D(1))
            except EntryRefused:
                pass
        broker.outcomes.clear()
        broker.market_moves()
        clock[0] += float(CFG.orders.detail_poll_seconds)
        engine.reconcile() if rng.random() < 0.2 else engine.step()
        for trade in engine.trades():
            if rng.random() < 0.05:
                engine.request_exit(trade.trade_id, "random")
        if not engine.trades() and rng.random() < 0.2:
            for name in list(engine.fences()):
                engine.clear_operator_fence(name)
    store.close()
    return broker


def test_no_entry_is_sent_while_a_position_lacks_a_working_stop(tmp_path):
    """Randomised days on the fake broker: refused, lost and unknown sends, partial fills,
    stop triggers, failed reads and cancels, operator fence clearing, and entry attempts all
    along. With max_open_positions = 1 (v1) the broker never holds an unstopped position
    when an entry reaches it, and every send happens once."""
    entries = 0
    for seed in SEEDS:
        broker = random_day(tmp_path / f"{seed}.sqlite3", seed)
        assert broker.violations == [], seed
        assert single_sends(broker), seed
        entries += broker.entries
    assert entries > 3 * len(SEEDS)  # entries really followed earlier trades


def two_position_day(path: Path, seed: int) -> tuple[list[str], int]:
    """Returns the violations and how many entries were sent while another trade was open."""
    rng = random.Random(1000 + seed)
    broker = FakeBroker()
    two = replace(CFG, risk=replace(CFG.risk, max_open_positions=2))
    store = Store.open(path, busy_timeout_seconds=0)
    clock = [1_000_000.0]
    engine = Engine(store, broker, two, f"two{seed:05d}", lambda: GateResult(True, "t"),
                    clock=lambda: clock[0])
    engine.recover()
    violations: list[str] = []
    concurrent = [0]
    real_place = broker.place

    def audited(request: OrderRequest):
        if request.side is Side.BUY:
            others = [t for t in engine.trades()
                      if all(o.client_order_id != request.client_order_id
                             for o in engine.orders(t.trade_id))]
            concurrent[0] += bool(others)
            for trade in others:
                orders = engine.orders(trade.trade_id)
                held = engine.expected_quantity(orders)
                stopped = sum((o.open_quantity for o in orders
                               if o.leg is Leg.STOP and o.verified), D(0))
                unknown = [o.client_order_id for o in orders
                           if o.send_state is SendState.UNKNOWN]
                if held > stopped or unknown:
                    violations.append(f"{request.client_order_id}: {trade.trade_id} holds "
                                      f"{held}, verified stops {stopped}, unknown {unknown}")
        return real_place(request)

    broker.place = audited  # type: ignore[method-assign]
    for _ in range(STEPS):
        broker.script(rng.choice(OUTCOMES))
        if rng.random() < 0.4:
            try:
                engine.open_trade(rng.choice(["ABC", "XYZ"]), D(rng.randint(1, 3)),
                                  D("10.00"), D("9.80"), D(1))
            except EntryRefused:
                pass
        broker.outcomes.clear()
        for cid, order in list(broker.orders.items()):
            if (not order.state.terminal and order.order_type != OrderType.STOP_LOSS
                    and rng.random() < 0.5):
                broker.fill(cid, order.quantity - order.filled_quantity, D(10))
        clock[0] += float(two.orders.detail_poll_seconds)
        engine.reconcile() if rng.random() < 0.2 else engine.step()
        for trade in engine.trades():
            if rng.random() < 0.05:
                engine.request_exit(trade.trade_id, "random")
    store.close()
    assert single_sends(broker), seed
    return violations, concurrent[0]


def test_a_second_entry_waits_for_the_first_position_to_be_verified_protected(tmp_path):
    """With room for two positions, the engine (by its own records, the only thing it can
    know between reads) never sends an entry while another trade holds shares that no
    verified stop covers, or while another trade has an order whose send outcome is
    unknown."""
    concurrent = 0
    for seed in SEEDS:
        violations, together = two_position_day(tmp_path / f"{seed}.sqlite3", seed)
        assert violations == [], seed
        concurrent += together
    assert concurrent > len(SEEDS)  # second entries really happened next to open trades


def test_a_lost_entry_that_is_cancelled_and_found_absent_does_not_block_entries_forever(
        tmp_path):
    """Place times out before reaching Webull (outcome unknown), the entry wait elapses and
    the engine cancels; Webull answers "order not present" (OrderNotFound), which sets
    cancel-unknown. Detail then finds the order absent and the trade closes; proven absence
    must also clear cancel-unknown, which is not an operator fence and survives restarts, or
    the bot could never enter again."""
    broker = FakeBroker()
    broker.script("unknown-lost")
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    clock = [1_000_000.0]
    engine = Engine(store, broker, CFG, "lost0001", lambda: GateResult(True, "t"),
                    clock=lambda: clock[0])
    engine.recover()
    trade = engine.open_trade("ABC", D(1), D("10.00"), D("9.80"), D(1))
    engine.request_exit(trade, "entry-not-filled-in-time")
    for _ in range(4 * CFG.orders.unknown_absent_reads):
        clock[0] += float(CFG.orders.detail_poll_seconds)
        engine.step()
    assert engine.trade(trade).state is TradeState.CLOSED and broker.positions() == []
    engine.recover()  # a restart
    for name in list(engine.fences()):
        engine.clear_operator_fence(name)
    assert engine.fences() == {}
    store.close()


def test_an_entry_never_follows_a_rejected_stop_until_the_operator_looks(tmp_path):
    """Every stop is refused: the position is exited, entries stay refused (operator fence),
    and the broker sees exactly one entry."""
    broker = AuditedBroker(random.Random(0), refuse_stops=True)
    store = Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0)
    clock = [1_000_000.0]
    engine = Engine(store, broker, CFG, "rej00001", lambda: GateResult(True, "t"),
                    clock=lambda: clock[0])
    engine.recover()
    trade = engine.open_trade("ABC", D(2), D("10.00"), D("9.80"), D(1))
    broker.fill(engine.orders(trade)[0].client_order_id, D(2), D(10))
    for _ in range(40):
        clock[0] += float(CFG.orders.detail_poll_seconds)
        engine.step()
        for cid, order in list(broker.orders.items()):
            if order.order_type == OrderType.MARKET and not order.state.terminal:
                broker.fill(cid, order.quantity - order.filled_quantity, D(10))
        with pytest.raises(EntryRefused):
            engine.open_trade("ABC", D(1), D("10.00"), D("9.80"), D(1))
    assert engine.trade(trade).state is TradeState.CLOSED
    assert any(n.startswith(Fence.STOP_REJECTED) for n in engine.fences())
    assert broker.entries == 1 and broker.violations == []
    store.close()



def test_only_the_f7_probe_builds_stop_limit_orders():
    """Structural: OrderType.STOP_LOSS_LIMIT is sent only by the extended-hours probe; the
    broker adapter only shapes and reads it. The engine never builds one."""
    users = sorted({path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                    if any(isinstance(n, ast.Attribute) and n.attr == "STOP_LOSS_LIMIT"
                           and ast.unparse(n.value) == "OrderType"
                           for n in ast.walk(parsed(path)))})
    assert users == ["broker/webull.py", "engine/session_probe.py"]


def test_only_the_trailing_probe_builds_trailing_stops():
    """Structural: OrderType.TRAILING_STOP_LOSS is sent only by the trailing-stop probe; the
    broker adapter only shapes and reads it. The engine never builds one until T1-T6 answer."""
    users = sorted({path.relative_to(SRC).as_posix() for path in SRC.rglob("*.py")
                    if any(isinstance(n, ast.Attribute) and n.attr == "TRAILING_STOP_LOSS"
                           and ast.unparse(n.value) == "OrderType"
                           for n in ast.walk(parsed(path)))})
    assert users == ["broker/webull.py", "engine/trailing_probe.py"]

"""`tickr demo --record`: the demo day the apps carry with them.

The apps' "Demo day" switch plays a recording instead of talking to an API, so the demo works
anywhere and any time: on the phone away from the Mac, while the bot trades, with the Mac's demo
service stopped. The recording is made here, with the real bot on the simulated exchange
(`tickr.demo.run`, the same code path as `tickr demo`):

1. The past sessions and the demo day up to `[demo] clock` run fast (`run.replay`), in their own
   folder (`<root_dir>-record/`), exactly as for the fixture.
2. The bot starts again at `clock`, as `tickr demo` does, and runs the live loop fast up to the
   end of `[recording] cycle`. Through that cycle, every `[api] poll_seconds` of simulated time
   (the apps' status poll), the bot is held while every API resource the apps read is asked of
   the demo's own API (`api/server.py`, on a free loopback port): status, account, flow,
   watching, observers, skipped, the day and its trades, jobs, journal, settings, config,
   performance, advisor, Lab replay, and per symbol of the day its decisions and bars; once a
   minute its charts in every range and interval the apps offer; once, the past days.
3. Right after that cycle, every command the apps can send (each kind, flatten per symbol) is
   POSTed to the demo's API, one every `[recording] command_seconds`, so the real bot acts on it:
   the API's answer and the bot's record of the outcome are kept, stop-bot last. After the bot
   stops, start-bot, a Lab replay and an advisor decision are asked once (the folder is then
   deleted, so nothing queued is ever run).

The file (`RECORDING`) is bundled in TickrKit (`DemoSource`): xz-compressed ASCII lines, line 1
the index, every further line a piece of an answer. Each route keeps its first answer whole and
every change as ops against the one before (runs of bytes copied from it, and new pieces), cut at
commas, so a ticking clock or an appended candle costs a few bytes, not a document. Its `inputs`
is the fixture digest of the scenario, the demo code, the API contract and the strategies
(`run.fixture_inputs`) it was made from, for reference only: a change of those does not call for a
new recording. tests/demo/test_recording.py fails only when the apps could no longer read it (an
answer that no longer matches today's committed schema, another FORMAT, a strategy that is gone);
.github/workflows/record-demo.yml records it again every night.
"""

from __future__ import annotations

import http.client
import json
import lzma
import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import urlencode

from tickr import config as config_module
from tickr.api.contract import ChartInterval, ChartRange, CommandKind
from tickr.api.server import (
    ADVISOR_DECISION,
    COMMAND_ROUTES,
    ENTRY_STOP_STATES,
    LAB_REPLAY,
    START_BOT,
    make_server,
    settings_from_config,
)
from tickr.calendar import SessionCalendar
from tickr.demo.run import (
    DemoRefused,
    Session,
    _schedule,
    _stop_self,
    demo_root,
    fixture_inputs,
    replay,
    running_lock,
    sessions,
)
from tickr.demo.scenario import Scenario
from tickr.demo.world import World, ny

# Where the apps find the recording, relative to the repo (TickrKit's resources).
RECORDING = Path("apps/shared/Sources/TickrKit/Resources/DemoDay.jsonl.xz")
FORMAT = 1  # version of the file layout; DemoSource refuses another
RECORD_SUFFIX = "-record"  # the recording replays into `<root_dir>-record/`
AT_CLOCK = "data-at-clock"  # inside it: the data folder at `clock`, both runs start from it
NONE = -1  # a POST without a recorded outcome
INSERT = -1  # an op of a change point that inserts a piece (others copy from the answer before)
XZ_PRESET = 9  # xz compression level of the file (the most; it is written once per recording)
SECONDS_PER_MINUTE = 60
OK = 200
ACCEPTED = 202
# Chart ranges and their intervals the apps offer (TickrKit ChartRange.intervals).
CHART_PAIRS = (
    (ChartRange.D1, (ChartInterval.M1, ChartInterval.M5, ChartInterval.M30, ChartInterval.H1)),
    (ChartRange.D5, (ChartInterval.M5, ChartInterval.M30, ChartInterval.H1)),
    (ChartRange.M1, (ChartInterval.D1,)),
    (ChartRange.M3, (ChartInterval.D1, ChartInterval.W1)),
    (ChartRange.Y1, (ChartInterval.D1, ChartInterval.W1, ChartInterval.MO1)),
)


def route(path: str, query: dict[str, str] | None = None) -> str:
    """The key an answer is kept under: path and query items sorted by name, as DemoSource
    builds it from the apps' request."""
    return path + ("?" + urlencode(sorted(query.items())) if query else "")


def post_key(path: str, body: dict[str, str]) -> str:
    """The key a POST answer is kept under: path and its body with sorted keys, compact."""
    return path + " " + json.dumps(body, sort_keys=True, separators=(",", ":"))


def compact(raw: bytes) -> bytes:
    """One line of ASCII JSON, whatever the API's spacing: no newline inside, so the file
    stays one piece per line; the document itself is unchanged."""
    return json.dumps(json.loads(raw), ensure_ascii=True, separators=(",", ":")).encode("ascii")


@dataclass
class Tape:
    """Answers per route, as change points (frame, HTTP status, body)."""

    routes: dict[str, list[tuple[int, int, bytes]]] = field(default_factory=dict)
    posts: dict[str, tuple[int, bytes]] = field(default_factory=dict)
    outcomes: dict[str, bytes] = field(default_factory=dict)  # POST key -> command-record

    def keep(self, frame: int, key: str, status: int, body: bytes) -> None:
        points = self.routes.setdefault(key, [])
        if not points or points[-1][1:] != (status, body):
            points.append((frame, status, body))

    def write(self, path: Path, header: dict[str, object]) -> None:
        """The file: line 1 the index, then the pieces, the whole compressed as xz."""
        pieces: dict[bytes, int] = {}

        def piece(text: bytes) -> int:
            return pieces.setdefault(text, len(pieces))

        routes: dict[str, list[list[int]]] = {}
        for key in sorted(self.routes):
            previous = b""
            out = []
            for frame, status, body in self.routes[key]:
                point = [frame, status]
                for start, size, new in delta(previous, body):
                    point += [start, size] if new is None else [INSERT, piece(new)]
                out.append(point)
                previous = body
            routes[key] = out
        posts = {key: [status, piece(body),
                       piece(self.outcomes[key]) if key in self.outcomes else NONE]
                 for key, (status, body) in sorted(self.posts.items())}
        index = json.dumps({**header, "routes": routes, "posts": posts}, ensure_ascii=True,
                           separators=(",", ":")).encode("ascii")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(lzma.compress(b"\n".join([index, *pieces]) + b"\n",
                                       format=lzma.FORMAT_XZ, preset=XZ_PRESET))


def tokens(body: bytes) -> list[bytes]:
    """`body` cut after every comma: the units a delta keeps or replaces (a field, a candle)."""
    parts = body.split(b",")
    return [p + b"," for p in parts[:-1]] + [parts[-1]]


def delta(old: bytes, new: bytes) -> list[tuple[int, int, bytes | None]]:
    """`new` as (start, size, None) runs copied from `old` and (0, 0, bytes) runs of its own, so
    a clock in the header and a candle at the end cost only themselves."""
    if not old:
        return [(0, 0, new)]
    a, b = tokens(old), tokens(new)
    offsets = [0]
    for t in a:
        offsets.append(offsets[-1] + len(t))
    out: list[tuple[int, int, bytes | None]] = []
    for op, i1, i2, j1, j2 in SequenceMatcher(None, a, b).get_opcodes():
        if op == "equal":
            out.append((offsets[i1], offsets[i2] - offsets[i1], None))
        elif j2 > j1:
            out.append((0, 0, b"".join(b[j1:j2])))
    return out


class Client:
    """Plain HTTP to the demo's own API on its loopback port."""

    def __init__(self, port: int) -> None:
        self.port = port

    def ask(self, method: str, target: str,
            body: dict[str, str] | None = None) -> tuple[int, bytes]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        headers = {"Host": f"127.0.0.1:{self.port}", "Accept": "application/json"}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        try:
            conn.request(method, target, body=data, headers=headers)
            resp = conn.getresponse()
            return resp.status, compact(resp.read())
        finally:
            conn.close()


def record(repo: Path, scenario: Scenario, today: date,
           log: Callable[[str], None] = print) -> Path:
    """Replay the scenario with the real bot and write the apps' recording (`RECORDING`)."""
    plan = scenario.recording
    loop = scenario.live
    if plan is None or loop is None:
        raise DemoRefused("config/demo.toml has no [recording] or no [live] loop to record")
    building = replace(scenario, root_dir=scenario.root_dir + RECORD_SUFFIX)
    with running_lock(repo, building):
        try:
            cfg, day, code = replay(repo, building, today, log)
            if code != 0:
                raise DemoRefused(f"the bot ended the demo day up to its clock with exit code "
                                  f"{code}")
            tape, header = _record_cycle(repo, building, cfg, day, log)
        finally:
            shutil.rmtree(demo_root(repo, building), ignore_errors=True)
    header["inputs"] = fixture_inputs(repo, scenario)
    out = repo / RECORDING
    tape.write(out, header)
    log(f"demo: recording {out} ({out.stat().st_size} bytes) of {day.isoformat()}")
    return out


def _record_cycle(repo: Path, scenario: Scenario, cfg: config_module.Config, day: date,
                  log: Callable[[str], None]) -> tuple[Tape, dict[str, object]]:
    """The bot again from `clock` (as `tickr demo` starts), fast, twice from the same data: once
    sampled through the recorded cycle, once sent every command the apps can send."""
    assert scenario.recording is not None and scenario.live is not None
    cycle = timedelta(minutes=scenario.live.cycle_minutes)
    start = ny(day, scenario.clock) + scenario.recording.cycle * cycle
    poll = int(cfg.api.poll_seconds)
    if poll <= 0 or cycle.total_seconds() % poll:
        raise DemoRefused("[api] poll_seconds must divide [live] cycle_minutes")
    frames = int(cycle.total_seconds()) // poll
    tape = Tape()
    data = cfg.root / cfg.run.data_dir
    at_clock = cfg.root / AT_CLOCK
    shutil.copytree(data, at_clock)

    def frames_run(world: World, client: Client) -> Callable[[], None]:
        sampler = Sampler(client, tape, scenario, day)
        for frame in range(frames):
            world.at(start + timedelta(seconds=frame * poll),
                     lambda frame=frame: sampler.sample(frame, frame * poll))
        world.at(start + cycle, _stop_self)

        def done() -> None:
            if sampler.taken != frames:
                raise DemoRefused(f"the bot stopped after {sampler.taken} of {frames} frames")
            held.update(sampler.held)
        return done

    held: dict[str, int] = {}
    log(f"demo: recording cycle {scenario.recording.cycle} of {day.isoformat()} "
        f"({frames} frames of {poll} s)")
    _from_clock(scenario, cfg, day, frames_run)
    if not held:
        raise DemoRefused("the recorded cycle never holds a position: nothing to flatten")
    shutil.rmtree(data)
    shutil.copytree(at_clock, data)

    def commands_run(world: World, client: Client) -> Callable[[], None]:
        sampler = Sampler(client, tape, scenario, day)
        step = timedelta(seconds=scenario.recording.command_seconds)
        plan = sampler.command_plan(held, start, cycle, step)
        for when, path, body in plan:
            world.at(when, lambda path=path, body=body: sampler.send(path, body))
        world.at(plan[-1][0] + step, sampler.collect)
        world.at(plan[-1][0] + step, _stop_self)

        def done() -> None:
            sampler.collect()  # stop-bot's outcome, written as the bot stopped
            if len(sampler.sent) != len(plan):
                raise DemoRefused(f"the bot stopped after {len(sampler.sent)} of {len(plan)} "
                                  "commands")
            sampler.posts()
        return done

    log("demo: recording what the bot does with each command the apps can send")
    _from_clock(scenario, cfg, day, commands_run)
    header: dict[str, object] = {
        "format": FORMAT, "demo_day": day.isoformat(),
        "start": start.isoformat().replace("+00:00", "Z"), "frame_seconds": poll,
        "frames": frames, "environment": scenario.environment}
    return tape, header


def _from_clock(scenario: Scenario, cfg: config_module.Config, day: date,
                arrange: Callable[[World, Client], Callable[[], None]]) -> None:
    """The real bot from `clock` on the data folder as it is, with the demo's API on a free
    loopback port; `arrange` queues its events and returns what to check once the bot stopped
    (the API still answering)."""
    config_path = cfg.root / "config" / "bot.toml"
    calendar = SessionCalendar.load((cfg.root / cfg.session.calendar_file).parent)
    days = sessions(calendar, day, 1 + cfg.market_data.history_sessions)
    session = Session(config_path, scenario, calendar, day, days[:-1], True)
    world = session.world(scenario.today, scenario.listed, start=scenario.clock)
    server = make_server(replace(settings_from_config(cfg), port=0, config_file=config_path),
                         world.clock)
    # Tens of thousands of reads: no access log line for each.
    server.RequestHandlerClass = type("Quiet", (server.RequestHandlerClass,),
                                      {"log_message": lambda *args: None})
    server.chart.use(world.feed)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    try:
        done = arrange(world, Client(server.server_port))
        _schedule(session, world, cfg, after=scenario.clock)
        world.sleep = _through_events(world)  # type: ignore[method-assign]
        code = session.run(world)
        if code != 0:
            raise DemoRefused(f"the bot ended a recorded run with exit code {code}")
        done()
    finally:
        server.shutdown()
        server.server_close()


def _through_events(world: World) -> Callable[[float], None]:
    """The world's fast sleep, stopping at every queued event on its way: each frame is taken at
    its own simulated second, even when the bot sleeps across several frames at once."""

    def sleep(seconds: float) -> None:
        if world.events and world.events[0][0] <= world.now:
            world.advance(0)  # due at this very second (the cycle starts as the bot does)
        left = seconds
        while left > 0:
            step = left
            if world.events:
                due = (world.events[0][0] - world.now).total_seconds()
                if 0 < due < left:
                    step = due
            world.advance(step)
            left -= step

    return sleep


class Sampler:
    def __init__(self, client: Client, tape: Tape, scenario: Scenario, day: date) -> None:
        assert scenario.recording is not None
        self.client = client
        self.tape = tape
        self.day = day.isoformat()
        self.symbols = [p.symbol for p in scenario.today]
        self.performance_from = (day - timedelta(days=scenario.recording.performance_days - 1))
        self.taken = 0
        self.sent: dict[str, str] = {}  # command id -> POST key
        self.held: dict[str, int] = {}  # symbol -> second of the cycle it is first held at

    def get(self, frame: int, path: str, query: dict[str, str] | None = None) -> bytes:
        key = route(path, query)
        status, body = self.client.ask("GET", key)
        self.tape.keep(frame, key, status, body)
        return body

    def sample(self, frame: int, second: int) -> None:
        """Every route the apps poll, at simulated second `second` of the cycle (bot held)."""
        d = {"date": self.day}
        document = json.loads(self.get(frame, "/status"))["status"]
        for position in document["positions"] if document else ():
            self.held.setdefault(position["symbol"], second)
        for path in ("/account", "/commands/pending", "/jobs", "/journal",
                     "/settings", "/config", "/advisor", "/lab/replay", "/days",
                     f"/days/{self.day}"):
            self.get(frame, path)
        for path in ("/flow", "/watching", "/observers", "/skipped", "/symbols", "/movers"):
            self.get(frame, path, d)
        self.get(frame, "/performance", {"from": self.performance_from.isoformat(),
                                         "to": self.day})
        for symbol in self.symbols:
            self.get(frame, "/decisions", {"symbol": symbol, **d})
            self.get(frame, "/bars", {"symbol": symbol, **d})
        if second % SECONDS_PER_MINUTE == 0:
            for symbol in self.symbols:
                for span, intervals in CHART_PAIRS:
                    for interval in intervals:
                        self.get(frame, "/chart", {"symbol": symbol, "interval": interval.value,
                                                   "range": span.value})
        if frame == 0:
            self.past_days()
        self.taken += 1

    def past_days(self) -> None:
        """The days before the demo day: their documents and the bars of their trades."""
        listed = json.loads(self.get(0, "/days"))["days"]
        for entry in listed:
            when = entry["day"]
            if when == self.day:
                continue
            document = json.loads(self.get(0, f"/days/{when}"))
            symbols = sorted({t["symbol"] for t in document["trades"]}
                             | set(document["bar_symbols"]))
            for symbol in symbols:
                self.get(0, "/bars", {"symbol": symbol, "date": when})
                self.get(0, "/decisions", {"symbol": symbol, "date": when})

    def command_plan(self, held: dict[str, int], start: datetime, cycle: timedelta,
                     step: timedelta) -> list[tuple[datetime, str, dict[str, str]]]:
        """When to send each command the apps can send (TickrKit OperatorCommand), so each meets
        the state it is shown in: from the cycle's start (flat) the entry stop, cancel and the
        flatten of each symbol the cycle never holds; the flatten of each held symbol a step after
        it is first held (as in the recorded run, which this one repeats until then); in the
        next cycle, once its first position is held, flatten all, emergency and stop-bot last."""
        flatten = next(p for p, k in COMMAND_ROUTES.items() if k is CommandKind.FLATTEN)
        early: list[tuple[str, dict[str, str]]] = []
        late: list[tuple[str, dict[str, str]]] = []
        for path, kind in COMMAND_ROUTES.items():
            if kind is None:  # entry stop: the kind is in the body
                early += [(path, {"state": state}) for state in ENTRY_STOP_STATES]
            elif kind is CommandKind.FLATTEN:
                early += [(path, {"symbol": s}) for s in self.symbols if s not in held]
                late.insert(0, (path, {}))
            elif kind is CommandKind.STOP_BOT:
                late.append((path, {}))
            elif kind is CommandKind.EMERGENCY:
                late.insert(1, (path, {}))
            else:
                early.insert(0, (path, {}))
        plan = [(start + n * step, path, body) for n, (path, body) in enumerate(early)]
        plan += [(start + timedelta(seconds=second) + step, flatten, {"symbol": symbol})
                 for symbol, second in held.items()]
        first = start + cycle + timedelta(seconds=min(held.values())) + step
        plan += [(first + n * step, path, body) for n, (path, body) in enumerate(late)]
        return sorted(plan, key=lambda p: p[0])

    def send(self, path: str, body: dict[str, str]) -> None:
        """POST one command (bot held); its outcome is collected once the bot reports it."""
        self.collect()
        key = post_key(path, body)
        status, answer = self.client.ask("POST", path, body)
        self.tape.posts[key] = (status, answer)
        if status == ACCEPTED:
            self.sent[json.loads(answer)["command"]["command_id"]] = key

    def collect(self) -> None:
        """The outcomes the bot has reported of the commands sent so far."""
        status, body = self.client.ask("GET", "/status")
        if status != OK or not self.sent:
            return
        document = json.loads(body)["status"]
        for record in document["recent_commands"] if document else ():
            key = self.sent.get(record["command_id"])
            if key is not None:
                self.tape.outcomes[key] = json.dumps(record, ensure_ascii=True,
                                                     separators=(",", ":")).encode("ascii")

    def posts(self) -> None:
        """Every other POST the apps can send, asked once after the bot stopped."""
        for path, body in ((START_BOT, {}), (LAB_REPLAY, {}), (ADVISOR_DECISION, {})):
            status, answer = self.client.ask("POST", path, body)
            self.tape.posts[post_key(path, body)] = (status, answer)

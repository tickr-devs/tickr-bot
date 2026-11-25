"""`tickr demo --record`: the demo day the apps carry (TickrKit DemoDay.jsonl)."""

from __future__ import annotations

import json
import lzma
from dataclasses import replace
from datetime import date
from decimal import Decimal as D
from pathlib import Path

import pytest
from api.schema_check import errors  # tests/ is on the path: each test package sits in it

from tickr.demo import record as rec
from tickr.demo import run as demo
from tickr.demo.scenario import LiveLoop, Recording, ScenarioError, load

from .test_demo import HOT, SUNDAY, repo, small

ROOT = Path(__file__).resolve().parents[2]


def read(path: Path) -> tuple[dict, list[bytes]]:
    lines = lzma.decompress(path.read_bytes()).split(b"\n")
    assert lines[-1] == b""  # ends with a newline
    return json.loads(lines[0]), lines[1:-1]


def answer(index: dict, pieces: list[bytes], key: str, frame: int) -> tuple[int, bytes]:
    """What DemoSource (Swift) serves for `key` at `frame`: the deltas applied in order."""
    body, status = b"", 0
    points = index["routes"][key]
    for at, code, *ops in points:
        if at > frame and at != points[0][0]:
            break
        pairs = zip(ops[::2], ops[1::2], strict=True)
        body = b"".join(pieces[second] if first == rec.INSERT else body[first:first + second]
                        for first, second in pairs)
        status = code
    return status, body


# Which committed schema (src/tickr/api/schema/) each recorded answer must match: a GET by its
# path, a POST by its path; any answer that is not 2xx is an error document.
GET_SCHEMAS = {
    "/status": "status-response-v1", "/account": "account-response-v1", "/days": "days-v1",
    "/bars": "bars-v1", "/chart": "chart-v1", "/decisions": "decisions-v1", "/flow": "flow-v1",
    "/watching": "watching-v1", "/observers": "observers-v1", "/skipped": "skipped-v1",
    "/jobs": "jobs-v1", "/journal": "journal-v1",
    "/events": "day-events-v1", "/symbols": "symbols-v1", "/movers": "movers-v1",
    "/settings": "settings-v1",
    "/config": "config-v1", "/commands/pending": "pending-commands-v1",
    "/performance": "performance-v1", "/advisor": "advisor-runs-v1",
    "/lab/replay": "lab-replay-v1"}
POST_SCHEMAS = {"/commands/start-bot": "bot-start-v1", "/lab/replay": "lab-replay-v1",
                "/advisor/decision": "advisor-runs-v1"}
COMMAND_SCHEMA = "command-accepted-v1"  # every other POST is a /commands/... route
ERROR_SCHEMA = "error-v1"
STALE = ("the apps' demo day no longer matches the API contract: run `tickr demo --record` and "
         f"commit {rec.RECORDING} (docs/demo.md)")


def committed_schema(name: str) -> dict:
    return json.loads((ROOT / "src" / "tickr" / "api" / "schema" / f"{name}.schema.json")
                      .read_text("utf-8"))


def versions(index: dict, pieces: list[bytes], key: str) -> list[tuple[int, int, bytes]]:
    """Every distinct answer of `key`: (frame, status, body) at each of its change points."""
    return [(at, *answer(index, pieces, key, at)) for at, *_ in index["routes"][key]]


def get_schema(key: str, status: int) -> str:
    path = key.split("?")[0]
    if not 200 <= status < 300:
        return ERROR_SCHEMA
    return "day-v1" if path.startswith("/days/") else GET_SCHEMAS[path]


def post_schema(key: str, status: int) -> str:
    path = key.split(" ")[0]
    if not 200 <= status < 300:
        return ERROR_SCHEMA
    return POST_SCHEMAS.get(path, COMMAND_SCHEMA)


def strategies_in(value: object) -> set[str]:
    """Names of the strategies a document shows (every `strategy` object with a name)."""
    if isinstance(value, list):
        return set().union(*map(strategies_in, value))
    if not isinstance(value, dict):
        return set()
    found = set().union(*map(strategies_in, value.values()))
    strategy = value.get("strategy")
    if isinstance(strategy, dict) and isinstance(strategy.get("name"), str):
        found.add(strategy["name"])
    return found


def test_the_committed_recording_is_readable_with_todays_contract():
    """The recording is a sample day for the apps: it need not be made again on every change of
    the scenario, the demo code, the contract or the strategies, only when the apps could no
    longer read it. Every distinct recorded answer (each change point of each route, and every
    POST with the bot's outcome) must match today's committed schema; the file layout must be
    the one DemoSource reads, and the strategies it shows must still exist."""
    index, pieces = read(ROOT / rec.RECORDING)
    assert index["format"] == rec.FORMAT, STALE
    problems: list[str] = []
    shown: set[str] = set()
    for key in index["routes"]:
        for frame, status, body in versions(index, pieces, key):
            document = json.loads(body)
            name = get_schema(key, status)
            problems += [f"{key} @{frame} ({name}): {e}"
                         for e in errors(committed_schema(name), document)]
            shown |= strategies_in(document)
    outcome_schema = committed_schema("status-v1")["properties"]["recent_commands"]["items"]
    for key, (status, piece, outcome) in index["posts"].items():
        name = post_schema(key, status)
        problems += [f"POST {key} ({name}): {e}"
                     for e in errors(committed_schema(name), json.loads(pieces[piece]))]
        if outcome != rec.NONE:
            problems += [f"POST {key} outcome (recent command): {e}"
                         for e in errors(outcome_schema, json.loads(pieces[outcome]))]
    gone = sorted(s for s in shown if not (ROOT / "strategies" / s / "strategy.toml").is_file())
    if gone:
        problems.append(f"strategies no longer in strategies/: {gone}")
    assert not problems, STALE + "\n" + "\n".join(problems[:20])


def test_the_committed_recording_holds_the_whole_cycle_and_every_screen():
    index, pieces = read(ROOT / rec.RECORDING)
    scenario = load(ROOT / "config" / "demo.toml")
    assert index["format"] == rec.FORMAT
    assert index["environment"] == scenario.environment
    assert index["frames"] * index["frame_seconds"] == scenario.live.cycle_minutes * 60
    day = index["demo_day"]
    for key in ("/status", "/account", "/days", f"/days/{day}", f"/flow?date={day}",
                f"/watching?date={day}", f"/observers?date={day}", f"/skipped?date={day}",
                "/jobs", "/journal", "/settings", "/config", "/commands/pending"):
        status, body = answer(index, pieces, key, index["frames"] - 1)
        assert status == 200 and json.loads(body)["api_version"] == 1, key
    for plan in scenario.today:
        assert answer(index, pieces, rec.route("/chart", {
            "symbol": plan.symbol, "interval": "1m", "range": "1d"}), 0)[0] == 200
    status = [json.loads(answer(index, pieces, "/status", f)[1]) for f in range(index["frames"])]
    assert len({s["served_at"] for s in status}) == index["frames"]  # a new answer every frame
    assert any(s["status"]["positions"] for s in status)  # the loop holds a position ...
    # ... and its seam is flat: the apps play the cycle in a loop, so it starts and ends with
    # no position ([live] in config/demo.toml is written for that).
    assert not status[0]["status"]["positions"] and not status[-1]["status"]["positions"]
    kinds = {key.split(" ")[0] for key in index["posts"]}
    assert {"/commands/flatten", "/commands/entry-stop", "/commands/stop-bot",
            "/commands/emergency", "/commands/cancel-own-orders", "/commands/start-bot"} <= kinds


def test_tape_deltas_round_trip(tmp_path):
    tape = rec.Tape()
    versions = [b'{"a":1,"bars":[1]}', b'{"a":1,"bars":[1,2]}', b'{"a":2,"bars":[1,2]}',
                b'{"a":2,"bars":[1,2]}', b"{}", b'{"a":2,"bars":[1,2,3]}']
    for frame, body in enumerate(versions):
        tape.keep(frame, "/x", 200 if body != b"{}" else 404, body)
    tape.keep(0, "/static", 200, b'{"s":1}')
    path = tmp_path / "rec.jsonl"
    tape.write(path, {"format": rec.FORMAT})
    index, pieces = read(path)
    assert [p[0] for p in index["routes"]["/x"]] == [0, 1, 2, 4, 5]  # frame 3 repeats frame 2
    for frame, body in enumerate(versions):
        assert answer(index, pieces, "/x", frame)[1] == body
    assert answer(index, pieces, "/x", 4)[0] == 404
    assert answer(index, pieces, "/static", 5) == (200, b'{"s":1}')


def test_route_keys_sort_the_query_like_the_apps():
    assert rec.route("/decisions", {"symbol": "MVLD", "date": "2026-11-13"}) == \
        "/decisions?date=2026-11-13&symbol=MVLD"
    assert rec.route("/status") == "/status"
    assert rec.post_key("/commands/entry-stop", {"state": "on"}) == \
        '/commands/entry-stop {"state":"on"}'


def test_the_recording_table_is_checked(tmp_path):
    text = (ROOT / "config" / "demo.toml").read_text("utf-8")
    path = tmp_path / "demo.toml"
    path.write_text(text.replace("cycle = 1\n", "cycle = 99\n"), "utf-8")
    with pytest.raises(ScenarioError, match="recording"):
        load(path)


def test_record_runs_the_real_bot_and_writes_what_the_apps_read(tmp_path):
    """A short day recorded over a five-minute cycle (one symbol stopped out before the clock,
    one bought on its breakout inside the cycle): every frame of every route, what the bot did
    with every command, and nothing left behind."""
    root = repo(tmp_path)
    scenario = small(root / "config" / "demo.toml")
    aaa = replace(scenario.today[0], legs=(("09:30", "09:45", D("24.50"), HOT),
                                           ("09:46", "09:48", D(21), HOT)))
    bbb = replace(scenario.today[1], legs=(("09:40", "10:00", D("34.50"), HOT),))
    scenario = replace(scenario, past=(), today=(aaa, bbb), commands=(), clock="09:48",
                       live_until="10:05", listed=(),
                       live=LiveLoop(5, 1, (), ()), recording=Recording(0, 30, 5))
    lines: list[str] = []
    out = rec.record(root, scenario, SUNDAY, lines.append)
    assert out == root / rec.RECORDING
    index, pieces = read(out)
    assert index["demo_day"] == date(2026, 11, 13).isoformat()
    assert index["frames"] == 5 * 60 // index["frame_seconds"]
    assert index["start"] == "2026-11-13T14:48:00Z"
    assert index["inputs"] == demo.fixture_inputs(root, scenario)
    day = index["demo_day"]
    status, body = answer(index, pieces, "/status", 0)
    assert status == 200 and json.loads(body)["status"]["environment"] == scenario.environment
    status, body = answer(index, pieces, f"/days/{day}", 0)
    assert [t["symbol"] for t in json.loads(body)["trades"]] == ["AAA"]
    assert answer(index, pieces, rec.route("/chart", {"symbol": "AAA", "interval": "1d",
                                                      "range": "1y"}), 0)[0] == 200
    served = {json.loads(answer(index, pieces, "/status", f)[1])["served_at"]
              for f in range(index["frames"])}
    assert len(served) == index["frames"]
    flatten = index["posts"]['/commands/flatten {"symbol":"AAA"}']
    assert flatten[0] == 202
    assert json.loads(pieces[flatten[1]])["command"]["kind"] == "flatten"
    # Every command the apps can send was acted on by the real bot, its outcome kept.
    commands = {k: v for k, v in index["posts"].items() if not k.startswith(
        "/commands/start-bot") and k.startswith("/commands/")}
    # entry stop on/off, cancel, flatten all, stop, emergency, flatten AAA and BBB
    assert len(commands) == 6 + 2
    for key, (status, answer_piece, outcome) in commands.items():
        accepted = json.loads(pieces[answer_piece])["command"]
        assert status == 202 and outcome != rec.NONE, key
        record = json.loads(pieces[outcome])
        assert record["command_id"] == accepted["command_id"]
        assert record["outcome"] in ("done", "refused")
    # Each flatten met the state it is shown in: BBB held (done), AAA not (refused).
    outcome = {k: json.loads(pieces[v[2]]) for k, v in commands.items()}
    assert outcome['/commands/flatten {"symbol":"BBB"}']["outcome"] == "done"
    assert outcome['/commands/flatten {"symbol":"AAA"}']["outcome"] == "refused"
    assert outcome["/commands/flatten {}"]["outcome"] == "done"
    assert not (root / (scenario.root_dir + rec.RECORD_SUFFIX)).exists()

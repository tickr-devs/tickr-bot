from __future__ import annotations

import json
import shutil
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from aperture import config as config_module
from aperture.advisor.__main__ import main
from aperture.advisor.client import AdvisorReply, AnthropicClient
from aperture.advisor.errors import AdvisorError
from aperture.advisor.proposal import SCHEMA, next_version, parse_reply
from aperture.advisor.run import Status, day_files, run_advisor
from aperture.observers.log import DecisionLog
from aperture.replay.settings import load_settings
from aperture.research.records import Admission, DecisionRecord
from aperture.research.settings import load_research_settings
from aperture.strategy.base import Check, Decision, Role, Verdict

ROOT = Path(__file__).resolve().parent.parent.parent
NY = ZoneInfo("America/New_York")
M1 = timedelta(minutes=1)
DAYS = [date(2026, 11, 16), date(2026, 11, 17)]
NOW = datetime(2026, 11, 18, 1, 0, tzinfo=UTC)
BASE = tomllib.loads((ROOT / "strategies/price-action-v1/strategy.toml").read_text())


def answer(changes=(), no_change="", summary="s"):
    return json.dumps({
        "summary": summary,
        "changes": [
            {"section": s, "key": k, "new_value": v, "reason": f"why {k}", "evidence": "n=12"}
            for s, k, v in changes
        ],
        "expected_effect": "fewer early entries",
        "risks": ["fewer trades"],
        "no_change_reason": no_change,
    })


class FakeClient:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def propose(self, system, user, schema):
        self.calls.append((system, user, schema))
        return AdvisorReply(self.text, "claude-opus-5-5")


def _bars_line(symbol, start, price):
    p = str(price)
    return json.dumps({"symbol": symbol, "start": start.isoformat(), "open": p, "high": p,
                       "low": p, "close": p, "volume": "1000"})


def make_root(tmp_path, min_days=2, extra=""):
    shutil.copytree(ROOT / "strategies", tmp_path / "strategies")
    shutil.copytree(ROOT / "config", tmp_path / "config")
    bot = tmp_path / "config" / "bot.toml"
    text = bot.read_text().replace("min_days = 5", f"min_days = {min_days}")
    bot.write_text(text + extra)
    cfg = config_module.load(bot)
    log = DecisionLog(tmp_path / cfg.run.data_dir / cfg.observers.decisions_dir
                      / cfg.environment_name)
    for day in DAYS:
        first = datetime.combine(day, time(10, 0), NY).astimezone(UTC)
        _, bars = day_files(cfg, day)
        bars.parent.mkdir(parents=True)
        bars.write_text("\n".join(_bars_line("ABC", first + i * M1, Decimal("10") + i)
                                  for i in range(10)) + "\n")
        check = Check("relative_volume", Decimal("1.2"), ">=", Decimal("2.0"), False)
        for i, verdict in enumerate((Verdict.IGNORE, Verdict.WATCH, Verdict.ENTER)):
            start = first + i * M1
            extra_fields = {}
            admission, cause = Admission.NOT_APPLICABLE, None
            if verdict is Verdict.ENTER:
                extra_fields = dict(entry_low=Decimal(12), entry_high=Decimal("12.1"),
                                    stop=Decimal("11.5"), quantity=Decimal(10))
                admission, cause = Admission.REFUSED, "daily loss ceiling"
            decision = Decision("price-action-v1", "1", "ABC", verdict,
                                (check,) if verdict is not Verdict.ENTER else (), **extra_fields)
            log.append(DecisionRecord(start + M1, start, Role.ACTIVE, decision, admission, cause))
    log.close()
    return cfg


def run(cfg, client, tmp_path, **kw):
    return run_advisor(cfg, DAYS, client,
                       load_research_settings(ROOT / "config" / "research.toml"),
                       load_settings(ROOT / "config" / "replay.toml"), NOW,
                       out_dir=tmp_path / "out", **kw)


# ------------------------------------------------------------------ run


def test_proposal_becomes_a_new_version_folder_and_report(tmp_path):
    cfg = make_root(tmp_path)
    client = FakeClient(answer([("breakout", "lookback_bars", "8")]))
    result = run(cfg, client, tmp_path)
    assert result.status is Status.PROPOSED
    assert result.proposal_folder.name == "price-action-v1-v2"
    text = (result.proposal_folder / "strategy.toml").read_text()
    values = tomllib.loads(text)
    assert values["strategy"] == {"name": "price-action-v1", "version": "2"}
    assert values["breakout"]["lookback_bars"] == 8
    rest = {k: v for k, v in values.items() if k not in ("strategy", "breakout")}
    assert rest == {k: v for k, v in BASE.items() if k not in ("strategy", "breakout")}
    assert text.startswith("# PROPOSAL by the AI advisor (claude-opus-5-5)")
    assert "# advisor proposal: was 5. why lookback_bars" in text
    assert "# Prior completed M1 bars whose highest high" in text  # base comments kept
    report = (result.folder / "report.md").read_text()
    assert "Proposed new version" in report and "| `[breakout] lookback_bars` | 5 | 8 |" in report
    assert "Not applied" in report
    assert "tuned on 2026-11-16, 2026-11-17. Judge it only on days after 2026-11-17" in report
    assert "# Tuned on (hold-out rule: judge it only on other days): " \
           "2026-11-16, 2026-11-17." in text
    assert not (tmp_path / "strategies" / "price-action-v1-v2").exists()  # never applied
    system, user, schema = client.calls[0]
    assert schema is SCHEMA and "At most 3" in system
    assert "lookback_bars = 5" in user and '"enter_refusal_causes"' in user
    evidence = json.loads((result.folder / "evidence.json").read_text())
    assert evidence["verdicts"] == {"ENTER": 2, "IGNORE": 2, "WATCH": 2}
    assert evidence["first_failed_check_on_ignore_and_watch"] == {"relative_volume": 4}
    assert evidence["enter_refusal_causes"] == {"daily loss ceiling": 2}
    assert evidence["days"] == ["2026-11-16", "2026-11-17"]


def test_no_change_answer_writes_only_a_report(tmp_path):
    cfg = make_root(tmp_path)
    result = run(cfg, FakeClient(answer(no_change="two days is too little")), tmp_path)
    assert result.status is Status.NO_CHANGE and result.proposal_folder is None
    assert "two days is too little" in (result.folder / "report.md").read_text()


def test_value_refused_by_the_strategy_refuses_the_proposal(tmp_path):
    cfg = make_root(tmp_path)
    bad = answer([("indicators", "fast_ema_period", "30")])  # above slow_ema_period = 20
    result = run(cfg, FakeClient(bad), tmp_path)
    assert result.status is Status.REFUSED and "fast_ema_period" in result.cause
    assert "Proposal refused" in (result.folder / "report.md").read_text()


def test_invalid_answer_is_refused_with_report(tmp_path):
    cfg = make_root(tmp_path)
    result = run(cfg, FakeClient(answer([("strategy", "version", '"9"')])), tmp_path)
    assert result.status is Status.REFUSED
    assert (result.folder / "response.json").is_file()


@pytest.mark.parametrize(
    ("kwargs", "cause"),
    [
        ({"min_days": 3}, "min_days is 3"),
        ({"extra": ""}, None),
    ],
)
def test_min_days(tmp_path, kwargs, cause):
    cfg = make_root(tmp_path, **kwargs)
    if cause is None:
        assert run(cfg, FakeClient(answer(no_change="x")), tmp_path).status is Status.NO_CHANGE
    else:
        with pytest.raises(AdvisorError, match=cause):
            run(cfg, FakeClient(answer()), tmp_path)


def test_missing_day_files_and_oversized_evidence_are_refused(tmp_path):
    cfg = make_root(tmp_path)
    decisions, _ = day_files(cfg, DAYS[0])
    decisions.unlink()
    with pytest.raises(AdvisorError, match="missing"):
        run(cfg, FakeClient(answer()), tmp_path)


def test_archived_day_files_are_read(tmp_path):
    from aperture.archive import compress
    cfg = make_root(tmp_path)
    for day in DAYS:
        for path in day_files(cfg, day):
            compress(path)
    result = run(cfg, FakeClient(answer([("breakout", "lookback_bars", "8")])), tmp_path)
    assert result.status is Status.PROPOSED


def test_evidence_too_big_is_refused_not_cut(tmp_path):
    cfg = make_root(tmp_path)
    from dataclasses import replace
    small = replace(cfg, advisor=replace(cfg.advisor, max_evidence_bytes=100))
    client = FakeClient(answer())
    with pytest.raises(AdvisorError, match="max_evidence_bytes is 100"):
        run(small, client, tmp_path)
    assert client.calls == []


def test_other_strategy_folder_without_decisions_is_refused(tmp_path):
    cfg = make_root(tmp_path)
    src = tmp_path / "strategies" / "price-action-v1" / "strategy.toml"
    (tmp_path / "strategies" / "pa2").mkdir()
    (tmp_path / "strategies" / "pa2" / "strategy.toml").write_text(
        src.read_text().replace('version = "1"', 'version = "2"'))
    with pytest.raises(AdvisorError, match="no recorded active decisions of price-action-v1 v2"):
        run(cfg, FakeClient(answer()), tmp_path, folder="pa2")


def test_cli_with_fake_client(tmp_path, capsys):
    make_root(tmp_path)
    code = main(["--days", *[d.isoformat() for d in DAYS],
                 "--config", str(tmp_path / "config" / "bot.toml"),
                 "--research-settings", str(ROOT / "config" / "research.toml"),
                 "--replay-settings", str(ROOT / "config" / "replay.toml"),
                 "--out", str(tmp_path / "out")],
                client_factory=lambda settings: FakeClient(answer([("breakout", "lookback_bars",
                                                                    "6")])))
    assert code == 0 and capsys.readouterr().out.startswith("proposed: ")
    code = main(["--days", "2026-11-16", "--config", str(tmp_path / "config" / "bot.toml"),
                 "--out", str(tmp_path / "out")], client_factory=lambda s: FakeClient(answer()))
    assert code == 2 and "advisor refused" in capsys.readouterr().err


# ------------------------------------------------------------------ parse_reply


@pytest.mark.parametrize(
    ("changes", "cause"),
    [
        ([("breakout", "lookback_bars", "8.5")], "is a float, current is integer"),
        ([("strategy", "version", '"2"')], "not the model's to change"),
        ([("breakout", "nope", "1")], "no such key"),
        ([("breakout", "lookback_bars", "5")], "equals the current value"),
        ([("breakout", "lookback_bars", "eight")], "not a TOML value"),
        ([("breakout", "lookback_bars", "6"), ("breakout", "lookback_bars", "7")],
         "changed twice"),
        ([("breakout", "lookback_bars", "6"), ("continuation", "impulse_bars", "3"),
          ("continuation", "base_bars", "4"), ("entry", "entry_extension_max_atr", "0.8")],
         "at most 3 allowed"),
    ],
)
def test_bad_changes_refuse_the_proposal(changes, cause):
    with pytest.raises(AdvisorError, match=cause):
        parse_reply(answer(changes), BASE, 3)


def test_unparseable_or_empty_answers():
    with pytest.raises(AdvisorError, match="not JSON"):
        parse_reply("{", BASE, 3)
    with pytest.raises(AdvisorError, match="schema"):
        parse_reply('{"summary": "x"}', BASE, 3)
    with pytest.raises(AdvisorError, match="no no_change_reason"):
        parse_reply(answer(), BASE, 3)


def test_float_and_bool_values_keep_their_type():
    proposal = parse_reply(answer([("indicators", "minimum_relative_volume", "2.5")]), BASE, 3)
    assert proposal.changes[0].new == 2.5


def test_next_version_counts_every_folder_of_the_strategy(tmp_path):
    shutil.copytree(ROOT / "strategies", tmp_path / "s")
    assert next_version(tmp_path / "s", "price-action-v1") == "2"
    (tmp_path / "s" / "x").mkdir()
    (tmp_path / "s" / "x" / "strategy.toml").write_text(
        '[strategy]\nname = "price-action-v1"\nversion = "7"\n')
    assert next_version(tmp_path / "s", "price-action-v1") == "8"
    with pytest.raises(AdvisorError, match="no strategy folder"):
        next_version(tmp_path / "s", "other")


# ------------------------------------------------------------------ Anthropic client


class _Err(Exception):
    def __init__(self, message="err", status_code=500):
        super().__init__(message)
        self.message, self.status_code = message, status_code


@dataclass
class FakeSdk:
    message: SimpleNamespace
    raises: Exception | None = None

    def __post_init__(self):
        self.calls = []
        sdk = self

        class Messages:
            @contextmanager
            def stream(self, **kwargs):
                sdk.calls.append(kwargs)
                if sdk.raises:
                    raise sdk.raises
                yield SimpleNamespace(get_final_message=lambda: sdk.message)

        class Client:
            def __init__(self, timeout):
                sdk.timeout = timeout
                self.beta = SimpleNamespace(messages=Messages())

        self.Anthropic = Client
        self.AuthenticationError = type("AuthenticationError", (_Err,), {})
        self.RateLimitError = type("RateLimitError", (_Err,), {})
        self.APIStatusError = _Err
        self.APIConnectionError = type("APIConnectionError", (Exception,), {})


def _message(stop="end_turn", text='{"a": 1}'):
    content = [SimpleNamespace(type="thinking"), SimpleNamespace(type="text", text=text)]
    return SimpleNamespace(stop_reason=stop, content=content, model="claude-opus-5-5")


def _settings():
    return config_module.load(ROOT / "config" / "bot.toml").advisor


def test_client_request_shape():
    sdk = FakeSdk(_message())
    client = AnthropicClient(_settings(), environ={"ANTHROPIC_API_KEY": "x"}, sdk=sdk)
    reply = client.propose("sys", "usr", SCHEMA)
    assert reply == AdvisorReply('{"a": 1}', "claude-opus-5-5")
    (call,) = sdk.calls
    assert call["model"] == "claude-opus-5-5" and call["fallbacks"] == "default"
    assert call["thinking"] == {"type": "adaptive"}
    assert call["output_config"]["format"] == {"type": "json_schema", "schema": SCHEMA}
    assert call["output_config"]["effort"] == "high"
    assert call["messages"] == [{"role": "user", "content": "usr"}] and call["system"] == "sys"
    assert sdk.timeout == 900.0


@pytest.mark.parametrize(
    ("message", "raises", "cause"),
    [
        (_message("refusal"), None, "declined"),
        (_message("max_tokens"), None, "max_output_tokens"),
        (SimpleNamespace(stop_reason="end_turn", content=[], model="m"), None, "no text"),
        (_message(), "rate", "rate limit"),
        (_message(), "status", "API error 500"),
    ],
)
def test_client_failures_are_typed(message, raises, cause):
    sdk = FakeSdk(message)
    if raises == "rate":
        sdk.raises = sdk.RateLimitError()
    elif raises == "status":
        sdk.raises = _Err("boom", 500)
    client = AnthropicClient(_settings(), environ={"ANTHROPIC_API_KEY": "x"}, sdk=sdk)
    with pytest.raises(AdvisorError, match=cause):
        client.propose("s", "u", SCHEMA)


def test_client_checks_only_the_key_name():
    from dataclasses import replace
    with pytest.raises(AdvisorError, match="ANTHROPIC_API_KEY is not set"):
        AnthropicClient(_settings(), environ={}, sdk=FakeSdk(_message()))
    with pytest.raises(AdvisorError, match="must be ANTHROPIC_API_KEY"):
        AnthropicClient(replace(_settings(), api_key_variable="MY_KEY"),
                        environ={"MY_KEY": "x"}, sdk=FakeSdk(_message()))

from __future__ import annotations

import json
import sys

from aperture import config as config_module
from aperture.runtime import __main__ as runtime_main
from aperture.runtime.__main__ import main
from aperture.runtime.preflight import Check, CheckName, PreflightCause, PreflightResult


def _records(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.startswith("{")]


def test_preflight_failure_alerts_and_returns_1(config_path, capsys):
    # The copied repo has no data folder, so preflight fails on any date.
    assert main(["--config", str(config_path), "preflight"]) == 1
    records = _records(capsys.readouterr().out)
    assert records[0]["record"] == "preflight" and records[0]["cause"] == "data-dir-missing"
    assert records[1]["record"] == "alert" and records[1]["kind"] == "preflight-failed"


def test_preflight_invalid_config_still_alerts(config_path, capsys):
    text = config_path.read_text("utf-8").replace("[risk]", "[riskk]", 1)
    config_path.write_text(text, "utf-8")
    assert main(["--config", str(config_path), "preflight"]) == 2
    records = _records(capsys.readouterr().out)
    assert records[1]["kind"] == "preflight-failed"
    assert records[1]["delivery"] in ("no-notifier", "shown", "failed")


def test_run_bot_refuses_without_command(config_path, capsys):
    assert main(["--config", str(config_path), "run-bot"]) == 2


def test_run_bot_does_not_start_when_preflight_fails(config_path, tmp_path, capsys):
    marker = tmp_path / "started"
    code = main(["--config", str(config_path), "run-bot", "--", sys.executable, "-c",
                 f"open({str(marker)!r}, 'w')"])
    assert code == 1 and not marker.exists()
    alert = _records(capsys.readouterr().out)[-1]
    assert alert["kind"] == "bot-did-not-start"


def _ready_preflight(monkeypatch, config_path):
    cfg = config_module.load(config_path)
    check = Check(CheckName.CONFIG, True, "ok")
    result = PreflightResult((check,), session_today=True, environment=cfg.environment_name,
                             config=cfg)
    monkeypatch.setattr(runtime_main, "run_preflight", lambda *a, **k: result)


def test_run_bot_crash_alerts_and_is_not_relaunched(config_path, monkeypatch, tmp_path, capsys):
    _ready_preflight(monkeypatch, config_path)
    counter = tmp_path / "runs"
    code = main(["--config", str(config_path), "run-bot", "--", sys.executable, "-c",
                 f"open({str(counter)!r}, 'a').write('x'); raise SystemExit(7)"])
    assert code == 7 and counter.read_text() == "x"
    records = _records(capsys.readouterr().out)
    assert records[-2]["record"] == "bot-exit" and records[-2]["return_code"] == 7
    assert records[-1]["kind"] == "bot-crashed"


def test_run_bot_clean_exit(config_path, monkeypatch, capsys):
    _ready_preflight(monkeypatch, config_path)
    assert main(["--config", str(config_path), "run-bot", "--", sys.executable, "-c",
                 "pass"]) == 0
    assert _records(capsys.readouterr().out)[-1]["record"] == "bot-exit"


def test_run_bot_starts_when_only_the_strategy_is_refused(config_path, monkeypatch, tmp_path,
                                                          capsys):
    cfg = config_module.load(config_path)
    checks = (Check(CheckName.CONFIG, True, "ok"),
              Check(CheckName.STRATEGY, False, "needs 5 prior sessions",
                    PreflightCause.STRATEGY_REFUSED))
    result = PreflightResult(checks, session_today=True, environment=cfg.environment_name,
                             config=cfg)
    monkeypatch.setattr(runtime_main, "run_preflight", lambda *a, **k: result)
    marker = tmp_path / "started"
    assert main(["--config", str(config_path), "run-bot", "--", sys.executable, "-c",
                 f"open({str(marker)!r}, 'w')"]) == 0
    assert marker.exists()
    alert = next(r for r in _records(capsys.readouterr().out) if r["record"] == "alert")
    assert alert["kind"] == "preflight-failed"


def test_alert_command(config_path, capsys):
    assert main(["--config", str(config_path), "alert", "bot-crashed", "test"]) == 0
    record = _records(capsys.readouterr().out)[0]
    assert record["kind"] == "bot-crashed" and record["text"] == "test"


def test_render_launchd(config_path, repo, tmp_path, capsys):
    out = tmp_path / "plists"
    assert main(["--config", str(config_path), "render-launchd", "--python", "/x/python",
                 "--env-file", "/x/env", "--log-dir", "/x/logs", "--out", str(out)]) == 0
    assert len(list(out.glob("*.plist"))) == 5


def test_token_probe_alerts_when_the_token_waits_for_the_owner(config_path, monkeypatch, capsys):
    from aperture.broker import webull

    cfg = config_module.load(config_path)
    monkeypatch.setattr(webull, "check_token", lambda cfg, on_token_pending: on_token_pending())
    assert runtime_main.TOKEN_PROBE(config_path)(cfg) is None
    (alert,) = _records(capsys.readouterr().out)
    assert alert["record"] == "alert" and alert["kind"] == "token-needs-confirmation"
    assert str(cfg.environment.token_check_duration_seconds) in alert["text"]


def test_token_probe_returns_the_refusal_cause(config_path, monkeypatch, capsys):
    from aperture.broker import webull

    def refuse(cfg, on_token_pending):
        raise webull.AdapterConfigError("token initialisation failed: token create failed")

    monkeypatch.setattr(webull, "check_token", refuse)
    probe = runtime_main.TOKEN_PROBE(config_path)
    cause = probe(config_module.load(config_path))
    assert cause == "token initialisation failed: token create failed"
    monkeypatch.setattr(webull, "check_token", lambda cfg, hook: 1 / 0)
    assert probe(config_module.load(config_path)) == "ZeroDivisionError"
    assert _records(capsys.readouterr().out) == []

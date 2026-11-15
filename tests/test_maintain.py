"""`tickr maintain`: archive finished days in place, keep what is still needed, read both."""

from __future__ import annotations

import gzip
import shutil
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest

from tickr import cli, config
from tickr.api.history import History, HistoryPaths
from tickr.archive import compress, existing, open_text
from tickr.cli import data_paths, main
from tickr.data.capture import BARS_FILE, CaptureWriter, read_captured_bars
from tickr.lock import SingleInstanceLock
from tickr.observers.log import FILE_NAME, DecisionLog
from tickr.research.records import Admission, DecisionRecord, load_records
from tickr.runtime.dayend import DAY_END
from tickr.store import Store
from tickr.strategy.base import Decision, Role, Verdict
from tickr.types import Bar

ROOT = Path(__file__).resolve().parent.parent
TODAY = date(2026, 11, 13)  # Friday; with 2 kept sessions Nov 11 and 12 stay plain
NOW = datetime(2026, 11, 13, 21, 0, tzinfo=UTC)
OLD, NO_DAY_END, RECENT = date(2026, 11, 5), date(2026, 11, 6), date(2026, 11, 10)
KEPT = (date(2026, 11, 11), date(2026, 11, 12), TODAY)


def _at(day: date) -> datetime:
    return datetime(day.year, day.month, day.day, 15, 0, tzinfo=UTC)  # 10:00 New York


def _bar(day: date) -> Bar:
    return Bar("ABCD", _at(day), D("1.0"), D("1.2"), D("0.9"), D("1.1"), D(100))


def _record(day: date) -> DecisionRecord:
    decision = Decision(strategy="s", version="1", symbol="ABCD", verdict=Verdict.WATCH,
                        checks=())
    return DecisionRecord(time=_at(day) + timedelta(minutes=1), bar_start=_at(day),
                          role=Role.ACTIVE, decision=decision,
                          admission=Admission.NOT_APPLICABLE)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """A config in tmp_path keeping 2 sessions, capture and decisions on six days, day-end rows
    on every day but NO_DAY_END. Returns (config path, config, store)."""
    (tmp_path / "config").mkdir()
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    path = tmp_path / "config" / "bot.toml"
    path.write_text(text.replace("maintain_keep_sessions = 20", "maintain_keep_sessions = 2"))
    cfg = config.load(path)
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    capture, log = CaptureWriter.from_config(cfg, "webull"), DecisionLog.from_config(cfg)
    store = Store.open(data_paths(cfg)[0], busy_timeout_seconds=1, now=lambda: NOW)
    run_id = store.start_run(environment=cfg.environment_name, config_digest="d",
                             strategy="s", strategy_version="1", code_version="c")
    for day in (OLD, NO_DAY_END, RECENT, *KEPT):
        capture.bars([_bar(day)], "webull", _at(day))
        log.append(_record(day))
        if day != NO_DAY_END:
            with store.transaction():
                store.append(DAY_END, {"day": day.isoformat()}, key=day.isoformat(),
                             run_id=run_id)
    capture.close()
    log.close()
    yield path, cfg, store, run_id
    store.close()


def _folders(cfg, day: date) -> list[Path]:
    data = cfg.root / cfg.run.data_dir
    return [data / cfg.market_data.capture_dir / cfg.environment_name / day.isoformat(),
            data / cfg.observers.decisions_dir / cfg.environment_name / day.isoformat()]


def _archived(cfg, day: date) -> bool:
    names = [p.name for f in _folders(cfg, day) for p in f.iterdir()]
    if all(n.endswith(".jsonl.gz") for n in names):
        return True
    assert all(n.endswith(".jsonl") for n in names), names
    return False


def test_dry_run_changes_nothing(setup, capsys):
    path, cfg, _, _ = setup
    assert main(["--config", str(path), "maintain"]) == 0
    out = capsys.readouterr().out
    assert out.count("would archive") == 4  # OLD and RECENT, capture and decisions
    assert "no day-end record" in out and "within the last 2 sessions" in out
    assert "dry run" in out
    assert not any(_archived(cfg, d) for d in (OLD, NO_DAY_END, RECENT, *KEPT))


def test_apply_archives_finished_days_and_keeps_the_rest(setup, capsys):
    path, cfg, _, _ = setup
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert "database checkpoint" in capsys.readouterr().out
    assert _archived(cfg, OLD) and _archived(cfg, RECENT)
    assert not any(_archived(cfg, d) for d in (NO_DAY_END, *KEPT))
    assert main(["--config", str(path), "maintain", "--apply"]) == 0  # nothing left to do
    lines = capsys.readouterr().out.splitlines()
    assert not any(line.startswith("archive") for line in lines)


def test_open_trade_keeps_its_day_and_later(setup, capsys):
    path, cfg, store, run_id = setup
    with store.transaction():
        store.execute(
            "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
            "entry_limit_price, stop_price, planned_loss_usd, opened_at) "
            "VALUES ('t1', ?, 'ABCD', 'open', '1', '1', '1', '1', ?)",
            (run_id, _at(NO_DAY_END).isoformat()))
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert "is still open" in capsys.readouterr().out
    assert _archived(cfg, OLD) and not _archived(cfg, RECENT)


def test_refused_while_the_bot_holds_the_lock(setup, capsys):
    path, cfg, _, _ = setup
    with SingleInstanceLock(data_paths(cfg)[1]):
        assert main(["--config", str(path), "maintain", "--apply"]) == 3
    assert "holds the lock" in capsys.readouterr().err
    assert not _archived(cfg, OLD)


def test_refused_without_database(tmp_path, capsys, monkeypatch):
    (tmp_path / "config").mkdir()
    shutil.copytree(ROOT / "config" / "calendar", tmp_path / "config" / "calendar")
    shutil.copy(ROOT / "config" / "bot.toml", tmp_path / "config" / "bot.toml")
    monkeypatch.setattr(cli, "NOW", lambda: NOW)
    assert main(["--config", str(tmp_path / "config" / "bot.toml"), "maintain"]) == 3
    assert "no database" in capsys.readouterr().err


def test_readers_read_archived_days(setup):
    path, cfg, _, _ = setup
    capture_day, decisions_day = _folders(cfg, OLD)
    bars_before = read_captured_bars(capture_day / BARS_FILE).bars
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert not (capture_day / BARS_FILE).exists()
    assert existing(capture_day / BARS_FILE) == capture_day / (BARS_FILE + ".gz")
    assert read_captured_bars(capture_day / BARS_FILE).bars == bars_before
    assert load_records([decisions_day / FILE_NAME]) == (_record(OLD),)
    history = History(HistoryPaths.from_config(cfg))
    assert [b.start for b in history.bars("ABCD", OLD, TODAY).bars] == [_at(OLD)]
    assert {d.day: d.has_bars for d in history.days(TODAY).days}[OLD] is True
    assert DecisionLog.from_config(cfg).latest_active(OLD, "ABCD") == _record(OLD)


def test_plain_file_beside_a_different_archive_keeps_the_folder(setup, capsys):
    path, cfg, _, _ = setup
    capture_day, _ = _folders(cfg, OLD)
    (capture_day / (BARS_FILE + ".gz")).write_bytes(b"not gzip")
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert "bars.jsonl.gz unreadable" in capsys.readouterr().out
    assert (capture_day / BARS_FILE).is_file()
    assert (capture_day / (BARS_FILE + ".gz")).read_bytes() == b"not gzip"
    other = capture_day / "quotes.jsonl"
    other.write_text("a\n")
    with gzip.open(capture_day / "quotes.jsonl.gz", "wt") as handle:
        handle.write("b\n")
    (capture_day / (BARS_FILE + ".gz")).unlink()
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert "quotes.jsonl and quotes.jsonl.gz differ" in capsys.readouterr().out
    assert other.is_file() and (capture_day / BARS_FILE).is_file()


GZIP_HEADER_BYTES = 10  # fixed gzip member header (RFC 1952) when no optional fields are set


def _truncated(packed: bytes) -> bytes:
    return packed[:len(packed) // 2]  # ends before the end-of-stream marker: EOFError


def _corrupt(packed: bytes) -> bytes:
    # first deflate byte 0xFF: block type 3, which does not exist: zlib.error
    return packed[:GZIP_HEADER_BYTES] + b"\xff" + packed[GZIP_HEADER_BYTES + 1:]


@pytest.mark.parametrize("damage", [_truncated, _corrupt])
def test_damaged_archive_beside_the_plain_file_keeps_the_folder_untouched(setup, capsys,
                                                                          damage):
    path, cfg, _, _ = setup
    capture_day, _ = _folders(cfg, OLD)
    plain = capture_day / BARS_FILE
    archived = capture_day / (BARS_FILE + ".gz")
    archived.write_bytes(damage(gzip.compress(plain.read_bytes(), mtime=0)))
    before = {f.name: f.read_bytes() for f in capture_day.iterdir()}
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert f"keep {capture_day}: bars.jsonl.gz unreadable" in capsys.readouterr().out
    assert {f.name: f.read_bytes() for f in capture_day.iterdir()} == before


def test_compress_round_trip_and_missing_file(tmp_path):
    plain = tmp_path / "x.jsonl"
    plain.write_text('{"a": 1}\nnot newline terminated', "utf-8")
    assert compress(plain) == tmp_path / "x.jsonl.gz" and not plain.exists()
    with open_text(plain) as handle:
        assert handle.read() == '{"a": 1}\nnot newline terminated'
    with pytest.raises(FileNotFoundError):
        open_text(tmp_path / "missing.jsonl")


def test_keep_sessions_below_one_refused(tmp_path):
    (tmp_path / "config").mkdir()
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    path = tmp_path / "config" / "bot.toml"
    path.write_text(text.replace("maintain_keep_sessions = 20", "maintain_keep_sessions = 0"))
    with pytest.raises(config.ConfigError, match="maintain_keep_sessions"):
        config.load(path)


def test_chart_cache_days_past_the_max_age_are_deleted(setup, capsys):
    path, cfg, _, _ = setup
    root = cfg.root / cfg.run.data_dir / cfg.chart.cache_dir / cfg.environment_name
    age = cfg.chart.cache_max_age_days
    kept_day, old_day = TODAY - timedelta(days=age), TODAY - timedelta(days=age + 1)
    kept = [root / "AAPL" / f"{kept_day.isoformat()}.json", root / "AAPL" / "notes.json",
            root / "AAPL" / f".{old_day.isoformat()}.json.0a.tmp",
            root / "AAPL" / "2026-02-30.json"]  # not a date: never ours to delete
    old = [root / "AAPL" / f"{old_day.isoformat()}.json", root / "BRK.B" / "2025-01-02.json"]
    for file in kept + old:
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("{}", "utf-8")
    assert main(["--config", str(path), "maintain"]) == 0
    assert f"would delete 2 chart cache files older than {age} days" in capsys.readouterr().out
    assert all(f.exists() for f in kept + old)
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert f"delete 2 chart cache files older than {age} days" in capsys.readouterr().out
    assert all(f.exists() for f in kept) and not any(f.exists() for f in old)
    assert (root / "BRK.B").is_dir()  # folders stay for a running API to write into


def test_chart_pruning_never_follows_symlinks_out_of_the_cache(setup, tmp_path, capsys):
    path, cfg, _, _ = setup
    root = cfg.root / cfg.run.data_dir / cfg.chart.cache_dir / cfg.environment_name
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = outside / "2025-01-02.json"
    victim.write_text("{}", "utf-8")
    root.mkdir(parents=True)
    (root / "LINKED").symlink_to(outside, target_is_directory=True)
    (root / "AAPL").mkdir()
    (root / "AAPL" / "2025-01-03.json").symlink_to(victim)
    plain = root / "AAPL" / "2025-01-04.json"
    plain.write_text("{}", "utf-8")
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert "delete 1 chart cache files" in capsys.readouterr().out
    assert victim.exists() and (root / "AAPL" / "2025-01-03.json").is_symlink()
    assert not plain.exists()


def test_a_chart_file_that_vanishes_is_skipped(setup, monkeypatch):
    from tickr import maintain
    _, cfg, _, _ = setup
    root = cfg.root / cfg.run.data_dir / cfg.chart.cache_dir / cfg.environment_name
    (root / "AAPL").mkdir(parents=True)
    gone = root / "AAPL" / "2025-01-02.json"
    gone.write_text("{}", "utf-8")
    kept = root / "AAPL" / "2025-01-03.json"
    kept.write_text("{}", "utf-8")
    real_stat = Path.stat

    def vanishing(self, *args, **kwargs):
        if self == gone and kwargs.get("follow_symlinks", True):  # stat, not lstat
            raise FileNotFoundError(self)
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", vanishing)
    assert [f for f, _ in maintain.stale_chart_files(cfg, TODAY)] == [kept]


def _history(cfg, day: date, names=("AAPL-5.jsonl", "BRK.B-5.partial")) -> Path:
    folder = (cfg.root / cfg.run.data_dir / cfg.market_data.history_dir / cfg.environment_name
              / day.isoformat())
    folder.mkdir(parents=True, exist_ok=True)
    for name in names:
        (folder / name).write_text("{}\n", "utf-8")
    return folder


def test_history_cache_days_past_the_max_age_are_deleted_whole(setup, capsys):
    path, cfg, _, _ = setup
    age = cfg.market_data.history_cache_max_age_days
    today, kept = _history(cfg, TODAY), _history(cfg, TODAY - timedelta(days=age))
    old = _history(cfg, TODAY - timedelta(days=age + 1))
    assert main(["--config", str(path), "maintain"]) == 0
    out = capsys.readouterr().out
    assert f"would delete history cache {old}: 2 files, 6 bytes" in out
    assert str(kept) not in out and str(today) not in out
    assert old.is_dir()
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert f"delete history cache {old}" in capsys.readouterr().out
    assert not old.exists() and today.is_dir() and kept.is_dir()


def test_history_folder_with_a_stranger_or_symlink_is_kept(setup, tmp_path, capsys):
    path, cfg, _, _ = setup
    stranger = _history(cfg, date(2025, 1, 2), ("AAPL-5.jsonl", "notes.txt"))
    linked = _history(cfg, date(2025, 1, 3), ())
    victim = tmp_path / "victim.jsonl"
    victim.write_text("{}\n", "utf-8")
    (linked / "AAPL-5.jsonl").symlink_to(victim)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "AAPL-5.jsonl").write_text("{}\n", "utf-8")
    (stranger.parent / "2025-01-04").symlink_to(outside, target_is_directory=True)
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    out = capsys.readouterr().out
    assert f"keep history cache {stranger}: notes.txt is not ours to delete" in out
    assert f"keep history cache {linked}: AAPL-5.jsonl is not ours to delete" in out
    assert (stranger / "AAPL-5.jsonl").exists() and victim.exists()
    assert (outside / "AAPL-5.jsonl").exists()


def _with_archive_age(path: Path, days: int):
    text = path.read_text("utf-8").replace("maintain_archive_max_age_days = 0",
                                           f"maintain_archive_max_age_days = {days}")
    path.write_text(text, "utf-8")
    return config.load(path)


def test_archives_are_never_deleted_with_the_shipped_zero(setup, capsys):
    path, cfg, _, _ = setup
    assert cfg.run.maintain_archive_max_age_days == 0
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert "archived" not in capsys.readouterr().out
    assert _archived(cfg, OLD) and all(f.is_dir() for f in _folders(cfg, OLD))


def test_old_archives_are_deleted_only_after_archiving_and_with_a_day_end(setup, capsys):
    path, cfg, store, run_id = setup
    assert main(["--config", str(path), "maintain", "--apply"]) == 0  # OLD, RECENT archived
    capsys.readouterr()
    cfg = _with_archive_age(path, (TODAY - NO_DAY_END).days - 1)  # OLD, NO_DAY_END past it
    for folder in _folders(cfg, NO_DAY_END):  # archived by hand, but never finished
        for file in list(folder.iterdir()):
            compress(file)
    assert main(["--config", str(path), "maintain"]) == 0
    out = capsys.readouterr().out
    assert out.count("would delete archived") == 2  # OLD: capture and decisions
    assert out.count("no day-end record for this day") == 2  # NO_DAY_END, both folders
    assert all(f.is_dir() for f in _folders(cfg, OLD))
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert not any(f.exists() for f in _folders(cfg, OLD))
    assert all(f.is_dir() for f in _folders(cfg, NO_DAY_END) + _folders(cfg, RECENT))


def test_old_archive_with_a_plain_file_or_an_open_trade_is_kept(setup, capsys):
    path, cfg, store, run_id = setup
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    capsys.readouterr()
    cfg = _with_archive_age(path, 1)
    stranger = _folders(cfg, RECENT)[0] / "notes.txt"
    stranger.write_text("x", "utf-8")
    assert main(["--config", str(path), "maintain"]) == 0
    assert "notes.txt is not ours to delete" in capsys.readouterr().out
    with store.transaction():
        store.execute(
            "INSERT INTO trades(trade_id, run_id, symbol, state, entry_quantity, "
            "entry_limit_price, stop_price, planned_loss_usd, opened_at) "
            "VALUES ('t1', ?, 'ABCD', 'open', '1', '1', '1', '1', ?)",
            (run_id, _at(OLD).isoformat()))
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    # OLD and RECENT archives (4 folders) and NO_DAY_END, still plain (2)
    assert capsys.readouterr().out.count("a trade opened 2026-11-05 is still open") == 6
    assert all(f.is_dir() for f in _folders(cfg, OLD) + _folders(cfg, RECENT))
    assert stranger.exists()


def test_archive_and_history_ages_validated(tmp_path):
    (tmp_path / "config").mkdir()
    text = (ROOT / "config" / "bot.toml").read_text("utf-8")
    path = tmp_path / "config" / "bot.toml"
    path.write_text(text.replace("maintain_archive_max_age_days = 0",
                                 "maintain_archive_max_age_days = -1"))
    with pytest.raises(config.ConfigError, match="maintain_archive_max_age_days"):
        config.load(path)
    path.write_text(text.replace("history_cache_max_age_days = 2",
                                 "history_cache_max_age_days = 0"))
    with pytest.raises(config.ConfigError, match="history_cache_max_age_days"):
        config.load(path)


def test_archives_are_not_deleted_by_the_run_that_made_them_nor_inside_kept_sessions(setup,
                                                                                        capsys):
    path, cfg, _, _ = setup
    cfg = _with_archive_age(path, 1)
    for folder in _folders(cfg, KEPT[0]):  # gzipped by hand inside the kept sessions
        for file in list(folder.iterdir()):
            compress(file)
    assert main(["--config", str(path), "maintain", "--apply"]) == 0  # archives OLD, RECENT
    assert "delete archived" not in capsys.readouterr().out
    assert all(f.is_dir() for f in _folders(cfg, OLD) + _folders(cfg, KEPT[0]))
    assert main(["--config", str(path), "maintain", "--apply"]) == 0
    assert capsys.readouterr().out.count("delete archived") == 4  # OLD and RECENT
    assert all(f.is_dir() for f in _folders(cfg, KEPT[0]))

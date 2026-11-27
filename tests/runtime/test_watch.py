from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

from baseline import load_baseline

from tickr.broker.fake import FakeBroker
from tickr.calendar import NEW_YORK
from tickr.config import GateResult
from tickr.engine.core import Engine, Fence
from tickr.runtime import service
from tickr.runtime.alerts import Alerter, AlertKind
from tickr.runtime.watch import ALERT, BACKUP, BACKUP_FAILED, Watch
from tickr.store import Store

ROOT = Path(__file__).resolve().parents[2]
CFG = load_baseline()
MONDAY_11 = datetime(2026, 11, 16, 11, 0, tzinfo=NEW_YORK)
AFTER = float(CFG.alerts.fence_alert_after_seconds)
RETRY = timedelta(seconds=float(CFG.run.backup_retry_seconds))


class Recorder:
    def __init__(self) -> None:
        self.shown: list[tuple[str, str]] = []

    def notify(self, title: str, text: str) -> None:
        self.shown.append((title, text))


class Rig:
    def __init__(self, tmp_path: Path, broker: FakeBroker | None = None,
                 stamped: datetime | None = None) -> None:
        """`stamped`: the time the store stamps its rows with (the real clock without it)."""
        self.cfg = replace(CFG, root=tmp_path)
        self.broker = broker or FakeBroker()
        self.store = (Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0) if stamped is None
                      else Store.open(tmp_path / "db.sqlite3", busy_timeout_seconds=0,
                                      now=lambda: stamped.astimezone(UTC)))
        self.clock = [1_000.0]
        self.engine = Engine(self.store, self.broker, self.cfg, "watch001",
                             lambda: GateResult(True, "t"), clock=lambda: self.clock[0])
        self.now = MONDAY_11
        self.notifier = Recorder()
        self.watch = Watch(self.engine, self.cfg,
                           Alerter(self.cfg.alerts, "Tickr test", self.notifier,
                                   lambda: self.now),
                           lambda: self.now)

    def fence(self, fence: Fence, key: str) -> None:
        with self.store.transaction():
            self.engine._fence_row(fence, key, "test")

    def kinds(self) -> list[str]:
        return [r.body["kind"] for r in self.store.journal(ALERT)]


def test_operator_fence_alerts_at_once_and_only_once(tmp_path):
    rig = Rig(tmp_path)
    rig.fence(Fence.STOP_REJECTED, "t1")
    rig.watch.check()
    rig.watch.check()
    assert rig.kinds() == [AlertKind.STOP_REJECTED]
    assert rig.notifier.shown == [("Tickr test: stop-rejected", "stop-rejected:t1: test")]


def test_self_clearing_fence_alerts_only_when_it_lasts(tmp_path):
    rig = Rig(tmp_path)
    rig.fence(Fence.BROKER_READ, "")
    rig.clock[0] += AFTER - 1
    rig.watch.check()
    assert rig.kinds() == []
    rig.clock[0] += 1
    rig.watch.check()
    assert rig.kinds() == [AlertKind.BROKER_READS_FAILING]


def test_detail_unreadable_alerts_at_once(tmp_path):
    """The engine sets it only after its own wait (`[orders] detail_unreadable_seconds`), and a
    stop may be gone meanwhile: no second wait before the owner is told."""
    rig = Rig(tmp_path)
    rig.fence(Fence.DETAIL_UNREADABLE, "c1")
    rig.watch.check()
    assert rig.kinds() == [AlertKind.UNKNOWN_ORDER_STATUS]
    assert not rig.engine.clear_operator_fence("detail-unreadable:c1")  # clears itself


def test_a_fence_set_again_after_clearing_alerts_again(tmp_path):
    rig = Rig(tmp_path)
    rig.fence(Fence.EXIT_REFUSED, "t1")
    rig.watch.check()
    rig.engine.clear_operator_fence("exit-refused:t1")
    rig.watch.check()
    rig.now += timedelta(seconds=float(CFG.alerts.dedupe_window_seconds))
    rig.fence(Fence.EXIT_REFUSED, "t1")
    rig.watch.check()
    assert rig.kinds() == [AlertKind.EXIT_REFUSED, AlertKind.EXIT_REFUSED]


def test_stream_lost_exit_alerts_once(tmp_path):
    rig = Rig(tmp_path)
    t = rig.engine.open_trade("ABC", D(1), D(10), D(9), D(1))
    rig.engine.request_exit(t, "stream-lost")
    rig.watch.check()
    rig.watch.check()
    assert rig.kinds() == [AlertKind.STREAM_LOST_WITH_POSITION]


def test_daily_backup_is_verified_and_only_the_latest_is_kept(tmp_path):
    rig = Rig(tmp_path)
    folder = tmp_path / CFG.run.data_dir / CFG.run.backups_dir
    folder.mkdir(parents=True)
    (folder / "tickr-2026-11-13.sqlite3").write_bytes(b"old")
    rig.watch.backup_daily()
    assert sorted(p.name for p in folder.iterdir()) == ["tickr-2026-11-16.sqlite3"]
    rig.watch.backup_daily()  # same day: nothing more
    assert len(rig.store.journal(BACKUP)) == 1
    rig.now += timedelta(days=1)
    rig.watch.backup_daily()
    assert sorted(p.name for p in folder.iterdir()) == ["tickr-2026-11-17.sqlite3"]


def test_backup_failure_is_journaled_and_keeps_the_old_one(tmp_path):
    rig = Rig(tmp_path)
    folder = tmp_path / CFG.run.data_dir / CFG.run.backups_dir
    folder.mkdir(parents=True)
    old = folder / "tickr-2026-11-13.sqlite3"
    old.write_bytes(b"old")

    def broken(target):
        raise OSError("disk full")

    rig.store.backup = broken
    rig.watch.backup_daily()
    assert old.exists()
    (row,) = rig.store.journal(BACKUP_FAILED)
    assert row.body["error"] == "OSError"


def test_failed_backup_is_retried_the_same_day_after_the_delay(tmp_path):
    """A transient failure must not cost the day's backup (review 2026-11-24 section 8)."""
    rig = Rig(tmp_path)
    folder = tmp_path / CFG.run.data_dir / CFG.run.backups_dir
    folder.mkdir(parents=True)
    real = rig.store.backup

    def broken(target):
        raise OSError("disk busy")

    rig.store.backup = broken
    rig.watch.backup_daily()
    rig.store.backup = real
    rig.now += RETRY - timedelta(seconds=1)
    rig.watch.backup_daily()  # still inside the delay: no new attempt
    assert list(folder.iterdir()) == []
    rig.now += timedelta(seconds=1)
    rig.watch.backup_daily()
    assert sorted(p.name for p in folder.iterdir()) == ["tickr-2026-11-16.sqlite3"]
    assert len(rig.store.journal(BACKUP_FAILED)) == 1
    assert len(rig.store.journal(BACKUP)) == 1
    rig.now += RETRY
    rig.watch.backup_daily()  # done for the day
    assert len(rig.store.journal(BACKUP)) == 1


def test_failed_cleanup_is_retried_and_the_day_is_done_only_after_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    folder = tmp_path / CFG.run.data_dir / CFG.run.backups_dir
    folder.mkdir(parents=True)
    old = folder / "tickr-2026-11-13.sqlite3"
    old.write_bytes(b"old")
    real_unlink = Path.unlink

    def refuse_old(self, missing_ok=False):
        if self == old:
            raise PermissionError("read-only")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_old)
    rig.watch.backup_daily()
    assert sorted(p.name for p in folder.iterdir()) == [old.name, "tickr-2026-11-16.sqlite3"]
    monkeypatch.setattr(Path, "unlink", real_unlink)
    rig.now += RETRY
    rig.watch.backup_daily()  # the published backup is kept, only the old one goes
    assert sorted(p.name for p in folder.iterdir()) == ["tickr-2026-11-16.sqlite3"]
    assert len(rig.store.journal(BACKUP)) == 1


class NeverCancels(FakeBroker):
    def cancel(self, cid):
        pass


def test_drain_deadline_alerts(tmp_path):
    rig = Rig(tmp_path, broker=NeverCancels())
    rig.engine.open_trade("ABC", D(1), D(10), D(9), D(1))
    paths = service.ServicePaths(tmp_path / "status.json", tmp_path / "in", tmp_path / "out")
    bot = service.Service(rig.engine, now=lambda: rig.now, calendar=None,
                          strategy=service.strategy_id(CFG), paths=paths, watch=rig.watch)
    clock = [0.0]

    def sleep(seconds):
        clock[0] += seconds

    assert not bot.run(should_stop=lambda: True, sleep=sleep, monotonic=lambda: clock[0])
    assert AlertKind.BOT_DID_NOT_STOP in rig.kinds()


def test_failed_verification_never_leaves_a_final_name_or_deletes_the_old(tmp_path):
    import sqlite3

    rig = Rig(tmp_path)
    folder = tmp_path / CFG.run.data_dir / CFG.run.backups_dir
    folder.mkdir(parents=True)
    old = folder / "tickr-2026-11-13.sqlite3"
    old.write_bytes(b"old")
    real = rig.store.backup

    def unverified(target):
        real(target)
        raise RuntimeError("backup failed integrity_check: test")

    rig.store.backup = unverified
    rig.watch.backup_daily()
    assert sorted(p.name for p in folder.iterdir()) == [old.name]

    def vacuum_fails(target):
        raise sqlite3.OperationalError("database or disk is full")

    rig.now += RETRY
    rig.store.backup = vacuum_fails
    rig.watch.backup_daily()  # never raises into the loop
    assert [r.body["error"] for r in rig.store.journal(BACKUP_FAILED)] == [
        "RuntimeError", "OperationalError"]
    rig.now += RETRY
    rig.store.backup = real
    rig.watch.backup_daily()
    assert sorted(p.name for p in folder.iterdir()) == ["tickr-2026-11-16.sqlite3"]


def test_cleanup_failure_is_journaled_not_raised(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    folder = tmp_path / CFG.run.data_dir / CFG.run.backups_dir
    folder.mkdir(parents=True)
    (folder / "tickr-2026-11-13.sqlite3").write_bytes(b"old")

    def refuse(self, missing_ok=False):
        raise PermissionError("read-only")

    rig.watch.backup_daily()
    monkeypatch.setattr(Path, "unlink", refuse)
    rig.now += timedelta(days=1)
    rig.watch.backup_daily()
    assert rig.store.journal(BACKUP_FAILED)[-1].body["error"] == "PermissionError"


def test_over_protection_alerts_when_it_lasts(tmp_path):
    rig = Rig(tmp_path)
    rig.fence(Fence.OVER_PROTECTED, "t1")
    rig.clock[0] += AFTER
    rig.watch.check()
    assert rig.kinds() == [AlertKind.UNKNOWN_ORDER_STATUS]


def test_daily_ceiling_alerts_once_per_day_when_the_net_loss_reaches_it(tmp_path, monkeypatch):
    rig = Rig(tmp_path)
    ceiling = CFG.risk.max_daily_loss_usd
    loss: list[D | None] = [ceiling - 1]
    monkeypatch.setattr("tickr.runtime.watch.realized_loss_today",
                        lambda engine, now: loss[0])
    rig.watch.check()
    loss[0] = None  # fill price unknown: no claim either way
    rig.watch.check()
    assert rig.kinds() == []
    loss[0] = ceiling
    rig.watch.check()
    rig.watch.check()
    assert rig.kinds() == [AlertKind.DAILY_CEILING_REACHED]
    assert f"reached the {ceiling} USD ceiling" in rig.store.journal(ALERT)[0].body["text"]
    rig.now += timedelta(days=1)
    rig.watch.check()
    assert rig.kinds() == [AlertKind.DAILY_CEILING_REACHED] * 2


def test_daily_ceiling_uses_the_real_net_result(tmp_path):
    rig = Rig(tmp_path)
    t = rig.engine.open_trade("ABC", D(100), D(10), D(5), D(1))
    rig.broker.fill(rig.engine.orders(t)[0].client_order_id, D(100), D(10))
    for _ in range(3):
        rig.engine.step()
    rig.engine.request_exit(t, "test")
    for _ in range(4):
        rig.engine.step()
        for o in rig.broker.open_orders():
            if o.order_type == "MARKET":
                rig.broker.fill(o.client_order_id, D(100), D(6))  # 400 USD loss
    assert rig.engine.trades() == []  # closed
    rig.now = datetime.now(NEW_YORK)  # trades_today reads the close time of the engine
    rig.watch.check()
    assert AlertKind.DAILY_CEILING_REACHED in rig.kinds()


def test_loop_pass_never_waits_on_a_slow_channel_and_journals_its_outcome_when_known(tmp_path):
    """The bot's loop calls watch.check(): an alert returns while the Telegram channel is
    still blocked; a later pass journals its outcome; exit flushes nothing twice."""
    import threading

    from tickr.runtime.alerts import TELEGRAM, Channels, NotifyError

    entered, release = threading.Event(), threading.Event()

    class SlowPhone:
        def notify(self, title: str, text: str) -> None:
            entered.set()
            release.wait()
            raise NotifyError("telegram answered HTTP 500")

    rig = Rig(tmp_path)
    rig.watch.alerter = Alerter(rig.cfg.alerts, "Tickr test",
                                Channels((("macos", rig.notifier),), ((TELEGRAM, SlowPhone()),)),
                                lambda: rig.now)
    rig.fence(Fence.STOP_REJECTED, "t1")
    rig.watch.check()  # returns although the Telegram send is blocked
    assert entered.wait(5) and not release.is_set()
    bodies = [r.body for r in rig.store.journal(ALERT)]
    assert [(b["delivery"], b.get("pending")) for b in bodies] == [("shown", ["telegram"])]
    release.set()
    rig.watch.flush_alerts()
    rig.watch.check()
    bodies = [r.body for r in rig.store.journal(ALERT)]
    assert [(b["delivery"], b.get("channel")) for b in bodies] == [
        ("shown", None), ("failed", "telegram")]
    assert len(rig.notifier.shown) == 1


def _filled_without_fees(tmp_path: Path, finished: datetime) -> tuple[Rig, str]:
    """A rig whose entry filled at `finished` and whose Detail shows no readable fees."""
    rig = Rig(tmp_path, FakeBroker(fill_fees=None), stamped=finished)
    t = rig.engine.open_trade("ABC", D(2), D(10), D(9), D(1))
    entry = rig.engine.orders(t)[0].client_order_id
    rig.broker.fill(entry, D(2), D(10))
    for _ in range(3):
        rig.engine.step()
    return rig, entry


def test_fees_still_unknown_after_the_configured_wait_alert_once(tmp_path):
    finished = MONDAY_11
    rig, entry = _filled_without_fees(tmp_path, finished)
    wait = float(rig.cfg.orders.fees_unknown_alert_seconds)
    rig.now = finished + timedelta(seconds=wait / 2)
    rig.watch.check()
    assert AlertKind.FEES_UNKNOWN not in rig.kinds()
    rig.now = finished + timedelta(seconds=wait + 5)
    rig.watch.check()
    rig.watch.check()
    assert rig.kinds().count(AlertKind.FEES_UNKNOWN) == 1
    assert entry in rig.notifier.shown[-1][1]


def test_fees_unknown_of_an_order_finished_just_before_ny_midnight_still_alert(tmp_path):
    # Due after midnight: the new day must not hide it.
    wait = float(CFG.orders.fees_unknown_alert_seconds)
    midnight = datetime(2026, 11, 17, tzinfo=NEW_YORK)
    finished = midnight - timedelta(seconds=wait / 2)
    rig, entry = _filled_without_fees(tmp_path, finished)
    rig.now = finished + timedelta(seconds=wait + 5)
    rig.watch.check()
    assert rig.kinds().count(AlertKind.FEES_UNKNOWN) == 1
    assert entry in rig.notifier.shown[-1][1]


def test_each_fee_correction_alerts_once_and_not_again_after_a_restart(tmp_path):
    rig = Rig(tmp_path)
    t = rig.engine.open_trade("ABC", D(2), D(10), D(9), D(1))
    entry = rig.engine.orders(t)[0].client_order_id
    rig.broker.fill(entry, D(2), D(10))
    for _ in range(3):
        rig.engine.step()
    assert rig.engine.record_order_cash(entry, D("-20.05"), D("0.05"), {})  # Detail said 0
    rig.watch.check()
    rig.watch.check()
    assert rig.kinds().count(AlertKind.FEES_CORRECTED) == 1
    assert "0 USD replaced by 0.05 USD" in rig.notifier.shown[-1][1]
    again = Watch(rig.engine, rig.cfg, Alerter(rig.cfg.alerts, "Tickr test", rig.notifier,
                                               lambda: rig.now), lambda: rig.now)
    again.check()
    assert rig.kinds().count(AlertKind.FEES_CORRECTED) == 1

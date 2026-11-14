"""Bot-side alerts (spec 11) and the daily verified database backup (spec 10).

The daily ceiling alerts once per New York day, when today's net realized loss reaches
`[risk] max_daily_loss_usd` (from then on every entry is refused for the day).

Alerts come from engine fences: a fence that needs the operator alerts at once; a fence that
clears itself alerts only after it has lasted `fence_alert_after_seconds` (a single failed read
is not worth a notification). Each fence alerts once while it stays set. Every outcome is
journaled as `alert`, whether shown, suppressed or failed.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Callable
from datetime import date, datetime
from pathlib import Path

from aperture.calendar import NEW_YORK
from aperture.config import Config
from aperture.engine.core import OPERATOR_FENCES, Engine, Fence
from aperture.runtime.alerts import Alerter, AlertKind, AlertOutcome
from aperture.runtime.entries import realized_loss_today

ALERT = "alert"
BACKUP = "backup"
BACKUP_FAILED = "backup-failed"
BACKUP_PREFIX = "aperture-"
BACKUP_SUFFIX = ".sqlite3"
TEMP_PREFIX = "partial-"

FENCE_ALERTS: dict[str, AlertKind] = {
    Fence.UNPROTECTED_TIMEOUT: AlertKind.UNPROTECTED_POSITION,
    Fence.UNPROTECTED: AlertKind.UNPROTECTED_POSITION,
    Fence.STOP_REJECTED: AlertKind.STOP_REJECTED,
    Fence.EXIT_REFUSED: AlertKind.EXIT_REFUSED,
    Fence.UNKNOWN_ORDER: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.STATUS_UNKNOWN: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.ORDER_MISSING: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.CANCEL_UNKNOWN: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.CANCEL_FAILED: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.RESURRECTED: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.QUANTITY_MISMATCH: AlertKind.UNKNOWN_ORDER_STATUS,
    Fence.OVER_PROTECTED: AlertKind.UNKNOWN_ORDER_STATUS,  # an extra stop could sell short
    Fence.FOREIGN_POSITION: AlertKind.FOREIGN_EXPOSURE,
    Fence.FOREIGN_ORDER: AlertKind.FOREIGN_EXPOSURE,
    Fence.BROKER_READ: AlertKind.BROKER_READS_FAILING,
}
STREAM_LOST_CAUSE = "stream-lost"


class Watch:
    def __init__(self, engine: Engine, config: Config, alerter: Alerter,
                 now: Callable[[], datetime]) -> None:
        self.engine = engine
        self.config = config
        self.alerter = alerter
        self.now = now
        self._alerted: set[str] = set()  # fences alerted while they stay set
        self._stream_lost: set[str] = set()  # trades alerted for a stream-lost exit
        self._backup_day: date | None = None
        self._ceiling_day: date | None = None  # day the daily ceiling was alerted

    def alert(self, kind: AlertKind, text: str) -> None:
        self._journal([self.alerter.alert(kind, text)])

    def _journal(self, outcomes: list[AlertOutcome]) -> None:
        for outcome in outcomes:
            with self.engine.store.transaction():
                self.engine.store.append(ALERT, outcome.journal_body(),
                                         run_id=self.engine.run_id)

    def flush_alerts(self) -> None:
        """At exit: wait for background alert sends (iMessage) and journal their outcomes."""
        self._journal(self.alerter.flush())

    def check(self) -> None:
        """Once per loop pass. Journals background alert sends that finished meanwhile."""
        self._journal(self.alerter.completed())
        fences = self.engine.fences()
        ages = self.engine.fence_ages()
        threshold = float(self.config.alerts.fence_alert_after_seconds)
        for name, cause in sorted(fences.items()):
            kind_name = name.split(":", 1)[0]
            kind = FENCE_ALERTS.get(kind_name)
            if kind is None or name in self._alerted:
                continue
            if kind_name not in OPERATOR_FENCES and ages.get(name, 0.0) < threshold:
                continue
            self._alerted.add(name)
            self.alert(kind, f"{name}: {cause}")
        self._alerted &= set(fences)  # a fence set again later alerts again
        for trade in self.engine.trades():
            if (trade.exit_requested == STREAM_LOST_CAUSE
                    and trade.trade_id not in self._stream_lost):
                self._stream_lost.add(trade.trade_id)
                self.alert(AlertKind.STREAM_LOST_WITH_POSITION,
                           f"{trade.symbol}: L1 stream lost, closing the position")
        self._check_daily_ceiling()

    def _check_daily_ceiling(self) -> None:
        now = self.now()
        day = now.astimezone(NEW_YORK).date()
        if self._ceiling_day == day:
            return
        loss = realized_loss_today(self.engine, now)
        ceiling = self.config.risk.max_daily_loss_usd
        if loss is None or loss < ceiling:
            return  # unknown fill prices: entries are refused for that anyway (risk)
        self._ceiling_day = day
        self.alert(AlertKind.DAILY_CEILING_REACHED,
                   f"net realized loss today {loss} USD reached the {ceiling} USD ceiling: "
                   "no new entries today")

    # --- backup ----------------------------------------------------------------------------

    def backup_daily(self) -> None:
        """One verified backup per New York date; older ones removed once it is verified."""
        day = self.now().astimezone(NEW_YORK).date()
        if self._backup_day == day:
            return
        self._backup_day = day
        folder = self.config.root / self.config.run.data_dir / self.config.run.backups_dir
        target = folder / f"{BACKUP_PREFIX}{day.isoformat()}{BACKUP_SUFFIX}"
        # Written under a temporary name and verified before it takes the final name, so a
        # file with the final name is always a verified backup (also after a crash).
        temporary = folder / f"{TEMP_PREFIX}{target.name}"
        store = self.engine.store
        try:
            if not target.exists():
                temporary.unlink(missing_ok=True)
                try:
                    store.backup(temporary)  # VACUUM INTO + integrity_check
                except BaseException:
                    temporary.unlink(missing_ok=True)
                    raise
                os.replace(temporary, target)
                with store.transaction():
                    store.append(BACKUP, {"file": target.name}, run_id=self.engine.run_id)
            for old in _older_backups(folder, target):
                old.unlink()
        except (OSError, RuntimeError, sqlite3.Error) as exc:
            with store.transaction():
                store.append(BACKUP_FAILED, {"error": type(exc).__name__, "detail": str(exc)},
                             run_id=self.engine.run_id)


def _older_backups(folder: Path, keep: Path) -> list[Path]:
    return [p for p in folder.glob(f"{BACKUP_PREFIX}*{BACKUP_SUFFIX}") if p != keep]

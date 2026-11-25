"""R34: the Benzinga feed lag monitor, read from the revision store (`tickr news-lag`).

Reads what `tickr poll-revisions` already wrote (the RevisionStore and the coverage log of
`tickr.model.revisions`); it fetches nothing and imports no network module. Per kind, over the
ids first received in the last `--window-seconds`: how many, the median and worst of receipt time
minus vendor time (news: `published`; earnings: `last_updated` of the first version), the share
above `bz_lag_s` (the training's assumed lag, read from the pinned settings, no second copy), and
the poll gaps inside the window. Baseline ids (first poll of a store) are left out, as in the
revisions report.

A median above `bz_lag_s` is a REPORT: with --alert it raises NEWS_LAG_HIGH once per UTC day per
kind. It never refuses or decides anything; the value is initial and meant to be replaced by the
measured delays.
"""

from __future__ import annotations

import datetime
import json
import sys
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from tickr.config import Config
from tickr.model import revisions
from tickr.model.errors import ModelRefused
from tickr.model.revisions import KINDS, CoverageLog, Kind, RevisionStore

NS = revisions.NS
# The delay series of `revisions.delays` that is the first receipt of a new id, per kind.
SERIES = {"news": "receipt - published (first receipt)",
          "earnings": "receipt - last_updated"}
ALERT_STATE = "newslag-alerts.json"  # kind -> UTC date of its last alert, in the store folder


def store_folder(cfg: Config) -> Path:
    rel = Path(cfg.massive.revision_store_dir)
    if not rel.parts or rel.is_absolute() or ".." in rel.parts:
        raise ModelRefused("revision_poll_config", "[massive] revision_store_dir must be relative")
    return cfg.root / cfg.run.data_dir / rel


def measure(folder: Path, kind: Kind, now: datetime.datetime, window_s: float,
            lag_s: int) -> dict[str, Any]:
    """The figures of one kind over [now - window_s, now]."""
    start_ns, end_ns = revisions.ns_of(now) - int(window_s * NS), revisions.ns_of(now)
    store = RevisionStore(folder, kind)
    firsts = {i: v[:1] for i, v in store.by_id.items()
              if start_ns <= v[0].received_ns <= end_ns}
    values = revisions.delays(kind, firsts).get(SERIES[kind.name], [])
    cov = CoverageLog(folder, kind).coverage()
    gaps = [g for g in cov.gaps
            if g.start_ns < end_ns and (g.end_ns is None or g.end_ns > start_ns)]
    out: dict[str, Any] = {"count": len(values), "gaps": len(gaps), "bz_lag_s": lag_s,
                           "median": None, "worst": None, "share_above": None}
    if values:
        out.update(median=revisions.quantile(values, 0.5), worst=values[-1],
                   share_above=sum(1 for x in values if x > lag_s) / len(values))
    return out


def report(folder: Path, now: datetime.datetime, window_s: float, lag_s: int
           ) -> dict[str, dict[str, Any]]:
    return {k.name: measure(folder, k, now, window_s, lag_s) for k in KINDS.values()}


def breached(figures: Mapping[str, Any]) -> bool:
    return figures["median"] is not None and figures["median"] > figures["bz_lag_s"]


def alert_text(name: str, figures: Mapping[str, Any]) -> str:
    return (f"{name} feed lag: median {figures['median']:.0f} s, worst {figures['worst']:.0f} s "
            f"over {figures['count']} ids, above the assumed bz_lag_s {figures['bz_lag_s']} s "
            "(a report, not a refusal)")


def _sent_days(path: Path) -> dict[str, str]:
    try:
        return dict(json.loads(path.read_text(encoding="utf-8")))
    except FileNotFoundError:
        return {}
    except (ValueError, TypeError):
        raise ModelRefused("revision_store_unreadable", path.name) from None


def raise_alerts(rep: Mapping[str, Mapping[str, Any]], folder: Path, now: datetime.datetime,
                 alerter: Any) -> list[str]:
    """Alert each breached kind not yet alerted today (UTC); a day counts as used only once an
    alert was delivered or is being delivered. Returns the kinds alerted."""
    from tickr.runtime.alerts import AlertKind, Delivery

    path = folder / ALERT_STATE
    sent = _sent_days(path)
    today = now.astimezone(datetime.UTC).date().isoformat()
    done = []
    for name, figures in rep.items():
        if not breached(figures) or sent.get(name) == today:
            continue
        outcome = alerter.alert(AlertKind.NEWS_LAG_HIGH, alert_text(name, figures))
        if outcome.delivery in (Delivery.SHOWN, Delivery.SENDING):
            sent[name] = today
            done.append(name)
    if done:
        path.write_text(json.dumps(sent, sort_keys=True), encoding="utf-8")
    return done


def main(cfg: Config, *, window_s: float, send_alert: bool, notifier: Any = None,
         now: Callable[[], datetime.datetime] | None = None,
         out: Callable[[str], None] = print) -> int:
    """`tickr news-lag`: 0 printed, 3 on a refusal."""
    try:
        if window_s <= 0:
            raise ModelRefused("news_lag_window", "--window-seconds must be above 0")
        moment = (now or (lambda: datetime.datetime.now(datetime.UTC)))()
        folder = store_folder(cfg)
        lag_s = revisions.pinned_lag_s()
        rep = report(folder, moment, window_s, lag_s)
        out(f"bz_lag_s (pinned training setting): {lag_s} s; window {window_s:g} s")
        for name, f in rep.items():
            if f["count"]:
                out(f"[{name}] ids {f['count']}, median {f['median']:g} s, worst {f['worst']:g} s, "
                    f"above bz_lag_s {f['share_above']:.1%}, poll gaps {f['gaps']}")
            else:
                out(f"[{name}] no new ids in the window, poll gaps {f['gaps']}")
        if send_alert:
            from tickr.runtime.alerts import Alerter
            alerter = Alerter(cfg.alerts, f"Tickr {cfg.environment_name}", notifier)
            for name in raise_alerts(rep, folder, moment, alerter):
                out(f"alert sent: {name}")
            alerter.flush()
    except ModelRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    return 0

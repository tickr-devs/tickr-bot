"""Synthetic non-bar inputs in the training's file formats, for the 191-feature tests: a tick
table in the schema of the frozen converter wm2tick v11 (sym, m and 61 value columns), a prepared
reference folder with its manifest, a Benzinga folder and an fl facts file; the training's own
modules imported the research way, the snapshot as of a minute cut by the training's own
`cut_inputs`, and the bot calendar's day."""

from __future__ import annotations

import datetime
import gzip
import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd

from tickr.calendar import NEW_YORK, MarketDay, SessionCalendar
from tickr.model import features
from tickr.model.features import full

CALENDAR_DIR = Path(__file__).parents[2] / "config" / "calendar"

# The 61 value columns of wm2tick v11 (c1c1ead1) in its order; tn and qn are BIGINT, the rest
# DOUBLE (read from a `local` run of the converter on a two-stock day).
TICK_COLUMNS = (
    "tn tv tdv tmax tbig toff tbuy tsell tvar tchg twt tlast topen thi tlo tbig5 tbig100 todd "
    "tvodd tbuyn tselln tlasta tne tve tiso tviso tisob tisos tesp tabove tbelow tin tvq trb trs "
    "trbq trsq tp15 tp45 tv15f ts15f tv15l ts15l tgap tburst qn qspr qbs qas qimb qofi qgap qbad "
    "qbid qask qhalt qmvar qbsl qasl qbd qad").split()
INTEGER_COLUMNS = ("tn", "qn")
DAY_MINUTE_ZERO = 240  # minute of the New York day of table minute 0 (04:00)
PUBLISHER = "GlobeNewswire Inc."  # one of wm2ref16.NW_PUB


def _ny_utc(day: str, minute: int, second: int = 0) -> str:
    """UTC text of table minute `minute` of `day` (from 04:00 New York) plus `second`."""
    t = pd.Timestamp(day + " 04:00", tz="America/New_York") + pd.Timedelta(
        minutes=minute, seconds=second)
    return t.tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")


def _ident(path: Path) -> list:
    return [path.stat().st_size, hashlib.md5(path.read_bytes()).hexdigest()]


def tick_file(path: Path, symbols: list[str], minutes: range, seed: int = 3) -> Path:
    rs = np.random.RandomState(seed)
    rows = []
    for sym in symbols:
        price = float(rs.uniform(5, 50))
        for m in minutes:
            if rs.rand() < 0.3:
                continue
            r = {c: float(rs.uniform(0.0, 1.0)) for c in TICK_COLUMNS}
            price *= float(np.exp(rs.normal(0, 0.003)))
            r.update(tn=int(rs.randint(1, 40)), qn=int(rs.randint(0, 60)),
                     tv=float(rs.randint(100, 9000)),
                     tlast=price, topen=price * 0.999, thi=price * 1.002, tlo=price * 0.997,
                     tlasta=price, tp15=price * 0.9995, tp45=price * 1.0004,
                     qbid=price * 0.999, qask=price * 1.001, qbsl=float(rs.randint(1, 900)),
                     qasl=float(rs.randint(1, 900)), qbs=float(rs.randint(1, 900)),
                     qas=float(rs.randint(1, 900)), qbd=float(rs.uniform(1e3, 1e5)),
                     qad=float(rs.uniform(1e3, 1e5)), ts15f=float(rs.normal(0, 100)))
            r["tdv"] = r["tv"] * price
            r["tvq"] = r["tv"] * 0.9
            rows.append({"sym": sym, "m": DAY_MINUTE_ZERO + m, **r})
    frame = pd.DataFrame(rows, columns=["sym", "m", *TICK_COLUMNS])
    frame["m"] = frame["m"].astype(np.int32)
    for c in INTEGER_COLUMNS:
        frame[c] = frame[c].astype(np.int64)
    import duckdb

    con = duckdb.connect()
    con.register("frame", frame)
    con.execute(f"COPY (SELECT * FROM frame) TO '{path}' (FORMAT parquet)")
    con.close()
    return path


def reference(folder: Path, day: str, symbols: list[str], minutes: list[int]) -> Path:
    """A prepared reference folder (l1feat.refprep's form): plain files, the news months of the
    72-hour window, manifest. News items at the given minutes of the day and the day before."""
    folder.mkdir()
    d = datetime.date.fromisoformat(day)
    old = (d - datetime.timedelta(days=40)).isoformat()
    lines = {
        "splits.jsonl": [{"execution_date": old, "ticker": symbols[1], "split_from": 1,
                          "split_to": 2}],
        "shares-outstanding-asof-a.jsonl": [
            {"ticker": s, "date": (d - datetime.timedelta(days=60 + i)).isoformat(),
             "share_class_shares_outstanding": 1e6 * (i + 1), "list_date": "2010-01-04",
             "sic_code": "2834" if i % 2 else "7372"} for i, s in enumerate(symbols)],
        "short-interest-a.jsonl": [
            {"settlement_date": (d - datetime.timedelta(days=k)).isoformat(), "ticker": s,
             "short_interest": 1e4 * (i + k), "days_to_cover": 1.0 + i} for i, s in
            enumerate(symbols) for k in (20, 50)],
    }
    for name, rws in lines.items():
        (folder / name).write_text("".join(json.dumps(r) + "\n" for r in rws))
    months: dict[str, list[str]] = {}
    for k, m in enumerate(minutes):
        for back in (0, 1):
            stamp = _ny_utc((d - datetime.timedelta(days=back)).isoformat(), m, 17)
            item = {"id": f"n{k}-{back}", "published_utc": stamp,
                    "publisher": {"name": PUBLISHER}, "tickers": [symbols[k % len(symbols)]]}
            months.setdefault(stamp[:7], []).append(json.dumps(item, sort_keys=True))
    for k in range(4):  # every month of the 72-hour window has its file, empty or not
        months.setdefault((d - datetime.timedelta(days=k)).isoformat()[:7], [])
    for month, items in months.items():
        (folder / f"news-{month}.jsonl").write_text("".join(i + "\n" for i in items))
    manifest = {"plain": sorted(lines), "files": {p.name: _ident(p) for p in folder.iterdir()}}
    (folder / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    return folder


def benzinga(folder: Path, day: str, symbols: list[str], minutes: list[int],
             first: str) -> Path:
    """news-<first>_<day>.jsonl.gz and earnings-<first>_<day>.jsonl.gz: articles at the given
    minutes of the day and on earlier days, quarterly reports of every symbol."""
    folder.mkdir()
    d = datetime.date.fromisoformat(day)
    chans = [["movers"], ["price target"], ["fda", "earnings"], ["hot"]]
    arts = []
    for k, m in enumerate(minutes):
        tk = [symbols[k % len(symbols)]] + ([symbols[(k + 1) % len(symbols)]] if k % 3 else [])
        arts.append({"benzinga_id": f"a{k}", "published": _ny_utc(day, m, 30), "tickers": tk,
                     "channels": chans[k % len(chans)]})
    for k in range(1, 15):
        old = (d - datetime.timedelta(days=k)).isoformat()
        arts.append({"benzinga_id": f"o{k}", "published": _ny_utc(old, 300 + k),
                     "tickers": [symbols[k % len(symbols)]], "channels": ["movers"]})
    reports = []
    for i, s in enumerate(symbols):
        for q in range(4):
            rd = (d - datetime.timedelta(days=91 * q + 5 + i)).isoformat()
            reports.append({"benzinga_id": f"e{i}-{q}", "ticker": s, "date": rd,
                            "time": "16:05:00", "last_updated": _ny_utc(rd, 900),
                            "actual_eps": 0.1 * (q + 1), "estimated_eps": 0.1 * q - 0.05})
    for kind, rws in (("news", arts), ("earnings", reports)):
        with gzip.open(folder / f"{kind}-{first}_{day}.jsonl.gz", "wt") as f:
            f.write("".join(json.dumps(r) + "\n" for r in rws))
    return folder


def fl_facts(path: Path, day: str, symbols: list[str]) -> Path:
    head = "ticker,cik,end,filed,known,float_usd,share,src,form\n"
    d = datetime.date.fromisoformat(day)
    body = ""
    for i, s in enumerate(symbols[::2]):
        filed = (d - datetime.timedelta(days=30 + i)).isoformat()
        known = (d - datetime.timedelta(days=27 + i)).isoformat()
        usd, share = 6e8 * (i + 1), 0.3 + 0.1 * i
        body += f"{s},{i + 1},2023-06-30,{filed},{known},{usd!r},{share!r},1,10-K\n"
    path.write_text(head + body)
    return path


def research() -> SimpleNamespace:
    """The pinned builder imported the way the training and the research tools import it: by
    its module names, from the pinned folder (the table code puts it on sys.path)."""
    w = features.table_code()
    import l1feat  # type: ignore[import-not-found]
    import l1labels  # type: ignore[import-not-found]
    import wm2ref16  # type: ignore[import-not-found]
    import wm2tk_l1  # type: ignore[import-not-found]

    settings = l1feat.settings(str(features.PINNED_DIR / full.SETTINGS_FILE))
    return SimpleNamespace(feat=l1feat, labels=l1labels, ref=wm2ref16, tk=wm2tk_l1, w=w,
                           settings=settings)


def asof(day: str, minute: int, given: full.FullInputs, work: Path) -> full.FullInputs:
    """The snapshot of `given` as of the mark of `minute`: the training's own `cut_inputs`."""
    import contextlib
    import io

    work.mkdir()
    with contextlib.redirect_stdout(io.StringIO()):
        tk, bz, ref, _ = research().feat.cut_inputs(day, minute, str(given.tick_file),
                                                    str(given.benzinga), str(work),
                                                    str(given.reference))
    return full.FullInputs(Path(tk), Path(ref), Path(bz), given.fl_facts)


def market_day(day: str) -> MarketDay:
    """The bot calendar's day; a day before its coverage (the open archive) is built with the
    standard hours, for the parity tests only."""
    d = datetime.date.fromisoformat(day)
    calendar = SessionCalendar.load(CALENDAR_DIR)
    if any(a <= d <= b for a, b in calendar.coverage):
        return calendar.day(d)

    def at(h: int, m: int = 0) -> datetime.datetime:
        return datetime.datetime(d.year, d.month, d.day, h, m, tzinfo=NEW_YORK)

    return MarketDay(d, at(4), at(9, 30), at(16), at(20), False, "test: open archive day")


RECEIPT_LAG_NS = 10**9  # the fixtures' receipts: one second after a tick or bar minute ends
NS_PER_MINUTE = 60 * 10**9


def early(day: str) -> datetime.datetime:
    """The fixtures' receipt of a record known before the session: 04:00 New York the day
    before."""
    d = datetime.date.fromisoformat(day) - datetime.timedelta(days=1)
    return datetime.datetime(d.year, d.month, d.day, 4, tzinfo=NEW_YORK)


def _text(instant: datetime.datetime) -> str:
    return instant.astimezone(datetime.UTC).isoformat()


def _ns(instant: datetime.datetime) -> int:
    return int(pd.Timestamp(instant).value)


def received_bars(bars: pd.DataFrame, late: dict | None = None) -> pd.DataFrame:
    """The bars with their receipt: one second after the bar's minute ends; `late` maps
    (ticker, window_start) to another receipt."""
    out = bars.copy()
    out[full.RECEIPT_COLUMN] = out.window_start + NS_PER_MINUTE + RECEIPT_LAG_NS
    for (ticker, start), when in (late or {}).items():
        hit = (out.ticker == ticker) & (out.window_start == start)
        assert hit.sum() == 1
        out.loc[hit, full.RECEIPT_COLUMN] = _ns(when)
    return out


def capture(given: full.FullInputs, day: str, root: Path, late=None,
            omit=None) -> full.FullInputs:
    """`given` as the bot would capture it: every record with its receipt. Tick rows one second
    after their minute ends, every other record `early(day)`; `late(source, record)` may return
    another receipt, and a record for which `omit(source, record)` is true is left out (source:
    "tk", "bz", "ref", "fl")."""
    import duckdb

    late = late or (lambda _source, _record: None)
    omit = omit or (lambda _source, _record: False)
    root.mkdir()
    con = duckdb.connect()
    frame = con.execute(f"SELECT * FROM read_parquet('{given.tick_file}')").df()
    frame = frame[[not omit("tk", r) for r in frame.to_dict("records")]].reset_index(drop=True)
    start = pd.Timestamp(day, tz="America/New_York").value
    stamps = start + (frame.m.astype(np.int64) + 1) * NS_PER_MINUTE + RECEIPT_LAG_NS
    frame[full.RECEIPT_COLUMN] = [
        _ns(w) if (w := late("tk", r)) else s for r, s in
        zip(frame.to_dict("records"), stamps.tolist(), strict=True)]
    frame[full.RECEIPT_COLUMN] = frame[full.RECEIPT_COLUMN].astype(np.int64)
    con.register("frame", frame)
    con.execute(f"COPY (SELECT * FROM frame) TO '{root / 'tk.parquet'}' (FORMAT parquet)")
    con.close()

    def stamped(source: str, record: dict) -> str:
        return json.dumps({**record, full.RECEIPT_FIELD: _text(late(source, record)
                                                               or early(day))}) + "\n"

    (root / "bz").mkdir()
    for path in sorted(Path(given.benzinga).glob("*.jsonl.gz")):
        rows = [json.loads(x) for x in gzip.open(path, "rt") if x.strip()]
        with gzip.open(root / "bz" / path.name, "wt") as f:
            f.write("".join(stamped("bz", r) for r in rows if not omit("bz", r)))
    (root / "ref").mkdir()
    manifest = json.loads((Path(given.reference) / "manifest.json").read_text())
    for name in manifest["files"]:
        rows = [json.loads(x) for x in (Path(given.reference) / name).read_text().splitlines()
                if x.strip()]
        (root / "ref" / name).write_text("".join(stamped("ref", r) for r in rows
                                                 if not omit("ref", r)))
        manifest["files"][name] = _ident(root / "ref" / name)
    (root / "ref" / "manifest.json").write_text(json.dumps(manifest, sort_keys=True))
    lines = Path(given.fl_facts).read_text().splitlines()
    head = lines[0].split(",")
    facts = [(x, dict(zip(head, x.split(","), strict=True))) for x in lines[1:]]
    body = [f"{x},{_text(late('fl', r) or early(day))}" for x, r in facts if not omit("fl", r)]
    (root / "fl.csv").write_text("\n".join([f"{lines[0]},{full.RECEIPT_FIELD}", *body]) + "\n")
    return full.FullInputs(root / "tk.parquet", root / "ref", root / "bz", root / "fl.csv")

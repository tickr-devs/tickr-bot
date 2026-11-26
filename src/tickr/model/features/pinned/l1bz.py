# Layer 1 features, group "bz" (paid, 13 columns): Benzinga news and past earnings. Definitions: spec thread, l1-feature-list-rulings-2026-11-23.md (b9cf732e) and the rulings of 05:38Z / 05:39Z 2026-11-23.
# Only an article's published time, tickers and channels are read; a report's ticker, date, time, last_updated, actual and estimated EPS. No title, no body, no text model.
# TIME. The decision mark of a row's minute m is the END of minute m (New York; m counts from 04:00). An article is KNOWN at published + bz_lag_s (published is UTC). Every window is measured on the time
#   an article becomes known, both ends: known in (mark - window, mark]. A report's date + time are New York time; last_updated is UTC.
# NEWS, per (ticker, m):
#   bz_n60 bz_n1d bz_n3d  articles known in the last bz_n60_s / bz_n1d_s / bz_n3d_s seconds. An article naming several tickers counts for each.
#   bz_min                minutes from the last known article to the mark; missing when none since 00:00 New York of the bz_hist_days-th prior trading day.
#   bz_rk                 bz_n1d / max(mean, bz_rk_floor), mean = articles known from 00:00 New York of the bz_hist_days-th prior trading day to 00:00 of the row's day, divided by bz_hist_days; missing when bz_n1d and the mean are both 0.
#   bz_solo               1 when the last known article (same window as bz_min) names exactly this one ticker, else 0; missing when bz_min is missing.
#   bz_c_earn bz_c_anl bz_c_deal bz_c_mov   1 when an article known in the last bz_n1d_s seconds carries a channel of the set (exact channel names, settings key bz_channels), else 0.
# EARNINGS, per (ticker, m), past reports only (report time <= mark):
#   er_since   trading days from the last report's date to the row's day (0 = today).
#   er_surp    (actual EPS - estimated EPS) / max(|estimated EPS|, er_surp_floor) of that report, known only from max(report time, last_updated); missing before that or without an estimate or an actual.
#   er_next    median gap in trading days between the ticker's last er_gap_reports reports, minus er_since; missing with fewer than er_min_reports past reports. No calendar of future dates is read.
# SEALED DATES (ruling 05:39Z, no switch): no sealed file is listed or read. A news value of a day whose window (the bz_hist_days-th prior trading day .. the day) holds a sealed date is MISSING.
#   er_since and er_surp are missing when a sealed date lies after the last known report's date and not after the row's day (a report inside the sealed window cannot be ruled out); er_next also when one lies after the oldest report it uses.
# Missing values are counted per feature by the caller. A month without its file is refused, never read as "no news". Every number comes from the settings file (keys below); none lives here except unit factors (60 s, 86400 s).
# The sealed rule and the calendar are the label builder's own (l1labels.SEAL, HARD_LAST, calendar()).
# KNOWN LIMIT, not corrected: the ticker of an article or report is the one Benzinga's file carries today, not point-in-time: a reused or renamed ticker can carry another company's news or reports.
# ONE REPORT PER (ticker, date) (spec 06:24Z 2026-11-23): of several rows with different ids for one ticker and date the one with the earliest time is kept; the dropped rows are counted (Earnings.same_date).
# KNOWN LIMIT, not corrected: the ticker of an article or report is the one Benzinga's file carries today, not point-in-time: a reused or renamed ticker can carry another company's news or reports.
# ONE REPORT PER (ticker, date) (spec 06:24Z 2026-11-23): of several rows with different ids for one ticker and date the one with the earliest time is kept; the dropped rows are counted (Earnings.same_date).
import sys, os, json, gzip, ast, bisect, datetime, zoneinfo, re, calendar, numpy as np
import l1labels as L

LIMIT = "the ticker of an article or report is the one Benzinga's file carries today, not point-in-time: a reused or renamed ticker can carry another company's news or reports"

NEWS = 'bz_n60 bz_n1d bz_n3d bz_min bz_rk bz_solo bz_c_earn bz_c_anl bz_c_deal bz_c_mov'.split(); ER = 'er_since er_surp er_next'.split(); BZCOLS = NEWS + ER
FLAGS = ('earn', 'anl', 'deal', 'mov')
KEYS = ('bz_lag_s', 'bz_n60_s', 'bz_n1d_s', 'bz_n3d_s', 'bz_hist_days', 'bz_rk_floor', 'bz_channels', 'er_surp_floor', 'er_gap_reports', 'er_min_reports')
NY = zoneinfo.ZoneInfo('America/New_York'); UTC = datetime.timezone.utc


def refuse(cause, text=''): raise SystemExit('REFUSED: %s%s' % (cause, (': ' + text) if text else ''))
def day_add(d, n): return (datetime.date.fromisoformat(d) + datetime.timedelta(days=n)).isoformat()
def ny_s(day, hh=0, mm=0, ss=0): x = datetime.date.fromisoformat(day); return int(datetime.datetime(x.year, x.month, x.day, hh, mm, ss, tzinfo=NY).timestamp())
STAMP = re.compile(r'^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d+))?(Z|[+-]\d{2}:?\d{2})$')   # a UTC or offset time stamp; the fraction may have any number of digits


def utc_s(s):
    # the first whole second at which a stamp is in the past: a stamp with a fraction of a second is rounded UP (Codex 060, point 4). A value known at 10:00:00.2 is not known
    # at the 10:00:00 mark; cutting the fraction off would have said it was. Read from the TEXT (Codex 061): the whole seconds, then one more when any digit after them is not
    # zero, however fine (a time library keeps six digits and would cut 13:30:00.000000001 to 13:30:00). A stamp of another form, or without a zone, refuses: no guess.
    m = STAMP.match(s) if isinstance(s, str) else None
    if not m: refuse('bz_stamp', 'a time stamp that is not YYYY-MM-DDTHH:MM:SS[.fraction] with Z or an offset')
    y, mo, d, hh, mi, ss = (int(x) for x in m.group(1, 2, 3, 4, 5, 6)); z = m.group(8); off = 0
    if z != 'Z': zz = z.replace(':', ''); off = (1 if zz[0] == '+' else -1) * (int(zz[1:3]) * 3600 + int(zz[3:5]) * 60)
    try: datetime.datetime(y, mo, d, hh, mi, ss)
    except ValueError: refuse('bz_stamp', 'a time stamp with an impossible date or time')
    return calendar.timegm((y, mo, d, hh, mi, ss)) - off + (1 if m.group(7) and m.group(7).strip('0') else 0)


def span_sealed(d0, d1):
    # is any calendar date of d0..d1 (both ends) a sealed date
    if d0 > d1: return False
    return (d0 <= L.SEAL[1] and d1 >= L.SEAL[0]) or d1 > L.HARD_LAST


def settings_ok(S):
    for k in KEYS:
        if k not in S: refuse('bz_setting_missing', k)
    if sorted(S['bz_channels']) != sorted(FLAGS) or any(not v or any(x != x.lower() for x in v) for v in S['bz_channels'].values()): refuse('bz_channels', 'four sets of lower-case channel names: %s' % ', '.join(FLAGS))
    if not (S['bz_n60_s'] <= S['bz_n1d_s'] <= S['bz_n3d_s']) or S['er_min_reports'] > S['er_gap_reports'] or S['er_min_reports'] < 2: refuse('bz_setting_order')


def files(folder, kind, d0, d1):
    # the files <kind>-<first>_<last>.jsonl.gz of the open folder whose dates overlap d0..d1; every date of d0..d1 that is not sealed must be covered. A file whose name holds a sealed date is refused (a mixed file counts as sealed).
    got = []
    for f in sorted(os.listdir(folder)):
        if not (f.startswith(kind + '-') and f.endswith('.jsonl.gz')): continue
        a, b = f[len(kind) + 1:-len('.jsonl.gz')].split('_')
        if len(a) != 10 or len(b) != 10 or a > b: refuse('bz_file_name', f)
        if b < d0 or a > d1: continue
        if span_sealed(a, b): refuse('sealed_date', 'a %s file of the folder holds a sealed date' % kind)   # the whole interval of the name, also past the last open day: nothing is clipped (Codex 060, point 1)
        got.append((a, b, os.path.join(folder, f)))
    d = d0
    while d <= d1:
        if not L.sealed(d) and not any(a <= d <= b for a, b, _ in got): refuse('bz_coverage', 'no %s file holds %s' % (kind, d))
        d = day_add(d, 1)
    return got


def _list(v):
    if isinstance(v, str): v = ast.literal_eval(v) if v.strip() else []
    if v is None: return []
    if not isinstance(v, (list, tuple)) or any(not isinstance(x, str) for x in v): refuse('bz_row_list')
    return list(v)


def rows(path):
    with gzip.open(path, 'rt', encoding='utf-8') as f:
        for l in f:
            if l.strip(): yield json.loads(l)


class News:
    def __init__(self, folder, cal, day, S):
        settings_ok(S); self.S = S; i = bisect.bisect_left(cal, day)
        if i >= len(cal) or cal[i] != day: refuse('unknown_trading_day', day)
        if i < S['bz_hist_days']: refuse('bz_history', 'fewer than %d trading days before %s in the calendar' % (S['bz_hist_days'], day))
        self.day = day; self.d0 = cal[i - S['bz_hist_days']]; self.touch = span_sealed(self.d0, day); self.by = {}; self.n = 0
        if self.touch: return
        lo = ny_s(self.d0); hi = ny_s(day_add(day, 1)); lag = S['bz_lag_s']; sets = [set(S['bz_channels'][f]) for f in FLAGS]; seen = set(); acc = {}
        for a, b, p in files(folder, 'news', day_add(self.d0, -1), day):   # one day earlier: an article published just before midnight UTC of the first day
            for j in rows(p):
                if 'benzinga_id' not in j or not j.get('published'): refuse('bz_row', 'an article without id or published time in %s' % os.path.basename(p))
                if j['benzinga_id'] in seen: continue
                seen.add(j['benzinga_id']); t = utc_s(j['published']) + lag
                if t < lo or t >= hi: continue
                tk = _list(j.get('tickers')); ch = {c.lower() for c in _list(j.get('channels'))}; fl = sum(1 << k for k, s in enumerate(sets) if ch & s)
                for s in set(tk): acc.setdefault(s, []).append((t, len(set(tk)), fl))
                self.n += 1
        for s, v in acc.items():
            v.sort(); t = np.array([x[0] for x in v], np.int64); self.by[s] = (t, np.array([x[1] for x in v], np.int64), np.array([[(x[2] >> k) & 1 for x in v] for k in range(len(FLAGS))], np.int64).cumsum(axis=1))

    def add(self, t):
        # t = table of the day (sym, m); adds the ten news columns in place
        S = self.S; n = len(t); o = {c: np.full(n, np.nan, np.float32) for c in NEWS}
        if not self.touch:
            mark = ny_s(self.day, 4) + (t['m'].values.astype(np.int64) + 1) * 60; day0 = ny_s(self.day)
            for c in NEWS:
                if c not in ('bz_min', 'bz_rk', 'bz_solo'): o[c][:] = 0
            for sym, ix in t.groupby('sym').indices.items():
                a = self.by.get(sym)
                if a is None: continue
                tk, nt, cf = a; mk = mark[ix]; i = np.searchsorted(tk, mk, side='right'); w = lambda sec: i - np.searchsorted(tk, mk - sec, side='right')
                n1 = w(S['bz_n1d_s']); o['bz_n60'][ix] = w(S['bz_n60_s']); o['bz_n1d'][ix] = n1; o['bz_n3d'][ix] = w(S['bz_n3d_s']); has = i > 0; last = np.maximum(i - 1, 0)
                o['bz_min'][ix] = np.where(has, (mk - tk[last]) / 60.0, np.nan); o['bz_solo'][ix] = np.where(has, (nt[last] == 1).astype(np.float64), np.nan)
                mean = np.searchsorted(tk, day0, side='left') / float(S['bz_hist_days']); o['bz_rk'][ix] = np.where((n1 == 0) & (mean == 0), np.nan, n1 / max(mean, S['bz_rk_floor']))
                j = np.searchsorted(tk, mk - S['bz_n1d_s'], side='right'); z = np.zeros(1, np.int64)
                for k, f in enumerate(FLAGS): c = np.concatenate([z, cf[k]]); o['bz_c_' + f][ix] = (c[i] - c[j] > 0)
        for c in NEWS: t[c] = o[c]


class Earnings:
    def __init__(self, folder, cal, day, S):
        settings_ok(S); self.S = S; self.cal = cal; self.day = day; self.by = {}; self.skipped = 0; self.same_date = 0; seen = set(); acc = {}; fs = [x for x in files_from(folder, 'earnings', day)]
        for a, b, p in fs:
            for j in rows(p):
                if 'benzinga_id' not in j: refuse('bz_row', 'a report without id in %s' % os.path.basename(p))
                if j['benzinga_id'] in seen: continue
                seen.add(j['benzinga_id'])
                if not j.get('ticker') or not j.get('date') or not j.get('time'): self.skipped += 1; continue
                hh, mm, ss = (int(x) for x in j['time'].split(':')); rt = ny_s(j['date'], hh, mm, ss); kn = max(rt, utc_s(j['last_updated'])) if j.get('last_updated') else None
                a_, e_ = j.get('actual_eps'), j.get('estimated_eps'); sp = (a_ - e_) / max(abs(e_), S['er_surp_floor']) if a_ is not None and e_ is not None and kn is not None else np.nan
                acc.setdefault(j['ticker'], []).append((rt, j['date'], sp, kn if kn is not None else rt))
        for s, v in acc.items():
            v.sort(); one = []
            for x in v:   # sorted by report time: the earliest row of a date is met first
                if one and one[-1][1] == x[1]: self.same_date += 1
                else: one.append(x)
            v = one; self.by[s] = (np.array([x[0] for x in v], np.int64), [x[1] for x in v], np.array([bisect.bisect_right(cal, x[1]) for x in v], np.int64), np.array([x[2] for x in v], np.float64), np.array([x[3] for x in v], np.int64))

    def add(self, t):
        S = self.S; n = len(t); o = {c: np.full(n, np.nan, np.float32) for c in ER}; mark = ny_s(self.day, 4) + (t['m'].values.astype(np.int64) + 1) * 60; pos = bisect.bisect_right(self.cal, self.day); G = S['er_gap_reports']
        for sym, ix in t.groupby('sym').indices.items():
            a = self.by.get(sym)
            if a is None: continue
            rt, rd, rp, sp, kn = a; mk = mark[ix]; i = np.searchsorted(rt, mk, side='right')
            for u in np.unique(i):
                if u == 0: continue
                r = ix[i == u]; last = u - 1
                if span_sealed(day_add(rd[last], 1), self.day): continue   # a report inside the sealed window cannot be ruled out
                since = pos - rp[last]; o['er_since'][r] = since; o['er_surp'][r] = np.where(kn[last] <= mark[r], sp[last], np.nan)
                first = max(u - G, 0)
                if u - first >= S['er_min_reports'] and not span_sealed(day_add(rd[first], 1), self.day): o['er_next'][r] = float(np.median(np.diff(rp[first:u]))) - since
        for c in ER: t[c] = o[c]


def files_from(folder, kind, day):
    # every open file of the kind that starts at or before the day; the day itself must be covered and no gap other than sealed dates may lie between the first file and the day
    names = sorted(f for f in os.listdir(folder) if f.startswith(kind + '-') and f.endswith('.jsonl.gz'))
    if not names: refuse('bz_coverage', 'no %s file' % kind)
    first = min(f[len(kind) + 1:len(kind) + 11] for f in names)
    if first > day: refuse('bz_coverage', 'no %s file holds %s' % (kind, day))
    return files(folder, kind, first, day)


def test():
    import tempfile, shutil, pandas as pd
    n = [0]
    def ck(c, t):
        n[0] += 1
        if not c: sys.exit('bz selftest failed: ' + t)
    def refuses(f, cause, t):
        try: f()
        except SystemExit as e: ck(str(e).startswith('REFUSED: ' + cause), '%s (got %s)' % (t, e)); return str(e)
        ck(False, t + ' (no refusal)')
    eq = lambda a, b: (a != a and b != b) or abs(float(a) - float(b)) < 1e-6
    cal = L.calendar(); S = {'bz_lag_s': 60, 'bz_n60_s': 3600, 'bz_n1d_s': 86400, 'bz_n3d_s': 259200, 'bz_hist_days': 20, 'bz_rk_floor': 0.05, 'er_surp_floor': 0.01, 'er_gap_reports': 4, 'er_min_reports': 3,
                             'bz_channels': {'earn': ['earnings', 'guidance'], 'anl': ['price target'], 'deal': ['m&a', 'fda'], 'mov': ['movers']}}
    D = '2024-03-06'; ck(D in cal and cal[cal.index(D) - 20] == '2024-02-06', 'the test day and its 20th prior trading day')
    td = tempfile.mkdtemp()
    try:
        def put(name, rws):
            with gzip.open(os.path.join(td, name), 'wt') as f:
                for r in rws: f.write(json.dumps(r) + '\n')
        def art(i, pub, tk, ch): return {'benzinga_id': str(i), 'published': pub, 'tickers': tk, 'channels': ch, 'title': 'never read', 'body': 'never read'}
        # 2024-03-06 is in winter time: 10:00 New York = 15:00 UTC. Row minute m: 09:59 = 359 (mark 10:00:00), 10:00 = 360 (mark 10:01:00)
        A = [art(1, '2024-03-06T14:59:00Z', ['AAA'], ['earnings', 'news']),            # known 10:00:00 New York = the mark of m 359
             art(2, '2024-03-06T14:59:01Z', "['AAA', 'BBB']", "['Price Target']"),       # known 10:00:01: after the mark of 359, inside 360; lists as text; channel in another case
             art(3, '2024-03-05T15:10:00Z', ['AAA'], ['m&a']),                           # yesterday 10:10 New York: inside 24 h of the 10:00 mark
             art(4, '2024-03-04T09:00:00Z', ['AAA'], ['movers']),                        # two days and six hours earlier: inside 72 h, outside 24 h
             art(5, '2024-02-20T15:00:00Z', ['AAA', 'CCC'], []), art(5, '2024-02-20T15:00:00Z', ['AAA', 'CCC'], []),   # the same id twice: counted once
             art(6, '2024-02-05T15:00:00Z', ['AAA'], ['fda']),                           # before the 20-day window: not counted anywhere
             art(7, '2024-03-06T21:30:00Z', ['AAA'], ['fda'])]                           # after every row of the test
        put('news-2024-02-01_2024-02-29.jsonl.gz', [x for x in A if x['published'] < '2024-03']); put('news-2024-03-01_2024-03-31.jsonl.gz', [x for x in A if x['published'] >= '2024-03'])
        t0 = lambda: pd.DataFrame({'sym': ['AAA', 'AAA', 'BBB', 'BBB', 'CCC', 'ZZZ'], 'm': [359, 360, 359, 360, 360, 360]})
        N = News(td, cal, D, S); t = t0(); N.add(t); r = t.set_index(['sym', 'm'])
        ck(N.n == 6 and not N.touch, 'six articles of the window, the repeated id once, the one before the window not')
        a = r.loc[('AAA', 359)]; ck(a.bz_n60 == 1 and a.bz_n1d == 2 and a.bz_n3d == 3 and eq(a.bz_min, 0.0) and a.bz_solo == 1, 'AAA at the 10:00 mark: the article known exactly at the mark counts, the one a second later does not')
        ck(a.bz_c_earn == 1 and a.bz_c_anl == 0 and a.bz_c_deal == 1 and a.bz_c_mov == 0, 'channel flags over 24 hours: earnings and yesterday\'s m&a; movers is older; the price target is not known yet')
        ck(eq(a.bz_rk, 2 / (3 / 20.0)), 'bz_rk: two in 24 hours against a mean of three articles in the 20 prior trading days')
        a = r.loc[('AAA', 360)]; ck(a.bz_n60 == 2 and a.bz_n1d == 3 and a.bz_n3d == 4 and eq(a.bz_min, 59 / 60.0) and a.bz_solo == 0 and a.bz_c_anl == 1, 'AAA one minute later: the two-ticker article is known, bz_solo turns 0, its channel in another case counts')
        a = r.loc[('BBB', 359)]; ck(a.bz_n60 == 0 and a.bz_n1d == 0 and a.bz_n3d == 0 and a.bz_min != a.bz_min and a.bz_rk != a.bz_rk and a.bz_solo != a.bz_solo and a.bz_c_anl == 0, 'BBB before its first article: counts 0, bz_min, bz_rk and bz_solo missing')
        a = r.loc[('BBB', 360)]; ck(a.bz_n1d == 1 and eq(a.bz_rk, 1 / 0.05) and a.bz_solo == 0, 'BBB with an article and a zero mean: the floor of the settings')
        a = r.loc[('CCC', 360)]; ck(a.bz_n3d == 0 and eq(a.bz_min, (L.ny_ns(D) // 10 ** 9 + 4 * 3600 + 361 * 60 - (utc_s('2024-02-20T15:00:00Z') + 60)) / 60.0) and eq(a.bz_rk, 0.0) and a.bz_solo == 0, 'CCC: one old article inside the 20-day window')
        a = r.loc[('ZZZ', 360)]; ck(a.bz_n60 == 0 and a.bz_min != a.bz_min and a.bz_c_mov == 0, 'a stock without any article')
        # an article stamped after the mark changes nothing: the same rows with the later articles removed
        put('news-2024-03-01_2024-03-31.jsonl.gz', [x for x in A if '2024-03' <= x['published'] <= '2024-03-06T14:59:00Z']); t2 = t0(); News(td, cal, D, S).add(t2)
        ck(all(eq(t.loc[k, c], t2.loc[k, c]) for k in (0, 2) for c in NEWS) and not eq(t.loc[1, 'bz_n60'], t2.loc[1, 'bz_n60']), 'rows at the 10:00 mark are the same without the articles known after it; the later row is not')
        S2 = dict(S, bz_lag_s=0); t3 = t0(); put('news-2024-03-01_2024-03-31.jsonl.gz', [x for x in A if x['published'] >= '2024-03']); News(td, cal, D, S2).add(t3); ck(t3.loc[0, 'bz_n60'] == 2, 'the lag comes from the settings: with 0 s the second article is inside the 10:00 mark')
        # coverage and sealed dates
        os.remove(os.path.join(td, 'news-2024-02-01_2024-02-29.jsonl.gz')); refuses(lambda: News(td, cal, D, S), 'bz_coverage', 'a month without its file'); put('news-2024-02-01_2024-02-29.jsonl.gz', [])
        t = pd.DataFrame({'sym': ['AAA'], 'm': [360]}); X = News(td, cal, '2026-08-17', S); X.add(t); ck(X.touch and t.loc[0, NEWS].isna().all(), 'a row of 2026-08-17: every news column missing, no file opened')
        i = cal.index('2026-08-17'); far = cal[i + 20]; put('news-2026-08-15_2026-08-31.jsonl.gz', [art(9, '2026-08-20T15:00:00Z', ['AAA'], [])]); put('news-%s_%s.jsonl.gz' % ('2026-09-01', '2026-09-30'), [art(10, far + 'T13:00:00Z', ['AAA'], ['movers'])])
        ck(not span_sealed(cal[i], far) and span_sealed(cal[i - 1], far), 'the 20th trading day after the sealed window is the first whose window is clear'); X = News(td, cal, far, S); t = pd.DataFrame({'sym': ['AAA'], 'm': [360]}); X.add(t)
        ck(not X.touch and t.bz_n60[0] == 0 and t.bz_n1d[0] == 1 and t.bz_c_mov[0] == 1 and eq(t.bz_rk[0], 1 / 0.05), 'a row far enough after the sealed window gives values')
        X = News(td, cal, cal[i + 19], S); ck(X.touch, 'one day earlier the window still touches the sealed dates'); put('news-2026-06-01_2026-07-02.jsonl.gz', []); refuses(lambda: files(td, 'news', '2026-06-15', '2026-06-20'), 'sealed_date', 'a file whose name holds a sealed date')
        os.remove(os.path.join(td, 'news-2026-06-01_2026-07-02.jsonl.gz'))
        put('news-2026-11-01_2026-11-30.jsonl.gz', []); refuses(lambda: files(td, 'news', '2026-11-05', '2026-11-10'), 'sealed_date', 'a file whose name runs past the last open day is refused, not clipped'); os.remove(os.path.join(td, 'news-2026-11-01_2026-11-30.jsonl.gz'))
        put('news-2024-04-01_2024-04-30.jsonl.gz', [{'benzinga_id': '1', 'tickers': ['AAA']}]); os.remove(os.path.join(td, 'news-2024-03-01_2024-03-31.jsonl.gz')); refuses(lambda: News(td, cal, '2024-04-10', S), 'bz_coverage', 'the month before is absent'); put('news-2024-03-01_2024-03-31.jsonl.gz', []); refuses(lambda: News(td, cal, '2024-04-10', S), 'bz_row', 'an article without a published time')
        refuses(lambda: News(td, cal, '2024-03-09', S), 'unknown_trading_day', 'a Saturday'); refuses(lambda: settings_ok({k: v for k, v in S.items() if k != 'bz_lag_s'}), 'bz_setting_missing', 'a missing setting'); refuses(lambda: settings_ok(dict(S, bz_channels={'earn': ['Earnings'], 'anl': ['x'], 'deal': ['x'], 'mov': ['x']})), 'bz_channels', 'a channel name that is not lower case')
        # AVAILABILITY IS ROUNDED UP to the whole second (Codex 060, point 4): equal, before, after; news and reports
        w0 = utc_s('2024-03-06T14:59:00Z'); ck(utc_s('2024-03-06T14:59:00+00:00') == w0 and utc_s('2024-03-06T14:59:00.000Z') == w0 and utc_s('2024-03-06T09:59:00-05:00') == w0, 'a stamp of a whole second is that second, in every form')
        ck(utc_s('2024-03-06T14:58:59.999999Z') == w0 and utc_s('2024-03-06T14:58:59.4Z') == w0 and utc_s('2024-03-06T14:59:00.000001Z') == w0 + 1 and utc_s('2024-03-06T14:59:00.25Z') == w0 + 1, 'a fraction of a second rounds up, never down')
        # Codex 061: finer than a microsecond, read from the text
        ck(utc_s('2024-03-06T14:59:00.000000001Z') == w0 + 1 and utc_s('2024-03-06T14:59:00.0000000000001Z') == w0 + 1 and utc_s('2024-03-06T14:58:59.999999999Z') == w0 and utc_s('2024-03-06T09:59:00.000000001-05:00') == w0 + 1 and utc_s('2024-03-06T09:59:00.000000001-0500') == w0 + 1, 'one nanosecond (or less) after a whole second is the next second')
        ck(utc_s('2024-03-06T14:59:00.000000000Z') == w0 and utc_s('2024-03-06T14:59:00.0Z') == w0 and utc_s('2024-03-06T14:59:00.000000000000+00:00') == w0, 'an all-zero fraction of any length is the whole second')
        ck(utc_s('2024-03-10T06:59:59.5Z') == utc_s('2024-03-10T07:00:00Z') and utc_s('2024-12-31T23:59:59.1Z') == utc_s('2025-01-01T00:00:00Z') and utc_s('2024-02-29T00:00:00Z') - utc_s('2024-02-28T00:00:00Z') == 86400, 'rounding up carries over the minute, the day and the year')
        for bad in ('2024-03-06T14:59:00', '2024-03-06 14:59:00Z', '2024-03-06T14:59Z', '2024-02-30T10:00:00Z', '2024-03-06T25:00:00Z', '2024-03-06T14:59:00.Z', 20240306, None): refuses(lambda: utc_s(bad), 'bz_stamp', 'a stamp without a zone or of another form refuses: %r' % (bad,))
        t4 = tempfile.mkdtemp()
        try:
            def put4(name, rws):
                with gzip.open(os.path.join(t4, name), 'wt') as f:
                    for r in rws: f.write(json.dumps(r) + '\n')
            put4('news-2024-02-01_2024-02-29.jsonl.gz', []); put4('news-2024-03-01_2024-03-31.jsonl.gz', [art(1, '2024-03-06T14:58:59.400Z', ['BEF'], []), art(2, '2024-03-06T14:59:00Z', ['EQU'], []), art(3, '2024-03-06T14:59:00.250Z', ['AFT'], []), art(4, '2024-03-06T14:59:00.000000001Z', ['NAN'], []), art(5, '2024-03-06T14:59:00.000000000Z', ['ZER'], [])])
            t = pd.DataFrame({'sym': ['BEF', 'EQU', 'AFT', 'AFT', 'NAN', 'NAN', 'ZER'], 'm': [359, 359, 359, 360, 359, 360, 359]}); News(t4, cal, D, S).add(t)
            ck(t.bz_n60[4] == 0 and t.bz_n60[5] == 1 and t.bz_n60[6] == 1, 'news (Codex 061): an article known one nanosecond after the 10:00:00 mark does not count at it, it counts at the next mark; an all-zero fraction is the mark itself')
            ck(t.bz_n60[0] == 1 and t.bz_n60[1] == 1, 'news: an article known 0.6 s before the 10:00:00 mark, and one known exactly at it, count at that mark')
            ck(t.bz_n60[2] == 0 and t.bz_n60[3] == 1, 'news: an article known 0.25 s after the 10:00:00 mark does not count at it (cut to the second it did); it counts at the next mark')
            put4('earnings-2025-01-01_2025-12-31.jsonl.gz', [{'benzinga_id': 'b', 'ticker': 'BEF', 'date': '2025-08-18', 'time': '08:30:00', 'last_updated': '2025-08-18T12:39:59.500Z', 'actual_eps': 0.3, 'estimated_eps': 0.1},
                                                             {'benzinga_id': 'q', 'ticker': 'EQU', 'date': '2025-08-18', 'time': '08:30:00', 'last_updated': '2025-08-18T12:40:00Z', 'actual_eps': 0.3, 'estimated_eps': 0.1},
                                                             {'benzinga_id': 'a', 'ticker': 'AFT', 'date': '2025-08-18', 'time': '08:30:00', 'last_updated': '2025-08-18T12:40:00.500Z', 'actual_eps': 0.3, 'estimated_eps': 0.1},
                                                             {'benzinga_id': 'n', 'ticker': 'NAN', 'date': '2025-08-18', 'time': '08:30:00', 'last_updated': '2025-08-18T12:40:00.000000001Z', 'actual_eps': 0.3, 'estimated_eps': 0.1},
                                                             {'benzinga_id': 'z', 'ticker': 'ZER', 'date': '2025-08-18', 'time': '08:30:00', 'last_updated': '2025-08-18T12:40:00.000000000Z', 'actual_eps': 0.3, 'estimated_eps': 0.1}])
            t = pd.DataFrame({'sym': ['BEF', 'EQU', 'AFT', 'AFT', 'NAN', 'NAN', 'ZER'], 'm': [279, 279, 279, 280, 279, 280, 279]}); Earnings(t4, cal, '2025-08-18', S).add(t)   # m 279 = the 08:40:00 mark
            ck(t.er_surp[4] != t.er_surp[4] and eq(t.er_surp[5], 2.0) and eq(t.er_surp[6], 2.0), 'reports (Codex 061): a surprise updated one nanosecond after the 08:40:00 mark is missing at it and known at the next mark; an all-zero fraction is the mark itself')
            ck(eq(t.er_surp[0], 2.0) and eq(t.er_surp[1], 2.0), 'reports: a surprise updated 0.5 s before the 08:40:00 mark, and one updated exactly at it, are known at that mark')
            ck(t.er_since[2] == 0 and t.er_surp[2] != t.er_surp[2] and eq(t.er_surp[3], 2.0), 'reports: a surprise updated 0.5 s after the 08:40:00 mark is missing at it (cut to the second it was known); known at the next mark')
        finally: shutil.rmtree(t4, ignore_errors=True)
        # EARNINGS. The two real rows as synthetic cases: time 17:42:26 with last_updated 21:44:52Z (summer: 17:44:52 New York), and 08:30:00 with 12:40:14Z (08:40:14 New York)
        def rep(i, tk, d, tm, lu, a=None, e=None): x = {'benzinga_id': 'e%d' % i, 'ticker': tk, 'date': d, 'time': tm, 'last_updated': lu}; x.update({} if a is None else {'actual_eps': a}); x.update({} if e is None else {'estimated_eps': e}); return x
        E = [rep(1, 'NK', '2025-08-18', '17:42:26', '2025-08-18T21:44:52Z', -0.16, -0.07), rep(2, 'IG', '2025-08-18', '08:30:00', '2025-08-18T12:40:14Z', -0.03, -0.02),
             rep(3, 'IG', '2025-05-16', '08:30:00', '2025-05-16T12:31:00Z', 0.10, 0.004), rep(4, 'IG', '2025-02-14', '08:30:00', '2025-02-14T13:31:00Z', 0.2), rep(5, 'IG', '2024-11-15', '08:30:00', '2024-11-15T13:31:00Z', 0.2, 0.1),
             rep(6, 'IG', '2024-08-16', '08:30:00', '2024-08-16T12:31:00Z', 0.2, 0.1), rep(7, 'NK', '2025-08-19', '09:00:00', '2025-08-22T12:00:00Z', 1.0, 0.5), {'benzinga_id': 'e8', 'ticker': 'NK', 'date': '2025-08-01'},
             rep(9, 'IG', '2025-02-14', '16:00:00', '2025-02-14T21:05:00Z', 9.0, 1.0)]   # a second row, another id, for the same ticker and date: dropped, counted
        put('earnings-2024-01-01_2024-12-31.jsonl.gz', [x for x in E if x['date'] < '2025']); put('earnings-2025-01-01_2025-12-31.jsonl.gz', [x for x in E if x['date'] >= '2025'])
        D2 = '2025-08-18'; G = Earnings(td, cal, D2, S); ck(G.skipped == 1, 'a report without a time is not used and is counted'); ck(G.same_date == 1 and len(G.by['IG'][1]) == 5 and len(set(G.by['IG'][1])) == 5, 'one report per ticker and date: the later row of 2025-02-14 is dropped and counted (no zero gap in er_next, checked below by its value)')
        # minutes: 08:29 = 269 (mark 08:30:00), 08:39 = 279 (mark 08:40:00), 08:40 = 280 (mark 08:41:00); 17:41 = 821 (mark 17:42:00), 17:42 = 822 (mark 17:43:00), 17:44 = 824 (mark 17:45:00)
        t = pd.DataFrame({'sym': ['IG', 'IG', 'IG', 'IG', 'NK', 'NK', 'NK', 'ZZ'], 'm': [268, 269, 279, 280, 821, 822, 824, 300]}); G.add(t)
        ck(eq(t.er_since[0], cal.index(D2) - cal.index('2025-05-16')) and eq(t.er_surp[0], (0.10 - 0.004) / 0.01), 'before 08:30 the last report is the one of May; a tiny estimate is floored by the settings')
        ck(t.er_since[1] == 0 and t.er_surp[1] != t.er_surp[1], 'at the 08:30:00 mark the report of 08:30:00 New York is the last one (summer time: 12:30 UTC); its surprise is not known before last_updated')
        ck(t.er_since[2] == 0 and t.er_surp[2] != t.er_surp[2] and eq(t.er_surp[3], (-0.03 + 0.02) / 0.02), 'last_updated 12:40:14Z is 08:40:14 New York: missing at the 08:40:00 mark, known at the 08:41:00 mark')
        g = [cal.index(a) - cal.index(b) for a, b in (('2025-08-18', '2025-05-16'), ('2025-05-16', '2025-02-14'), ('2025-02-14', '2024-11-15'))]; ck(eq(t.er_next[3], float(np.median(g)) - 0) and eq(t.er_next[0], float(np.median([g[1], g[2], cal.index('2024-11-15') - cal.index('2024-08-16')])) - t.er_since[0]), 'er_next: the median gap of the last four reports minus er_since')
        ck(t.loc[4, ER].isna().all() and t.er_since[5] == 0 and t.er_surp[5] != t.er_surp[5] and eq(t.er_surp[6], (-0.16 + 0.07) / 0.07) and t.er_next[6] != t.er_next[6], '17:42:26 New York with last_updated 21:44:52Z: no report before 17:42:26, surprise from the 17:45 mark; one report gives no er_next')
        ck(t.loc[7, ER].isna().all(), 'a stock without a report')
        t = pd.DataFrame({'sym': ['NK'], 'm': [400]}); Earnings(td, cal, '2025-08-20', S).add(t); ck(t.er_since[0] == 1 and t.er_surp[0] != t.er_surp[0], 'a report whose last_updated is days later: its surprise is missing, the earlier report\'s is not put in its place')
        E2 = [rep(1, 'IG', '2024-11-15', '08:30:00', '2024-11-15T13:31:00Z', 0.2, 0.1), rep(2, 'IG', '2025-02-14', '08:30:00', '2025-02-14T13:31:00Z', 0.2, 0.1), rep(3, 'IG', '2025-05-16', '08:30:00', '2025-05-16T12:31:00Z', 0.2, 0.1)]
        put('earnings-2024-01-01_2024-12-31.jsonl.gz', [x for x in E2 if x['date'] < '2025']); put('earnings-2025-01-01_2025-12-31.jsonl.gz', [x for x in E2 if x['date'] >= '2025'])
        t = pd.DataFrame({'sym': ['IG'], 'm': [100]}); G = Earnings(td, cal, '2025-05-15', S); G.add(t); ck(t.er_next[0] != t.er_next[0] and t.er_since[0] > 0, 'two past reports: no er_next; a report dated tomorrow is not read')
        # sealed dates: the last known report lies before the sealed window
        put('earnings-2026-01-01_2026-06-30.jsonl.gz', [rep(20, 'IG', '2026-05-15', '08:30:00', '2026-05-15T12:31:00Z', 0.2, 0.1), rep(21, 'NK', '2026-02-13', '08:30:00', '2026-02-13T13:31:00Z', 0.2, 0.1), rep(22, 'NK', '2026-05-15', '08:30:00', '2026-05-15T12:31:00Z', 0.2, 0.1)])
        put('earnings-2026-08-15_2026-11-19.jsonl.gz', [rep(23, 'NK', '2026-08-17', '08:30:00', '2026-08-17T12:31:00Z', 0.3, 0.1)])
        t = pd.DataFrame({'sym': ['IG', 'NK', 'IG', 'NK'], 'm': [300, 300, 100, 100]}); Earnings(td, cal, '2026-08-17', S).add(t)
        ck(t.loc[0, ER].isna().all() and t.loc[2, ER].isna().all(), 'IG on 2026-08-17: the last known report is from May, the sealed weeks lie between: every er column missing')
        ck(t.er_since[1] == 0 and eq(t.er_surp[1], 2.0) and t.er_next[1] != t.er_next[1] and t.loc[3, ER].isna().all(), 'NK reported on 2026-08-17 itself: er_since and er_surp are proven; er_next is not (its older reports lie across the sealed window); before the report: missing')
        t = pd.DataFrame({'sym': ['IG', 'NK'], 'm': [300, 300]}); Earnings(td, cal, '2026-06-30', S).add(t); ck(t.er_since[0] > 0 and t.er_since[1] > 0, 'before the sealed window the same reports give values')
        os.remove(os.path.join(td, 'earnings-2026-01-01_2026-06-30.jsonl.gz')); refuses(lambda: Earnings(td, cal, '2026-08-17', S), 'bz_coverage', 'a missing earnings file between the first one and the day')
    finally: shutil.rmtree(td, ignore_errors=True)
    print('bz selftest passed: %d checks' % n[0])


if __name__ == '__main__':
    if sys.argv[1:] == ['test']: test()
    else: sys.exit('usage: python l1bz.py test   (the module is used by l1feat.py)')

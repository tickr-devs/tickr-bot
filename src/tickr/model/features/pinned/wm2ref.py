# New layer 1: company reference columns for the wide table (wm2.py adds them when WM_REF = the reference folder). Everything is taken AS OF the row's minute, never later:
#   so_log  = log10 of shares outstanding (latest record dated at least SO_LAG calendar days before the day; shares-outstanding-asof*.jsonl); mcap = log10(shares x previous close); age = log10 of days since the listing date
#   si_pct  = short interest / shares outstanding; si_dtc = days to cover (capped at DTC_CAP); si_chg = short interest / the settlement before it - 1.
#             A settlement is used only from SI_LAG calendar days after its date (FINRA publishes it about 8 business days later, after the close; the worst case over year-end is 12 calendar days, so 14 leaves a day of margin; the file has no publication date).
#   nw_n    = news items about the stock since 16:00 of the previous calendar day and before minute m; nw_3d = the same over the last 72 hours;
#             nw_min = minutes since the last news item (NaN when none in 72 hours). News timestamps are the publisher's; an item counts only when it is strictly older than the row's minute.
#   sic2    = first two digits of the SIC industry code (same as-of record as the shares); sx_n = table rows (in-list stocks) of the same sic2 in the same minute; sx_med = median change from the previous close of those rows;
#             sx_chg = the row's change minus sx_med (sx_med and sx_chg are empty when the group has fewer than SXMIN rows). The table must already hold the column chg.
#   Splits (splits.jsonl): a shares record or a short-interest settlement dated before a split's execution date is multiplied by split_to / split_from from the execution date on,
#             so shares, short interest and the split-adjusted previous close are on the same basis (audit 2026-11-20: without it mcap was off by the split ratio until the next filing).
#   News safety lag: an item counts only when it is at least NW_LAG minutes older than the row's minute (feed arrival is later than the publisher's stamp).
# No price rows are read here. The free-float file is NOT used: it is a snapshot of today, so it would look into the future.
import os, glob, json, numpy as np, pandas as pd
REFCOLS = 'so_log mcap age si_pct si_dtc si_chg nw_n nw_3d nw_min sic2 sx_n sx_med sx_chg'.split(); SXMIN = 3; SI_LAG = 14; SO_LAG = 3; NW_LAG = 2; DTC_CAP = 50.0; H72 = 72 * 60; PREV16 = 12 * 60   # minutes from 16:00 of the previous calendar day to 04:00


def _day(s): return int(pd.Timestamp(s).value // 86400000000000)


class Ref:
    def __init__(self, folder):
        so = {}; si = {}; nw = {}; sp = {}
        for f in sorted(glob.glob(os.path.join(folder, 'splits.jsonl'))):
            for l in open(f):
                j = json.loads(l)
                if j.get('ticker') and j.get('execution_date') and j.get('split_from') and j.get('split_to'): sp.setdefault(j['ticker'], {})[_day(j['execution_date'])] = float(j['split_to']) / float(j['split_from'])
        self.sp = {t: (np.array(sorted(d)), np.array([d[k] for k in sorted(d)])) for t, d in sp.items()}
        for f in sorted(glob.glob(os.path.join(folder, 'shares-outstanding-asof*.jsonl'))):
            for l in open(f):
                j = json.loads(l); v = j.get('share_class_shares_outstanding') or j.get('weighted_shares_outstanding')
                if v and j.get('ticker') and j.get('date'): so.setdefault(j['ticker'], {})[_day(j['date'])] = (float(v), _day(j['list_date']) if j.get('list_date') else -1, int(str(j['sic_code'])[:2]) if str(j.get('sic_code') or '')[:2].isdigit() else -1)
        self.so = {t: (np.array(sorted(d)), np.array([d[k][0] for k in sorted(d)]), np.array([d[k][1] for k in sorted(d)]), np.array([d[k][2] for k in sorted(d)])) for t, d in so.items()}
        for f in sorted(glob.glob(os.path.join(folder, 'short-interest*.jsonl'))):
            for l in open(f):
                j = json.loads(l)
                if not j.get('ticker') or j.get('short_interest') is None or j.get('days_to_cover') is None: continue
                si.setdefault(j['ticker'], {})[_day(j['settlement_date'])] = (float(j['short_interest']), float(j['days_to_cover']))
        self.si = {t: (np.array(sorted(d)), np.array([d[k][0] for k in sorted(d)]), np.array([d[k][1] for k in sorted(d)])) for t, d in si.items()}
        seen = set(); T = []; K = []
        for f in sorted(glob.glob(os.path.join(folder, 'news-*.jsonl'))):
            for l in open(f):
                j = json.loads(l)
                if 'id' not in j or 'published_utc' not in j or j['id'] in seen: continue
                seen.add(j['id']); T.append(j['published_utc']); K.append(j.get('tickers') or [])
        if T:
            tm = pd.to_datetime(pd.Series(T), utc=True, format='ISO8601').dt.tz_convert('America/New_York').dt.tz_localize(None).values.astype('datetime64[m]').astype(np.int64)
            for m_, ks in zip(tm, K):
                for k in ks: nw.setdefault(k, []).append(m_)
        self.nw = {t: np.sort(np.array(v, np.int64)) for t, v in nw.items()}
        print('reference: shares for', len(self.so), 'tickers, short interest for', len(self.si), ', news for', len(self.nw), 'tickers,', len(seen), 'news items', flush=True)

    def adj(self, sym, since, day):
        # product of split_to / split_from of the splits of sym executed after day number since and not later than day
        s = self.sp.get(sym)
        if s is None: return 1.0
        return float(np.prod(s[1][np.searchsorted(s[0], since, side='right'):np.searchsorted(s[0], day, side='right')]))

    def add(self, t, dt, pc):
        # t = table of one day (columns sym, m), pc = previous close of every row; adds REFCOLS in place
        n = len(t); day = _day(dt); base = int(pd.Timestamp(dt + ' 04:00').value // 60000000000); now = base + t.m.values.astype(np.int64); nwn = now - NW_LAG + 1; o = {k: np.full(n, np.nan, np.float32) for k in REFCOLS}   # an item stamped at minute a counts when a <= now - NW_LAG
        for sym, ix in t.groupby('sym').indices.items():
            s = self.so.get(sym); sh = np.nan
            if s is not None:
                k = np.searchsorted(s[0], day - SO_LAG, side='right') - 1          # latest record dated at least SO_LAG calendar days before today (audit 2026-11-20: record dates sit about a day before the filing date)
                if k >= 0:
                    sh = s[1][k] * self.adj(sym, s[0][k], day); o['so_log'][ix] = np.log10(sh); o['mcap'][ix] = np.log10(np.maximum(sh * pc[ix], 1.0))
                    if s[2][k] >= 0 and day > s[2][k]: o['age'][ix] = np.log10(day - s[2][k])
                    if s[3][k] >= 0: o['sic2'][ix] = s[3][k]
            s = self.si.get(sym)
            if s is not None:
                k = np.searchsorted(s[0], day - SI_LAG, side='right') - 1
                if k >= 0:
                    o['si_dtc'][ix] = min(s[2][k], DTC_CAP)
                    if sh == sh and sh > 0: o['si_pct'][ix] = s[1][k] * self.adj(sym, s[0][k], day) / sh
                    if k > 0 and s[1][k - 1] > 0: o['si_chg'][ix] = s[1][k] / (s[1][k - 1] * self.adj(sym, s[0][k - 1], s[0][k])) - 1
            a = self.nw.get(sym)
            if a is not None:
                i = np.searchsorted(a, nwn[ix], side='left'); o['nw_n'][ix] = np.maximum(i - np.searchsorted(a, base - PREV16, side='left'), 0); o['nw_3d'][ix] = i - np.searchsorted(a, now[ix] - H72, side='left')
                last = np.where(i > 0, now[ix] - a[np.maximum(i - 1, 0)], H72 + 1).astype(np.float32); o['nw_min'][ix] = np.where(last <= H72, last, np.nan)
            else: o['nw_n'][ix] = 0; o['nw_3d'][ix] = 0
        g = pd.DataFrame({'m': t.m.values, 's': o['sic2'], 'c': t.chg.values.astype(np.float64)}); gb = g[g.s == g.s].groupby(['m', 's']).c; cnt = gb.transform('count').reindex(g.index); med = gb.transform('median').reindex(g.index).where(cnt >= SXMIN)
        o['sx_n'] = cnt.values.astype(np.float32); o['sx_med'] = med.values.astype(np.float32); o['sx_chg'] = (g.c - med).values.astype(np.float32)
        for k in REFCOLS: t[k] = o[k]

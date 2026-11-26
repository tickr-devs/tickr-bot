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
#   News items are taken only from the publishers of NW_PUB: the only ones present in every month of 2021-11..2026-11 (audit 2026-11-20: Zacks absent 2024-09..2026-02, Benzinga absent from 2026-08, MarketWatch and Seeking Alpha gone since 2024).
#             Even their density halves from 2024, so two forms that do not depend on the density are added: nw_any = 1 when nw_n > 0, nw_rk = place (0..1) of nw_3d among the table rows of the same minute.
#   News safety lag: an item counts only when it is at least NW_LAG minutes older than the row's minute (feed arrival is later than the publisher's stamp).
# wm2ref16 (2026-11-23, Codex 061 point 1; wm2ref15.py 96a19cf3 is kept unchanged beside the older sets): the splits file holds OPEN execution dates only, so a split executed on a
#             closed date (`closed` = the sealed window as two day numbers, given by the caller) cannot be known. A factor over a span that holds a closed date is therefore MISSING, and with it
#             every value that needs it: so_log, mcap and si_pct when the shares record is older than the window and the row's day is after it; si_pct also when the settlement is;
#             si_chg when the two settlements lie on the two sides. age, sic2, si_dtc and the news columns need no factor and stay. Nothing else differs from wm2ref15.
#             `stats` (after add): rows by the age in calendar days of the shares record and of the settlement used, and the cells this rule left missing, for the day's record.
# No price rows are read here. The free-float file is NOT used: it is a snapshot of today, so it would look into the future.
import os, glob, json, numpy as np, pandas as pd
REFCOLS = 'so_log mcap age si_pct si_dtc si_chg nw_n nw_3d nw_min sic2 sx_n sx_med sx_chg nw_any nw_rk'.split(); NW_PUB = ('GlobeNewswire Inc.', 'The Motley Fool'); SXMIN = 3; SI_LAG = 14; SO_LAG = 3; NW_LAG = 2; DTC_CAP = 50.0; H72 = 72 * 60; PREV16 = 12 * 60   # minutes from 16:00 of the previous calendar day to 04:00


def _day(s): return int(pd.Timestamp(s).value // 86400000000000)


class Ref:
    def __init__(self, folder, closed):
        # closed = (first, last) day numbers of the sealed window; a split executed inside it is not in the file
        so = {}; si = {}; nw = {}; sp = {}; self.closed = (int(closed[0]), int(closed[1])); self.stats = None
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
                if 'id' not in j or 'published_utc' not in j or j['id'] in seen or (j.get('publisher') or {}).get('name') not in NW_PUB: continue
                seen.add(j['id']); T.append(j['published_utc']); K.append(j.get('tickers') or [])
        if T:
            tm = pd.to_datetime(pd.Series(T), utc=True, format='ISO8601').dt.tz_convert('America/New_York').dt.tz_localize(None).values.astype('datetime64[m]').astype(np.int64)
            for m_, ks in zip(tm, K):
                for k in ks: nw.setdefault(k, []).append(m_)
        self.nw = {t: np.sort(np.array(v, np.int64)) for t, v in nw.items()}
        print('reference: shares for', len(self.so), 'tickers, short interest for', len(self.si), ', news for', len(self.nw), 'tickers,', len(seen), 'news items', flush=True)

    def adj(self, sym, since, day):
        # product of split_to / split_from of the splits of sym executed after day number since and not later than day; NaN when a day of (since, day] is closed: its splits are not known
        if since < self.closed[1] and day >= self.closed[0]: return float('nan')
        s = self.sp.get(sym)
        if s is None: return 1.0
        return float(np.prod(s[1][np.searchsorted(s[0], since, side='right'):np.searchsorted(s[0], day, side='right')]))

    def add(self, t, dt, pc):
        # t = table of one day (columns sym, m), pc = previous close of every row; adds REFCOLS in place
        n = len(t); day = _day(dt); base = int(pd.Timestamp(dt + ' 04:00').value // 60000000000); now = base + t.m.values.astype(np.int64); nwn = now - NW_LAG + 1; o = {k: np.full(n, np.nan, np.float32) for k in REFCOLS}; soa = np.full(n, -1, np.int64); sia = np.full(n, -1, np.int64); cut = dict.fromkeys(('so_log', 'mcap', 'si_pct', 'si_chg'), 0)   # an item stamped at minute a counts when a <= now - NW_LAG
        for sym, ix in t.groupby('sym').indices.items():
            s = self.so.get(sym); sh = np.nan; so_k = False
            if s is not None:
                k = np.searchsorted(s[0], day - SO_LAG, side='right') - 1          # latest record dated at least SO_LAG calendar days before today (audit 2026-11-20: record dates sit about a day before the filing date)
                if k >= 0:
                    so_k = True; f = self.adj(sym, s[0][k], day); soa[ix] = day - s[0][k]
                    if f == f: sh = s[1][k] * f; o['so_log'][ix] = np.log10(sh); o['mcap'][ix] = np.log10(np.maximum(sh * pc[ix], 1.0))
                    else: cut['so_log'] += len(ix); cut['mcap'] += len(ix)
                    if s[2][k] >= 0 and day > s[2][k]: o['age'][ix] = np.log10(day - s[2][k])
                    if s[3][k] >= 0: o['sic2'][ix] = s[3][k]
            s = self.si.get(sym)
            if s is not None:
                k = np.searchsorted(s[0], day - SI_LAG, side='right') - 1
                if k >= 0:
                    o['si_dtc'][ix] = min(s[2][k], DTC_CAP); sia[ix] = day - s[0][k]; f = self.adj(sym, s[0][k], day)
                    if sh == sh and sh > 0 and f == f: o['si_pct'][ix] = s[1][k] * f / sh
                    elif (sh != sh and so_k) or (sh == sh and sh > 0): cut['si_pct'] += len(ix)   # a shares record and a settlement exist; a factor over the window is the only thing missing
                    if k > 0 and s[1][k - 1] > 0:
                        f = self.adj(sym, s[0][k - 1], s[0][k])
                        if f == f: o['si_chg'][ix] = s[1][k] / (s[1][k - 1] * f) - 1
                        else: cut['si_chg'] += len(ix)
            a = self.nw.get(sym)
            if a is not None:
                i = np.searchsorted(a, nwn[ix], side='left'); o['nw_n'][ix] = np.maximum(i - np.searchsorted(a, base - PREV16, side='left'), 0); o['nw_3d'][ix] = i - np.searchsorted(a, now[ix] - H72, side='left')
                last = np.where(i > 0, now[ix] - a[np.maximum(i - 1, 0)], H72 + 1).astype(np.float32); o['nw_min'][ix] = np.where(last <= H72, last, np.nan)
            else: o['nw_n'][ix] = 0; o['nw_3d'][ix] = 0
        o['nw_any'] = (o['nw_n'] > 0).astype(np.float32); o['nw_rk'] = pd.Series(o['nw_3d']).groupby(t.m.values).rank(pct=True).values.astype(np.float32)
        g = pd.DataFrame({'m': t.m.values, 's': o['sic2'], 'c': t.chg.values.astype(np.float64)}); gb = g[g.s == g.s].groupby(['m', 's']).c; cnt = gb.transform('count').reindex(g.index); med = gb.transform('median').reindex(g.index).where(cnt >= SXMIN)
        o['sx_n'] = cnt.values.astype(np.float32); o['sx_med'] = med.values.astype(np.float32); o['sx_chg'] = (g.c - med).values.astype(np.float32)
        for k in REFCOLS: t[k] = o[k]
        def ages(a):   # rows by the age of the record used, calendar days
            v = a[a >= 0]
            return {'rows_with_a_record': int(len(v)), 'rows_without': int(n - len(v)), 'age_days_min': int(v.min()) if len(v) else None, 'age_days_median': float(np.median(v)) if len(v) else None, 'age_days_p90': float(np.percentile(v, 90)) if len(v) else None, 'age_days_max': int(v.max()) if len(v) else None}
        self.stats = {'shares_record': ages(soa), 'short_interest_settlement': ages(sia), 'cells_missing_because_a_split_factor_would_cross_the_sealed_window': cut}

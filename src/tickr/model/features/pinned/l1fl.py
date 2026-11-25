# Layer 1 features, group "fl" (free, 4 columns): the public float a company states on the cover of its annual report (SEC XBRL fact EntityPublicFloat). Spec thread, rulings b9cf732e (b) and 05:38Z 2026-11-23.
#   fl_has    1 when a fact is known for the stock on the row's day, else 0
#   fl_logd   log10 of the float dollars of the latest known fact
#   fl_age    calendar days from the day the fact became known to the row's day; known = filing date + fl_lag_days
#   fl_share  float dollars / market cap at the fact's date (its `end`): close of the stock on the last trading day at or before `end`, times shares:
#             Massive company-level weighted shares as of `end` (latest record dated at or before it, split-adjusted to `end`), else the same filing's cover shares (EntityCommonStockSharesOutstanding).
#             MISSING where `end` is before the first open bar day (the older folders stay closed to every feature script), where `end`'s bar day is sealed, where the stock has no bar that day, or where no shares exist.
# Annual facts only (forms of the settings key fl_forms: 10-K, its amendment 10-K/A and the transition report 10-KT, spec ruling 06:09Z 2026-11-23); each fact is known from ITS OWN filing date + fl_lag_days.
# The latest known fact wins (by the day it became known, then by `end`); its form is kept in the facts file and counted in the day's record. Nothing is clipped: a share above 1 stays as computed.
# PRICE BASE (spec 06:24Z 2026-11-23): the day bar files hold the UNADJUSTED prices of their day (seen on real data: NVDA closed 467.50 on 2023-07-28, before its 10-for-1 split of 2024-06-10), so the close of the
#   bar day times the shares as of `end` is the market cap of that day; no split after `end` is applied to either. Cover shares (src 2) are dated at the cover date, usually later than `end`: they are brought
#   back to `end` through the splits executed after `end` and not later than the cover date (forward when the cover date is the earlier one).
# KNOWN LIMIT, not corrected: the ticker to company link is today's (ticker-cik.csv), not point-in-time: a reused or renamed ticker can carry another company's facts.
# Two steps, so that the per-day builder opens no bar file of another year:
#   python l1fl.py facts <folder of the open-only shards sec-cover-facts-*.jsonl> <ticker-cik.csv> <reference folder: shares-outstanding-asof*.jsonl, splits.jsonl> <day bar folder: DAY.csv.gz> <settings.json> <out fl-facts.csv>
#        one line per (ticker, fact): ticker, cik, end, filed, known, float_usd, share, src, form   (src: 1 Massive shares, 2 the filing's cover shares, 0 no shares, -1 end before the open bars, -2 sealed bar day, -3 no bar of the stock,
#        -4 the split factor between the shares date and `end` would cross the sealed window)
# OPEN INPUTS ONLY (Codex 061, points 1 and 2; spec rulings 16:50Z and 16:58Z 2026-11-23). The SEC facts come from the open-only shards of refshard2b.py: a fact is there only when its filing date AND its period end are
#   open dates; a fact of a sealed date met here refuses the run (sec_fact_not_open). The facts of a company are joined over the shards in name order. The splits file holds open execution dates only, so a
#   factor over a span that holds a sealed-window day is not known: the share is then missing (src -4), never computed without the unknown splits. fl_share is a ratio fixed at `end`; no later split touches it.
#   Fl(<fl-facts.csv>).add(table of a day, day)   adds the four columns
# Day bar files are opened BY NAME only (the folder also holds sealed days; they are never opened: src -2). The sealed rule, the calendar and the first open day are the label builder's own.
# Every number comes from the settings file (fl_lag_days, fl_forms); none lives here.
import sys, os, json, csv, gzip, glob, bisect, datetime, hashlib, numpy as np
import l1labels as L

LIMIT = "the ticker to company link is today's (ticker-cik.csv), not point-in-time: a reused or renamed ticker can carry another company's facts"
FLCOLS = 'fl_share fl_logd fl_has fl_age'.split(); KEYS = ('fl_lag_days', 'fl_forms'); HEAD = ['ticker', 'cik', 'end', 'filed', 'known', 'float_usd', 'share', 'src', 'form']


def refuse(cause, text=''): raise SystemExit('REFUSED: %s%s' % (cause, (': ' + text) if text else ''))
def day_add(d, n): return (datetime.date.fromisoformat(d) + datetime.timedelta(days=n)).isoformat()
def dnum(d): return datetime.date.fromisoformat(d).toordinal()


def closes(folder, day, want):
    # close of the wanted tickers in the day bar file of `day`, opened by name. A ticker is text.
    p = os.path.join(folder, day + '.csv.gz')
    if not os.path.isfile(p): refuse('day_bar_file_missing', day)
    out = {}
    with gzip.open(p, 'rt', newline='') as f:
        for r in csv.DictReader(f):
            if r['ticker'] in want: out[r['ticker']] = float(r['close'])
    return out


def facts(secf, cikf, ref, bars, setf, out):
    S = json.load(open(setf))
    for k in KEYS:
        if k not in S: refuse('fl_setting_missing', k)
    cal = L.calendar(); by_cik = {}
    for r in csv.DictReader(open(cikf, newline='')): by_cik.setdefault(int(r['cik']), []).append(r['ticker'])
    sh = {}; sp = {}
    for f in sorted(glob.glob(os.path.join(ref, 'shares-outstanding-asof*.jsonl'))):
        for l in open(f):
            j = json.loads(l)
            if j.get('ticker') and j.get('date') and j.get('weighted_shares_outstanding'): sh.setdefault(j['ticker'], {})[j['date']] = float(j['weighted_shares_outstanding'])
    sh = {t: (sorted(d), [d[k] for k in sorted(d)]) for t, d in sh.items()}
    for l in open(os.path.join(ref, 'splits.jsonl')):
        j = json.loads(l)
        if j.get('ticker') and j.get('execution_date') and j.get('split_from') and j.get('split_to'): sp.setdefault(j['ticker'], []).append((j['execution_date'], float(j['split_to']) / float(j['split_from'])))
    def adj(t, a, b):   # product of the split factors executed after date a and not later than b; None when a day of (a, b] lies in the sealed window: its splits are not in the file
        if a < L.SEAL[1] and b >= L.SEAL[0]: return None
        x = 1.0
        for d, k in sp.get(t, ()):
            if a < d <= b: x *= k
        return x
    rows = []; need = {}
    shards = sorted(glob.glob(os.path.join(secf, 'sec-cover-facts-*.jsonl'))); comp = {}
    if not shards: refuse('sec_shards_missing', 'no sec-cover-facts-*.jsonl in the folder')
    for f in shards:
        for l in open(f):
            j = json.loads(l)
            for x in j['facts']:
                if not isinstance(x.get('filed'), str) or not isinstance(x.get('end'), str) or L.sealed(x['filed']) or L.sealed(x['end']): refuse('sec_fact_not_open', 'a fact of %s has a filing date or a period end that is not an open date' % os.path.basename(f))
            comp.setdefault(int(j['cik']), []).extend(j['facts'])
    for cik, fs in comp.items():
        j = {'cik': cik, 'facts': fs}; tks = by_cik.get(cik, [])
        if not tks: continue
        cover = {}
        for x in j['facts']:
            if x.get('c') == 'EntityCommonStockSharesOutstanding' and x.get('val') and x.get('accn') and x.get('end'):
                if x['accn'] not in cover or x['end'] > cover[x['accn']][0]: cover[x['accn']] = (x['end'], float(x['val']))
        for x in j['facts']:
            if x.get('c') != 'EntityPublicFloat' or x.get('u') != 'USD' or x.get('form') not in S['fl_forms'] or not x.get('val') or float(x['val']) <= 0 or not x.get('end') or not x.get('filed'): continue
            for t in tks: rows.append([t, int(j['cik']), x['end'], x['filed'], day_add(x['filed'], S['fl_lag_days']), float(x['val']), cover.get(x['accn']), x['form']])
    rows.sort(key=lambda r: (r[0], r[4], r[2], r[3], r[5])); seen = set(); keep = []
    for r in rows:
        k = (r[0], r[2], r[3], r[5])
        if k not in seen: seen.add(k); keep.append(r)
    for r in keep:   # the bar day of each fact: the last trading day at or before `end`
        i = bisect.bisect_right(cal, r[2]) - 1; r.append(cal[i] if i >= 0 and r[2] >= L.ARCH_FIRST else None)
        if r[8] and not L.sealed(r[8]): need.setdefault(r[8], set()).add(r[0])
    px = {d: closes(bars, d, w) for d, w in sorted(need.items())}; lines = []; cnt = {}
    for t, cik, end, filed, known, val, cov, form, bd in keep:
        share = ''; src = 0
        if bd is None: src = -1
        elif L.sealed(bd): src = -2
        elif t not in px[bd]: src = -3
        else:
            s = sh.get(t); k = bisect.bisect_right(s[0], end) - 1 if s else -1; n = None
            if k >= 0: a = adj(t, s[0][k], end); n = s[1][k] * a if a is not None else None; src = 1 if a is not None else -4
            elif cov: a = adj(t, end, cov[0]) if cov[0] >= end else adj(t, cov[0], end); n = (cov[1] / a if cov[0] >= end else cov[1] * a) if a is not None else None; src = 2 if a is not None else -4
            if src == -4: pass
            elif n and n > 0 and px[bd][t] > 0: share = repr(val / (n * px[bd][t]))
            else: src = 0
        cnt[src] = cnt.get(src, 0) + 1; lines.append([t, str(cik), end, filed, known, repr(val), share, str(src), form])
    tmp = out + '.part'
    with open(tmp, 'w', newline='') as f: w = csv.writer(f, lineterminator='\n'); w.writerow(HEAD); w.writerows(lines)
    os.replace(tmp, out)
    print('fl facts: %d lines for %d tickers; by source of the shares %s; day bar files opened %d; md5 %s' % (len(lines), len({x[0] for x in lines}), ' '.join('%d: %d' % (k, cnt[k]) for k in sorted(cnt)), len(px), hashlib.md5(open(out, 'rb').read()).hexdigest()))


class Fl:
    def __init__(self, path):
        self.by = {}
        with open(path, newline='') as f:
            r = csv.reader(f)
            if next(r) != HEAD: refuse('fl_facts_form')
            for t, cik, end, filed, known, val, share, src, form in r: self.by.setdefault(t, []).append((known, end, float(val), float(share) if share else np.nan, form))
        for t in self.by: self.by[t].sort()

    def add(self, t, day):
        # returns the number of rows by the form of the fact used
        n = len(t); o = {c: np.full(n, np.nan, np.float32) for c in FLCOLS}; o['fl_has'][:] = 0; used = {}
        for sym, ix in t.groupby('sym').indices.items():
            a = self.by.get(sym)
            if not a: continue
            k = bisect.bisect_right(a, (day, '9999', np.inf, np.inf, '')) - 1
            if k < 0: continue
            known, end, val, share, form = a[k]; used[form] = used.get(form, 0) + len(ix); o['fl_has'][ix] = 1; o['fl_logd'][ix] = np.log10(val); o['fl_age'][ix] = dnum(day) - dnum(known); o['fl_share'][ix] = share
        for c in FLCOLS: t[c] = o[c]
        return used


def test():
    import tempfile, shutil, pandas as pd
    n = [0]
    def ck(c, t):
        n[0] += 1
        if not c: sys.exit('fl selftest failed: ' + t)
    eq = lambda a, b: (a != a and b != b) or abs(float(a) - float(b)) < 1e-6 * max(1.0, abs(float(b)))
    td = tempfile.mkdtemp()
    try:
        ref = os.path.join(td, 'ref'); bars = os.path.join(td, 'bars'); os.makedirs(ref); os.makedirs(bars)
        def bar(day, rows):
            with gzip.open(os.path.join(bars, day + '.csv.gz'), 'wt', newline='') as f: f.write('ticker,volume,open,close,high,low,window_start,transactions\n' + ''.join('%s,1,1,%s,1,1,0,1\n' % r for r in rows))
        F = lambda c, end, val, filed, form, accn: {'c': c, 'u': 'USD' if c == 'EntityPublicFloat' else 'shares', 'end': end, 'val': val, 'filed': filed, 'form': form, 'accn': accn}
        P, C = 'EntityPublicFloat', 'EntityCommonStockSharesOutstanding'
        sec = [{'cik': 1, 'facts': [F(P, '2023-06-30', 600e6, '2024-02-20', '10-K', 'a1'), F(C, '2024-02-10', 11e6, '2024-02-20', '10-K', 'a1'),      # NA: Massive shares exist; 2-for-1 split between the record and `end`
                                               F(P, '2022-06-30', 500e6, '2023-02-21', '10-K', 'a0'), F(P, '2023-06-30', 999e6, '2023-08-01', '10-Q', 'q1'),        # a quarterly form is not an annual fact
                                               F(P, '2021-06-30', 400e6, '2022-02-22', '10-K', 'a9')]},                                                             # `end` before the first open bar day
               {'cik': 2, 'facts': [F(P, '2023-07-01', 80e6, '2024-03-01', '10-K', 'b1'), F(C, '2024-02-25', 4e6, '2024-03-01', '10-K', 'b1'), F(C, '2024-02-20', 9e6, '2024-03-01', '10-K', 'b1')]},   # BB: no Massive record, cover shares; `end` a Saturday
               {'cik': 3, 'facts': [F(P, '2026-08-15', 70e6, '2026-09-20', '10-K', 'c1'), F(P, '2023-06-30', 60e6, '2024-02-20', '10-K', 'c0'), F(P, '2022-06-30', 0, '2023-02-20', '10-K', 'c9')]},   # CC: a sealed bar day; no bar of the stock; a zero value
               {'cik': 9, 'facts': [F(P, '2023-06-30', 1e6, '2024-02-20', '10-K', 'z')]},
               {'cik': 4, 'facts': [F(P, '2023-06-30', 30e6, '2024-02-20', '10-K', 'd1'), F(P, '2023-06-30', 33e6, '2024-04-10', '10-K/A', 'd2'), F(P, '2022-12-31', 20e6, '2023-03-10', '10-KT', 'd0')]}]             # DD: an amendment known from its own filing date; a transition report                                                                # a company without a ticker in the list
        sec.append({'cik': 5, 'facts': [F(P, '2026-08-18', 90e6, '2026-09-21', '10-K', 'x1'), F(P, '2026-08-18', 91e6, '2026-09-22', '10-K', 'y1'), F(C, '2026-06-20', 3e6, '2026-09-22', '10-K', 'y1'), F(P, '2026-06-30', 50e6, '2026-08-20', '10-K', 'w1')]})   # XX, YY: factors that would cross the window
        sd = os.path.join(td, 'sec'); os.makedirs(sd); A, B = 'sec-cover-facts-1900-01-01_2026-06-30.jsonl', 'sec-cover-facts-2026-08-15_2026-11-19.jsonl'
        for nm, keep in ((A, lambda x: x['filed'] <= '2026-06-30'), (B, lambda x: x['filed'] >= '2026-08-15')):   # as refshard2b.py writes them: a company line per shard with the facts filed in its interval
            open(os.path.join(sd, nm), 'w').write(''.join(json.dumps({'cik': c['cik'], 'facts': [x for x in c['facts'] if keep(x)]}) + '\n' for c in sec if any(keep(x) for x in c['facts'])))
        open(os.path.join(td, 'cik.csv'), 'w').write('ticker,cik\nNA,1\nNB,1\nBB,2\nCC,3\nDD,4\nXX,5\nYY,5\n')
        open(os.path.join(ref, 'shares-outstanding-asof.jsonl'), 'w').write(''.join(json.dumps(x) + '\n' for x in [{'ticker': 'NA', 'date': '2023-06-01', 'weighted_shares_outstanding': 5e6, 'share_class_shares_outstanding': 1}, {'ticker': 'NA', 'date': '2023-07-03', 'weighted_shares_outstanding': 77e6},
                                                                                                             {'ticker': 'NA', 'date': '2022-06-15', 'weighted_shares_outstanding': 4e6}, {'ticker': 'XX', 'date': '2026-06-20', 'weighted_shares_outstanding': 2e6}]))
        open(os.path.join(ref, 'splits.jsonl'), 'w').write(json.dumps({'ticker': 'NA', 'execution_date': '2023-06-15', 'split_from': 1.0, 'split_to': 2.0}) + '\n' + json.dumps({'ticker': 'NA', 'execution_date': '2023-07-10', 'split_from': 1.0, 'split_to': 3.0}) + '\n' + json.dumps({'ticker': 'NB', 'execution_date': '2023-12-01', 'split_from': 1.0, 'split_to': 2.0}) + '\n' + json.dumps({'ticker': 'NB', 'execution_date': '2024-02-15', 'split_from': 1.0, 'split_to': 5.0}) + '\n')
        bar('2023-06-30', [('NA', 50.0), ('NB', 55.0), ('BB', 99.0)]); bar('2026-08-18', [('XX', 10.0), ('YY', 10.0)]); bar('2026-06-30', [('XX', 10.0), ('YY', 10.0)]); bar('2022-06-30', [('NA', 100.0), ('NB', 100.0)]); bar('2022-12-30', [('ZZ', 1.0)])
        setf = os.path.join(td, 'set.json'); open(setf, 'w').write(json.dumps({'fl_lag_days': 3, 'fl_forms': ['10-K', '10-K/A', '10-KT']})); out = os.path.join(td, 'fl-facts.csv')
        facts(sd, os.path.join(td, 'cik.csv'), ref, bars, setf, out); R = list(csv.DictReader(open(out, newline=''))); g = lambda t, end: [r for r in R if r['ticker'] == t and r['end'] == end]
        ck(len(R) == 18 and {r['ticker'] for r in R} == {'NA', 'NB', 'BB', 'CC', 'DD', 'XX', 'YY'} and all(r['form'] in ('10-K', '10-K/A', '10-KT') for r in R), 'annual facts only, one line per ticker of the company and fact, the zero value and the company without a ticker left out (%d)' % len(R))
        r = g('NA', '2023-06-30')[0]; ck(r['known'] == '2024-02-23' and r['src'] == '1' and eq(r['share'], 600e6 / (5e6 * 2.0 * 50.0)), 'NA: Massive shares as of `end` (the record of 06-01, not the later one), the split of 06-15 applied, the one of 07-10 (after `end`) not, and the close of the bar day is taken as it is (unadjusted); known = filed + 3 days')
        r = g('NB', '2023-06-30')[0]; ck(r['src'] == '2' and eq(r['share'], 600e6 / (11e6 / 2.0 * 55.0)), 'NB (another ticker of the same company): no Massive record, the filing\'s own cover shares brought back to `end` through the 2-for-1 split executed between `end` and the cover date (the split after the cover date is not applied), its own close')
        r = g('BB', '2023-07-01')[0]; ck(r['src'] == '2' and eq(r['share'], 80e6 / (4e6 * 99.0)), 'BB: `end` on a Saturday takes the bar of the trading day before; of two cover facts of the filing the later one')
        ck(g('NA', '2021-06-30')[0]['src'] == '-1' and g('NA', '2021-06-30')[0]['share'] == '' and eq(g('NA', '2021-06-30')[0]['float_usd'], 400e6), '`end` before the first open bar day: share missing, the float dollars stay')
        ck(g('CC', '2026-08-15')[0]['src'] == '-2' and g('CC', '2023-06-30')[0]['src'] == '-3', 'a sealed bar day (the Friday before a period end on Saturday 2026-08-15) is not opened; a stock without a bar on the day has no share')
        ck(not os.path.exists(os.path.join(bars, '2026-08-14.csv.gz')), 'the sealed day bar file did not even exist: it was never asked for')
        x = g('XX', '2026-08-18'); ck([r['src'] for r in x] == ['-4', '-4'] and all(r['share'] == '' for r in x) and eq(x[0]['float_usd'], 90e6), 'XX (Codex 061): the shares record is dated before the sealed window and the period end after it: the splits of the window are not known, the share is missing (src -4), the float dollars stay')
        y = g('YY', '2026-08-18'); ck([r['src'] for r in y] == ['0', '-4'] and y[1]['share'] == '', 'YY: no Massive record; cover shares dated before the window for a period end after it: missing too (the fact without cover shares has no shares at all)')
        ck(g('XX', '2026-06-30')[0]['src'] == '1' and eq(g('XX', '2026-06-30')[0]['share'], 50e6 / (2e6 * 10.0)), 'XX: a period end before the window with a record before it needs no unknown factor, whenever it was filed')
        bad = os.path.join(td, 'secbad'); os.makedirs(bad)
        for i_, (fd, en) in enumerate((('2026-07-15', '2025-12-31'), ('2026-09-20', '2026-07-31'), ('2026-11-20', '2025-12-31'), (None, '2025-12-31'))):
            open(os.path.join(bad, A), 'w').write(json.dumps({'cik': 1, 'facts': [dict(F(P, en, 1e6, fd, '10-K', 'k'))]}) + '\n')
            try: facts(bad, os.path.join(td, 'cik.csv'), ref, bars, setf, out + 'b'); ck(False, 'a fact of a sealed date is refused (%d)' % i_)
            except SystemExit as e: ck(str(e).startswith('REFUSED: sec_fact_not_open') and not os.path.exists(out + 'b'), 'a fact filed in the window, with its period end in the window, filed after the last open day or without a filing date refuses the run (%d)' % i_)
        try: facts(os.path.join(td, 'none'), os.path.join(td, 'cik.csv'), ref, bars, setf, out + 'b'); ck(False, 'no shards')
        except SystemExit as e: ck(str(e).startswith('REFUSED: sec_shards_missing'), 'a folder without shards refuses')
        os.remove(os.path.join(bars, '2022-06-30.csv.gz'))
        try: facts(sd, os.path.join(td, 'cik.csv'), ref, bars, setf, out + '2'); ck(False, 'a missing open day bar file is refused')
        except SystemExit as e: ck(str(e).startswith('REFUSED: day_bar_file_missing'), 'a missing open day bar file is refused')
        X = Fl(out); t = pd.DataFrame({'sym': ['NA', 'NA', 'BB', 'CC', 'ZZ', 'NA'], 'm': [1, 2, 1, 1, 1, 3]})
        X.add(t, '2024-02-23'); ck(t.fl_has.tolist() == [1, 1, 0, 1, 0, 1] and t.fl_age[0] == 0 and eq(t.fl_logd[0], np.log10(600e6)) and eq(t.fl_share[0], 6.0 / 5.0), 'on the day a fact becomes known it is used (age 0); a share above 1 is not clipped')
        ck(np.isnan(t.fl_logd[2]) and np.isnan(t.fl_share[2]) and np.isnan(t.fl_age[2]) and t.fl_has[4] == 0, 'no known fact: fl_has 0, the rest missing')
        ck(eq(t.fl_logd[3], np.log10(60e6)) and np.isnan(t.fl_share[3]) and t.fl_age[3] == 0, 'CC: a known fact without a share: fl_logd, fl_has and fl_age are unaffected')
        X.add(t, '2024-02-22'); ck(eq(t.fl_logd[0], np.log10(500e6)) and t.fl_age[0] == dnum('2024-02-22') - dnum('2023-02-24') and eq(t.fl_share[0], 500e6 / (4e6 * 100.0)), 'the day before: the earlier fact, with its age')
        X.add(t, '2022-02-25'); ck(eq(t.fl_logd[0], np.log10(400e6)) and np.isnan(t.fl_share[0]) and t.fl_has[0] == 1, 'a fact whose `end` is before the open bars: share missing, counted as a fact')
        X.add(t, '2022-02-24'); ck(t.fl_has[0] == 0, 'before the first fact is known')
        t = pd.DataFrame({'sym': ['DD', 'DD'], 'm': [1, 2]}); u = X.add(t, '2024-04-12'); ck(eq(t.fl_logd[0], np.log10(30e6)) and u == {'10-K': 2}, 'DD: the day before the amendment is known the original 10-K fact is used; the form used is counted by rows')
        u = X.add(t, '2024-04-13'); ck(eq(t.fl_logd[0], np.log10(33e6)) and t.fl_age[0] == 0 and u == {'10-K/A': 2}, 'DD: the amendment is used from ITS OWN filing date + 3 days')
        u = X.add(t, '2023-03-13'); ck(eq(t.fl_logd[0], np.log10(20e6)) and u == {'10-KT': 2}, 'DD: a transition report is an annual fact')
        t = pd.DataFrame({'sym': ['CC'], 'm': [1]}); X.add(t, '2026-10-01'); ck(eq(t.fl_logd[0], np.log10(70e6)) and np.isnan(t.fl_share[0]) and t.fl_age[0] == dnum('2026-10-01') - dnum('2026-09-23'), 'a fact whose bar day is sealed: its float dollars stay, its share is missing')
    finally: shutil.rmtree(td, ignore_errors=True)
    print('fl selftest passed: %d checks' % n[0])


if __name__ == '__main__':
    a = sys.argv[1:]
    if a == ['test']: test()
    elif a[:1] == ['facts'] and len(a) == 7: facts(*a[1:])
    else: sys.exit('usage: python l1fl.py facts <folder of sec-cover-facts-*.jsonl shards> <ticker-cik.csv> <reference folder> <day bar folder> <settings.json> <out> | test')

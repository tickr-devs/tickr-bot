# Layer 1 features, group "tk2": the 20 columns of the tick table (tk61, frozen converter wm2tick-v11) that wm2tk.py never reads, as 21 features.
# Form ruled by the spec thread 05:36Z 2026-11-23 on the table l1features/tk2-proposal-2026-11-23.md (md5 d509f6b7): at the row's minute ONLY, no windows, no ranks, scale-free:
#   a price            = ratio to tlast minus 1                 (k2_open k2_hi k2_lo k2_lasta k2_p15)
#   a count or volume  = share of its own population in the converter: tn (k2_odd k2_buyn k2_selln k2_ne k2_iso), tv (k2_ve k2_rbq k2_rsq k2_v15f), tdv (k2_max), tne (k2_chg), tvq (k2_in), tv15f (k2_s15f)
#   qbsl, qasl (sizes) = both forms: against the minute's time-weighted mean size (k2_bsl = qbsl / qbs - 1, k2_asl = qasl / qas - 1) and the imbalance at the last quote (k2_limb = (qbsl - qasl) / (qbsl + qasl))
# A base that is zero or missing, a missing tlast, or no tick row for (stock, minute) leaves the feature MISSING; nothing is filled or carried forward. Missing values are counted per feature by the caller.
# The row's minute m counts from 04:00 New York; the tick table stores the minute of the day (m + M0), as wm2tk does. Data of minute m is complete at the decision mark of m (the end of minute m).
# No number lives here except the structural 1 of "ratio minus 1" and M0 (protocol constant of the two minute scales, the same as in wm2tk.py).
import sys, os, numpy as np

M0 = 240   # minute of the day of table minute 0 (04:00 New York); wm2tk.M0
# feature -> (numerator column, base column, kind): kind 'r' = numerator / base - 1 (price against tlast, size against its mean), 's' = numerator / base (share)
FORM = (('k2_max', 'tmax', 'tdv', 's'), ('k2_chg', 'tchg', 'tne', 's'), ('k2_open', 'topen', 'tlast', 'r'), ('k2_hi', 'thi', 'tlast', 'r'), ('k2_lo', 'tlo', 'tlast', 'r'), ('k2_odd', 'todd', 'tn', 's'),
        ('k2_buyn', 'tbuyn', 'tn', 's'), ('k2_selln', 'tselln', 'tn', 's'), ('k2_lasta', 'tlasta', 'tlast', 'r'), ('k2_ne', 'tne', 'tn', 's'), ('k2_ve', 'tve', 'tv', 's'), ('k2_iso', 'tiso', 'tn', 's'),
        ('k2_in', 'tin', 'tvq', 's'), ('k2_rbq', 'trbq', 'tv', 's'), ('k2_rsq', 'trsq', 'tv', 's'), ('k2_p15', 'tp15', 'tlast', 'r'), ('k2_v15f', 'tv15f', 'tv', 's'), ('k2_s15f', 'ts15f', 'tv15f', 's'),
        ('k2_bsl', 'qbsl', 'qbs', 'r'), ('k2_asl', 'qasl', 'qas', 'r'))
TK2COLS = [f[0] for f in FORM] + ['k2_limb']
RAW = sorted({c for f in FORM for c in f[1:3]})
UNREAD = 'tmax tchg topen thi tlo todd tbuyn tselln tlasta tne tve tiso tin trbq trsq tp15 tv15f ts15f qbsl qasl'.split()   # the 20 columns this group exists for


def values(k):
    # k = dict of float64 arrays of the RAW columns for a set of (stock, minute) cells (NaN = no value) -> dict feature -> float64 array
    out = {}
    with np.errstate(divide='ignore', invalid='ignore'):
        for name, num, base, kind in FORM:
            b = k[base]; ok = np.isfinite(b) & (b > 0) & np.isfinite(k[num]); v = np.where(ok, k[num] / np.where(ok, b, 1.0), np.nan); out[name] = v - 1 if kind == 'r' else v
        s = k['qbsl'] + k['qasl']; ok = np.isfinite(s) & (s > 0); out['k2_limb'] = np.where(ok, (k['qbsl'] - k['qasl']) / np.where(ok, s, 1.0), np.nan)
    return out


def add(t, tkf):
    # t = table of one day (columns sym as text, m); tkf = the day's tick file. Adds TK2COLS in place (float32). A stock-minute without a tick row gets missing values.
    import duckdb, pandas as pd
    k = duckdb.sql("SELECT sym, m - %d AS m, %s FROM read_parquet('%s') WHERE m >= %d" % (M0, ', '.join(RAW), tkf, M0)).df()
    if str(k['sym'].dtype) not in ('object', 'str', 'string') or k['sym'].isna().any(): raise SystemExit('REFUSED: tick_sym_not_text')
    if k.duplicated(['sym', 'm']).any(): raise SystemExit('REFUSED: tick_key_twice')
    j = pd.DataFrame({'sym': t['sym'].values, 'm': t['m'].values.astype(np.int64)}).merge(k.astype({'m': np.int64}), on=['sym', 'm'], how='left', sort=False)
    if len(j) != len(t): raise SystemExit('REFUSED: tk2_join_rows')
    v = values({c: j[c].values.astype(np.float64) for c in RAW})
    for c in TK2COLS: t[c] = v[c].astype(np.float32)


def test():
    n = [0]
    def ck(c, t):
        n[0] += 1
        if not c: sys.exit('tk2 selftest failed: ' + t)
    eq = lambda a, b: (a != a and b != b) or abs(a - b) < 1e-12
    # one synthetic minute: every raw number distinct, so that a swapped column cannot pass
    R = dict(tn=40.0, tv=5000.0, tdv=52000.0, tvq=4000.0, tlast=10.40, tne=32.0, tmax=11700.0, tchg=9.0, topen=10.00, thi=10.92, tlo=9.88, todd=10.0, tbuyn=18.0, tselln=14.0, tlasta=10.53, tve=4500.0, tiso=6.0, tin=1100.0,
             trbq=275.0, trsq=150.0, tp15=10.14, tv15f=800.0, ts15f=-200.0, qbsl=300.0, qasl=100.0, qbs=250.0, qas=125.0)
    ck(set(R) == set(RAW) and len(RAW) == 27 and len(TK2COLS) == 21 and len(set(TK2COLS)) == 21, 'the raw columns read and the 21 features')
    ck(sorted({f[1] for f in FORM}) == sorted(UNREAD) and len(UNREAD) == 20, 'every one of the 20 unread tick columns is used, and no other column is a numerator')
    WANT = dict(k2_max=11700.0 / 52000.0, k2_chg=9.0 / 32.0, k2_open=10.00 / 10.40 - 1, k2_hi=10.92 / 10.40 - 1, k2_lo=9.88 / 10.40 - 1, k2_odd=10.0 / 40.0, k2_buyn=18.0 / 40.0, k2_selln=14.0 / 40.0, k2_lasta=10.53 / 10.40 - 1,
                k2_ne=32.0 / 40.0, k2_ve=4500.0 / 5000.0, k2_iso=6.0 / 40.0, k2_in=1100.0 / 4000.0, k2_rbq=275.0 / 5000.0, k2_rsq=150.0 / 5000.0, k2_p15=10.14 / 10.40 - 1, k2_v15f=800.0 / 5000.0, k2_s15f=-200.0 / 800.0,
                k2_bsl=300.0 / 250.0 - 1, k2_asl=100.0 / 125.0 - 1, k2_limb=(300.0 - 100.0) / (300.0 + 100.0))
    v = values({c: np.array([R[c]]) for c in RAW})
    for c in TK2COLS: ck(eq(float(v[c][0]), WANT[c]), '%s on the synthetic minute: %r, expected %r' % (c, float(v[c][0]), WANT[c]))
    ck(len(set(round(x, 9) for x in WANT.values())) == 21, 'the 21 expected values are all different')
    # a zero base, a missing base, a missing numerator and a missing tlast leave the feature missing; the others stay
    for name, num, base, kind in FORM:
        for bad in (0.0, np.nan):
            r = dict(R); r[base] = bad; w = values({c: np.array([r[c]]) for c in RAW}); ck(np.isnan(w[name][0]), '%s is missing when its base %s is %r' % (name, base, bad))
            ck(all(eq(float(w[o[0]][0]), WANT[o[0]]) for o in FORM if o[2] != base and o[1] != base), 'the features that do not use %s are unchanged' % base)
        r = dict(R); r[num] = np.nan; ck(np.isnan(values({c: np.array([r[c]]) for c in RAW})[name][0]), '%s is missing when %s is missing' % (name, num))
    r = dict(R); r['qbsl'] = 0.0; r['qasl'] = 0.0; ck(np.isnan(values({c: np.array([r[c]]) for c in RAW})['k2_limb'][0]), 'k2_limb is missing when both last sizes are zero')
    r = dict(R); r['qasl'] = 0.0; ck(eq(float(values({c: np.array([r[c]]) for c in RAW})['k2_limb'][0]), 1.0), 'k2_limb with an empty ask side')
    # add(): the join by (stock, minute), the stock NA as text, no carry-forward, a tick minute of another minute scale
    import tempfile, shutil, duckdb, pandas as pd
    td = tempfile.mkdtemp()
    try:
        f = os.path.join(td, 'tk.parquet'); rows = [('NA', M0 + 400, R), ('AAA', M0 + 400, dict(R, tmax=26000.0)), ('AAA', M0 + 401, dict(R, tlast=np.nan)), ('BBB', M0 - 1, R)]
        duckdb.sql("COPY (SELECT * FROM (VALUES %s) v(sym, m, %s)) TO '%s' (FORMAT parquet)" % (', '.join("('%s', %d, %s)" % (s, m, ', '.join('NULL::DOUBLE' if r[c] != r[c] else '%r::DOUBLE' % r[c] for c in RAW)) for s, m, r in rows), ', '.join(RAW), f))
        t = pd.DataFrame({'sym': ['AAA', 'NA', 'AAA', 'AAA', 'CCC'], 'm': np.array([400, 400, 401, 402, 400], np.int16)}); add(t, f)
        ck(list(t.columns) == ['sym', 'm'] + TK2COLS and all(str(t[c].dtype) == 'float32' for c in TK2COLS) and len(t) == 5, 'add() writes the 21 columns as float32 and keeps the rows')
        ck(eq(float(t.k2_max[0]), np.float32(0.5)) and eq(float(t.k2_max[1]), float(np.float32(0.225))) and t.sym[1] == 'NA', 'rows get the values of their own stock and minute; the stock NA is text')
        ck(np.isnan(t.k2_open[2]) and eq(float(t.k2_odd[2]), np.float32(0.25)), 'a minute without tlast: the price features are missing, the shares stay')
        ck(t.loc[3, TK2COLS].isna().all() and t.loc[4, TK2COLS].isna().all(), 'a minute or a stock without a tick row is missing, nothing is carried forward')
        duckdb.sql("COPY (SELECT * FROM (VALUES %s) v(sym, m, %s)) TO '%s' (FORMAT parquet)" % (', '.join("('AAA', %d, %s)" % (M0 + 400, ', '.join('%r::DOUBLE' % R[c] for c in RAW)) for _ in range(2)), ', '.join(RAW), f))
        try: add(pd.DataFrame({'sym': ['AAA'], 'm': [400]}), f); ck(False, 'a tick key twice is refused')
        except SystemExit as e: ck(str(e) == 'REFUSED: tick_key_twice', 'a tick key twice is refused')
    finally: shutil.rmtree(td, ignore_errors=True)
    print('tk2 selftest passed: %d checks' % n[0])


if __name__ == '__main__':
    if sys.argv[1:] == ['test']: test()
    else: sys.exit('usage: python l1tk2.py test   (the module is used by l1feat.py)')

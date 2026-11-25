# Wide-model table: every stock that is in a per-minute top-N list (by change, by fall, by cumulative volume), from its first in-list minute of a session
# to the session flatten minute, EVERY minute that has a bar. In post-market the lists by change from the regular close are added (checked against Webull's recorded lists 2026-11-16/17). No sieve. One row = one snapshot at minute q; features use bars up to and including q only.
# Labels (future, never features), all relative to pb = price to buy now (next bar open, never below the close of q):
#   t05 t10 t20 t30 = what a 5/10/20/30% trailing exit on closes gets from here (flat at the session end)
#   h10 h30 h60     = price change 10/30/60 minutes after q (last close up to that minute, not later than the flatten)
#   d10 d30 d60 dfl = deepest low within 10/30/60 minutes and up to the flatten
#   ps = price to sell now (next bar open, never above the close of q); lo = lowest low until the next bar relative to the close of q; nq = minute of the next bar
# Used by wmsample.py (training sample) and wmscore.py (scores for every row). TOPN comes from the environment (WM_TOPN, default 500).
import os, glob, json, numpy as np, pandas as pd, numba as nb
from wide import load, NM, SESS

TOPN = int(os.environ.get('WM_TOPN', '500')); MINP = 0.10
FEAT = ('m sess c chg dv ret1 ret2 ret3 ret5 ret10 ret15 ret30 ret60 rise30 dd30 ddday hiage rng1 rng5 rng30 clv vsp dv5s dv30s act10 act30 up10b vwd ema9d ema20d ema45d ema100d ema9s rsi14 macdh atr14 bbp ue9 udv10 '
        'inl nin tofl gap pmhic').split()
TG = 't05 t10 t20 t30 h10 h30 h60 d10 d30 d60 dfl'.split()
LAB = ['pb', 'ps', 'nq'] + TG + ['lo']
TR = np.array([0.05, 0.10, 0.20, 0.30]); HZ = np.array([10, 30, 60], np.int64)


@nb.njit(cache=True)
def labels(C, L, O, ri, rq, rf, tr, hz):
    n = len(ri); out = np.full((n, 15), np.nan)
    for r in range(n):
        i = ri[r]; q = rq[r]; flat = rf[r]; nq = -1
        for j in range(q + 1, min(q + 6, NM)):
            if not np.isnan(C[i, j]):
                nq = j; break
        if nq < 0 or nq >= flat:
            continue
        c = C[i, q]; pb = max(O[i, nq], c); ps = min(O[i, nq], c); out[r, 0] = pb; out[r, 1] = ps; out[r, 2] = nq
        mx = -1.0; last = c; mn = np.inf; nt = len(tr); nh = len(hz); done = 0
        for k in range(nt): out[r, 3 + k] = np.nan
        for j in range(nq, flat + 1):
            x = C[i, j]
            if np.isnan(x):
                continue
            last = x
            if x > mx: mx = x
            if L[i, j] < mn: mn = L[i, j]
            for k in range(nt):
                if np.isnan(out[r, 3 + k]) and x <= mx * (1 - tr[k]): out[r, 3 + k] = x / pb - 1
            for k in range(nh):
                if j <= q + hz[k]: out[r, 3 + nt + k] = x / pb - 1; out[r, 3 + nt + nh + k] = mn / pb - 1
        for k in range(nt):
            if np.isnan(out[r, 3 + k]): out[r, 3 + k] = last / pb - 1
        for k in range(nh):
            if np.isnan(out[r, 3 + nt + k]): out[r, 3 + nt + k] = C[i, nq] / pb - 1; out[r, 3 + nt + nh + k] = L[i, nq] / pb - 1
        out[r, 3 + nt + 2 * nh] = mn / pb - 1
        lo = np.inf
        for j in range(q + 1, nq + 1):
            if not np.isnan(L[i, j]) and L[i, j] < lo: lo = L[i, j]
        out[r, 14] = lo / c - 1
    return out


def day(path, prev, types):
    # returns (table or None, last closes of the day for the next day's previous close)
    d = load(path)
    if types is not None: d = d[d.ticker.isin(types)]
    tick, idx = np.unique(d.ticker.values, return_inverse=True); n = len(tick); mm = d.m.values
    def mat(col, fill=np.nan):
        a = np.full((n, NM), fill); a[idx, mm] = d[col].values; return a
    C = mat('close'); V0 = np.nan_to_num(mat('volume')); pc = np.array([prev.get(t, np.nan) for t in tick]); P = pd.DataFrame(C).ffill(axis=1).values; CV = np.cumsum(V0, axis=1); has = ~np.isnan(C)
    lastc = {}
    for i in range(n):
        r = C[i, 330:720]; r = r[~np.isnan(r)]; cl = r[-1] if len(r) else P[i, -1]
        if cl == cl: lastc[tick[i]] = cl
    with np.errstate(divide='ignore', invalid='ignore'): chg = np.where(pc[:, None] > 0, P / pc[:, None] - 1, np.nan)
    inl = np.zeros((n, NM), np.bool_); in2 = np.zeros((n, NM), np.bool_); N2 = 200
    for m in range(NM):
        c = chg[:, m]; ids = np.where(~np.isnan(c))[0]
        if len(ids) == 0: continue
        o = np.argsort(c[ids]); inl[ids[o[-TOPN:]], m] = True; inl[ids[o[:TOPN]], m] = True; in2[ids[o[-N2:]], m] = True; in2[ids[o[:N2]], m] = True
        v = CV[:, m]; iv = np.argsort(v)[-TOPN:]; inl[iv[v[iv] > 0], m] = True; iv = iv[-N2:]; in2[iv[v[iv] > 0], m] = True
    # post-market: Webull ranks gainers and losers by the change from the regular close (rank_type AFTER_MARKET), so add those two lists there
    rc = pd.DataFrame(C[:, 330:720]).ffill(axis=1).iloc[:, -1].values
    with np.errstate(divide='ignore', invalid='ignore'): chg2 = np.where((rc[:, None] > 0) & has[:, 720:].cumsum(axis=1).astype(bool), P[:, 720:] / rc[:, None] - 1, np.nan)
    for m in range(720, NM):
        c = chg2[:, m - 720]; ids = np.where(~np.isnan(c))[0]
        if len(ids) == 0: continue
        o = np.argsort(c[ids]); inl[ids[o[-TOPN:]], m] = True; inl[ids[o[:TOPN]], m] = True; in2[ids[o[-N2:]], m] = True; in2[ids[o[:N2]], m] = True
    ok = inl & has & (np.nan_to_num(C) >= MINP); R = np.zeros((n, NM), np.bool_); FL = np.zeros(NM, np.int64); SS = np.zeros(NM, np.int64)
    for s in range(3):
        a, b, fl = SESS[s]; w = ok[:, a:b + 1]; anyw = w.any(axis=1); first = a + w.argmax(axis=1); cols = np.arange(a, fl)[None, :]
        R[:, a:fl] = anyw[:, None] & (cols >= first[:, None]) & has[:, a:fl]; FL[a:fl] = fl; SS[a:fl] = s
    keep = R.any(axis=1)
    if not keep.any(): return None, lastc
    ki = np.where(keep)[0]; tick = tick[ki]; n = len(ki); R = R[ki]; inl = inl[ki]; in2 = in2[ki]; pc = pc[ki]; idx2 = np.full(len(keep), -1); idx2[ki] = np.arange(n); sel = keep[idx]; di = idx2[idx[sel]]; dm = mm[sel]; dd_ = d[sel]
    def mat2(col):
        a = np.full((n, NM), np.nan); a[di, dm] = dd_[col].values; return a
    O, H, L, C, V = mat2('open'), mat2('high'), mat2('low'), mat2('close'), mat2('volume')
    Cd = pd.DataFrame(C); Cf = Cd.ffill(axis=1); P = Cf.values; V0 = np.nan_to_num(V); Vd = pd.DataFrame(V0); DVm = np.nan_to_num(C) * V0; DV = np.cumsum(DVm, axis=1); CV = np.cumsum(V0, axis=1)
    Hd = pd.DataFrame(H); Ld = pd.DataFrame(L); has = Cd.notna().astype(float)
    def back(k): return Cf.shift(k, axis=1)
    def rmax(x, k): return x.T.rolling(k, min_periods=1).max().T
    def rmin(x, k): return x.T.rolling(k, min_periods=1).min().T
    def rsum(x, k): return x.T.rolling(k, min_periods=1).sum().T
    ew = lambda x, k: x.T.ewm(span=k, adjust=False, ignore_na=True).mean().T; wl = lambda x: x.T.ewm(alpha=1 / 14, adjust=False, ignore_na=True).mean().T
    prevc = back(1); ch = Cd - prevc; up = ch.clip(lower=0); dn = (-ch).clip(lower=0)
    rsi = (100 - 100 / (1 + wl(up) / wl(dn))).ffill(axis=1); e9 = ew(Cd, 9).ffill(axis=1); e20 = ew(Cd, 20).ffill(axis=1); macd = ew(Cd, 12) - ew(Cd, 26); macdh = ((macd - ew(macd, 9)) / Cd).ffill(axis=1)
    trng = pd.DataFrame(np.fmax(np.fmax((Hd - Ld).values, (Hd - prevc).abs().values), (Ld - prevc).abs().values)); atr = (wl(trng) / Cd).ffill(axis=1)
    mid = Cf.T.rolling(20, min_periods=5).mean().T; sd = Cf.T.rolling(20, min_periods=5).std().T; bbp = (Cf - (mid - 2 * sd)) / (4 * sd)
    u9v = (Cd < e9).astype(float).where(Cd.notna()).ffill(axis=1).fillna(0).values; ue9 = np.zeros((n, NM))
    for j in range(1, NM): ue9[:, j] = np.where(u9v[:, j] > 0, ue9[:, j - 1] + 1, 0)
    uv = rsum(Vd.where(ch > 0, 0.0), 10); dvv = rsum(Vd.where(ch < 0, 0.0), 10); udv = (uv - dvv) / (uv + dvv).where((uv + dvv) > 0)
    hi30 = rmax(Hd, 30); lo30 = rmin(Ld, 30); hi5 = rmax(Hd, 5); lo5 = rmin(Ld, 5); dayhi = Hd.cummax(axis=1).ffill(axis=1)
    hiarg = np.zeros((n, NM)); dh = dayhi.values
    for j in range(1, NM): hiarg[:, j] = np.where(H[:, j] >= dh[:, j], j, hiarg[:, j - 1])
    pv = rsum(Vd, 10).shift(1, axis=1) / 10; rng = (Hd - Ld)
    with np.errstate(divide='ignore', invalid='ignore'):
        vw = DV / CV; pmhi = np.nanmax(np.where(np.isnan(H[:, :330]), -np.inf, H[:, :330]), axis=1); o330 = pd.DataFrame(O[:, 330:]).bfill(axis=1).iloc[:, 0].values; DVd = pd.DataFrame(DV).where(DV > 0)
        M = {'chg': P / pc[:, None] - 1, 'dv': DV, 'ret1': (Cf / back(1) - 1).values, 'ret2': (Cf / back(2) - 1).values, 'ret3': (Cf / back(3) - 1).values, 'ret5': (Cf / back(5) - 1).values, 'ret10': (Cf / back(10) - 1).values,
             'ret15': (Cf / back(15) - 1).values, 'ret30': (Cf / back(30) - 1).values, 'ret60': (Cf / back(60) - 1).values, 'rise30': (Cf / lo30.ffill(axis=1) - 1).values, 'dd30': (Cf / hi30.ffill(axis=1) - 1).values, 'ddday': (Cf / dayhi - 1).values,
             'hiage': np.arange(NM)[None, :] - hiarg, 'rng1': (rng / Cd).values, 'rng5': (hi5 / lo5 - 1).values, 'rng30': (hi30 / lo30 - 1).values, 'clv': ((Cd - Ld) / rng.where(rng > 0)).fillna(1.0).values, 'vsp': (Vd / pv.where(pv > 0)).values,
             'dv5s': (rsum(pd.DataFrame(DVm), 5) / DVd).values, 'dv30s': (rsum(pd.DataFrame(DVm), 30) / DVd).values, 'act10': rsum(has, 10).values, 'act30': rsum(has, 30).values,
             'up10b': (rsum((ch > 0).astype(float), 10) / rsum(has, 10)).values, 'vwd': P / vw - 1, 'ema9d': (Cf / e9 - 1).values, 'ema20d': (Cf / e20 - 1).values, 'ema45d': (Cf / ew(Cd, 45).ffill(axis=1) - 1).values,
             'ema100d': (Cf / ew(Cd, 100).ffill(axis=1) - 1).values, 'ema9s': (e9 / e9.shift(3, axis=1) - 1).values, 'rsi14': rsi.values, 'macdh': macdh.values, 'atr14': atr.values, 'bbp': bbp.values, 'ue9': ue9, 'udv10': udv.values,
             'inl': inl.astype(np.float64), 'nin': np.cumsum(inl, axis=1).astype(np.float64)}
    ri, rq = np.nonzero(R); rf = FL[rq]; lab = labels(C, L, O, ri.astype(np.int64), rq.astype(np.int64), rf, TR, HZ); g = ~np.isnan(lab[:, 0]); ri = ri[g]; rq = rq[g]; rf = rf[g]; lab = lab[g]; s = SS[rq]
    f = {k: v[ri, rq].astype(np.float32) for k, v in M.items()}; f['m'] = rq.astype(np.float32); f['sess'] = s.astype(np.float32); f['c'] = C[ri, rq].astype(np.float32); f['tofl'] = (rf - rq).astype(np.float32)
    with np.errstate(divide='ignore', invalid='ignore'):
        f['gap'] = np.where(s >= 1, o330[ri] / pc[ri] - 1, np.nan).astype(np.float32); f['pmhic'] = np.where((s >= 1) & np.isfinite(pmhi[ri]), C[ri, rq] / pmhi[ri] - 1, np.nan).astype(np.float32)
    t = pd.DataFrame({k: f[k] for k in FEAT})
    for k_, nm in enumerate(LAB): t[nm] = lab[:, k_] if nm in ('pb', 'ps') else (lab[:, k_].astype(np.int16) if nm == 'nq' else lab[:, k_].astype(np.float32))
    t['sym'] = tick[ri]; t['in2'] = in2[ri, rq].astype(np.int8); t['hc30'] = (C >= Cf.T.rolling(30, min_periods=1).max().T.values)[ri, rq].astype(np.int8)
    return t, lastc


def days(src, types_path, first, last):
    # yields (date, table) for every day file from `first` to `last`; earlier files (up to 45 days back) are read only for the previous close
    types = set(pd.read_csv(types_path, dtype=str).ticker) if types_path != '-' else None
    files = sorted(glob.glob(os.path.join(src, '*.csv.gz'))); SP = {}; sp = os.path.join(os.path.dirname(src.rstrip('/')), 'reference', 'splits.jsonl')
    if os.path.exists(sp):
        for l in open(sp):
            j = json.loads(l); SP.setdefault(j['execution_date'], []).append((j['ticker'], j['split_from'] / j['split_to']))
    prev = {}
    for f in files:
        dt = os.path.basename(f)[:10]
        if dt > last: break
        if dt < first and (pd.Timestamp(first) - pd.Timestamp(dt)).days > 45: continue
        for tk, fac in SP.get(dt, []):
            if tk in prev: prev[tk] *= fac
        if dt >= first:
            t, lastc = day(f, prev, types); yield dt, t
        else:
            d = load(f)
            if types is not None: d = d[d.ticker.isin(types)]
            d = d.sort_values(['ticker', 'm']); reg = d[(d.m >= 330) & (d.m < 720)].groupby('ticker').close.last(); allc = d.groupby('ticker').close.last(); lastc = {**allc.to_dict(), **reg.to_dict()}
        prev.update(lastc)

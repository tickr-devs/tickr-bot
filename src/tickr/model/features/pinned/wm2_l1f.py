# Wide model, version 2 of the table (training spec, stage 4). The same rows, the same 44 features and the same labels as wm.py, plus features that the minute bars give and the first version did not use:
#   market: SPY and QQQ change from the previous regular close and over the last 5 / 30 minutes (spy_chg spy_r5 spy_r30 qqq_*), and the stock minus SPY on the same windows (x_chg x_r5 x_r30);
#   place among ALL stocks of the universe in the same minute, 0..1: by change from the previous close (r_chg), by relative volume (r_rv), by the day's range so far (r_rng); brup = share of stocks above their previous close;
#   earlier days: rv20 = today's dollar volume so far against the average of the previous 20 days up to the same minute; pd_ret, pd_rng = previous day's change and range; rng20 = average day range of 20 days;
#   d20hi, d20lo = distance to the highest high / lowest low of 20 days; dvn20 = log10 of the average day dollar volume of 20 days; vu = volatility unit = standard deviation of the stock's 1-minute returns over the previous 20 days.
# Every new value uses bars up to and including minute q and whole EARLIER days only (history needs at least MINH earlier days, else empty). Nothing depends on a bet or a money limit.
# vu is also the unit of the targets: wm2train.py divides each label by vu * sqrt(minutes of its horizon); the labels themselves stay in percent here.
# Splits: day high / low / close of the stored history are multiplied by the split factor on the execution date (as the previous close). Dates after HARD_LAST are never read (sealed from 2026-11-20).
import os, glob, json, collections, numpy as np, pandas as pd, numba as nb, wm
from wm import labels, FEAT, TG, LAB, TR, HZ, TOPN, MINP
from wide import load, NM, SESS

ETF = ('SPY', 'QQQ'); NDAYS = 20; MINH = 5; MINR = 200; HARD_LAST = '2026-11-19'; SEAL = ('2026-07-01', '2026-08-14')   # sealed days: used only as history of later days, never returned as rows
NEW = 'spy_chg spy_r5 spy_r30 qqq_chg qqq_r5 qqq_r30 x_chg x_r5 x_r30 r_chg r_rv r_rng brup rv20 pd_ret pd_rng rng20 d20hi d20lo dvn20 vu'.split()
FEAT2 = FEAT + NEW
LAG = int(os.environ.get('WM_LAG', '0'))
GROUPS = {'market': 'spy_chg spy_r5 spy_r30 qqq_chg qqq_r5 qqq_r30 x_chg x_r5 x_r30'.split(), 'cross': 'r_chg r_rv r_rng brup'.split(), 'hist': 'rv20 pd_ret pd_rng rng20 d20hi d20lo dvn20 vu'.split()}   # groups of the new columns for the ablation


def cols(fs, nm):
    # column indices of a feature set in a sample file with columns nm: old | new | ref (everything but the tick columns) | tick (every column of the file), optionally minus groups: new-market, new-cross-hist ...
    p = fs.split('-'); base = {'old': FEAT, 'new': FEAT2, 'ref': [c for c in nm if not c.startswith('k_')], 'tick': list(nm)}[p[0]]; drop = {c for g in p[1:] for c in GROUPS[g]}
    return [list(nm).index(c) for c in base if c not in drop]
import wm2ref
REF = wm2ref.Ref(os.environ['WM_REF']) if os.environ.get('WM_REF') else None   # company reference columns (shares, short interest, news), see wm2ref.py
import wm2tk
TK = wm2tk.Tick(os.environ['WM_TICK']) if os.environ.get('WM_TICK') else None   # tick columns (trades / quotes per minute), see wm2tk.py
FEATX = FEAT2 + (wm2ref.REFCOLS if REF is not None else []) + (wm2tk.TKCOLS if TK is not None else [])   # the columns a sample file holds


# Second set of layer-1 questions (owner 2026-11-20), kept beside the 11 of wm.TG:
#   b10 b30s b30 b60 = result of a trade with three limits, in volatility units (unit = vu * sqrt(minutes)): bought at pb, closed at the first of: a low at or under -LOWER units (result = -LOWER),
#                      a close at or over +UPPER units (result = +UPPER), the time limit or the flatten minute (result = change then, in units, inside the two limits). The lower limit is checked first in a minute.
#   rk10 rk30        = place 0..1 of the row's h10 / h30 among all table rows of the same minute (1 = the best); empty when the minute has fewer than RKMIN rows.
#   u10 u30 u60      = highest high within 10 / 30 / 60 minutes after q (not later than the flatten), from pb: the mirror of d10 d30 d60, so any profit target can be valued later without a new layer 1.
#   x10 x30          = h10 / h30 minus the change of SPY from minute q to the same end minute (stock move in excess of the market).
# Derived at training time, not stored (wm2.derived): pu10 pu30s pu30 pu60 = 1 when the upper limit of the matching b-question was hit first, else 0; pl... = 1 when the lower limit was hit first. Trained as probabilities (log loss).
TG2 = 'b10 b30s b30 b60 rk10 rk30 u10 u30 u60 x10 x30'.split()
PCLS = {'%s%s' % (k, b[1:]): (b, j, k) for j, b in enumerate('b10 b30s b30 b60'.split()) for k in ('pu', 'pl')}   # derived class target -> (its b column, row of BAR, kind)


def derived(tg, yb):
    # class label of a derived target from the stored b column (NaN stays NaN)
    b, j, k = PCLS[tg]; return np.where(np.isfinite(yb), (yb == (BAR[j, 1] if k == 'pu' else -BAR[j, 2])).astype(np.float32), np.nan)
BAR = np.array([[10, 1, 1], [30, 1, 1], [30, 2, 1], [60, 2, 1]], np.float64)   # per barrier question: time limit in minutes, UPPER, LOWER (volatility units)
RK = (('rk10', 'h10'), ('rk30', 'h30')); RKMIN = 50
YARD = {'b10': 'h10', 'b30s': 'h30', 'b30': 'h30', 'b60': 'h60', 'rk10': 'h10', 'rk30': 'h30', 'u10': 'h10', 'u30': 'h30', 'u60': 'h60', 'x10': 'h10', 'x30': 'h30', 'pu10': 'h10', 'pu30s': 'h30', 'pu30': 'h30', 'pu60': 'h60', 'pl10': 'h10', 'pl30s': 'h30', 'pl30': 'h30', 'pl60': 'h60'}   # the percent label a second-set model is judged against
SPANS = np.array([10, 30, 60, 0], np.int64)   # label spans in minutes for the uniqueness weights; 0 = to the flatten minute
SPANOF = {'h10': 0, 'd10': 0, 'b10': 0, 'rk10': 0, 'u10': 0, 'x10': 0, 'pu10': 0, 'pl10': 0, 'h30': 1, 'd30': 1, 'b30s': 1, 'b30': 1, 'rk30': 1, 'u30': 1, 'x30': 1, 'pu30s': 1, 'pu30': 1, 'pl30s': 1, 'pl30': 1, 'h60': 2, 'd60': 2, 'b60': 2, 'u60': 2, 'pu60': 2, 'pl60': 2}   # target -> column of SPANS; every other target runs to the flatten (column 3)


@nb.njit(cache=True)
def barriers(C, L, ri, rq, rf, nq, pb, vu, bar):
    n = len(ri); out = np.full((n, len(bar)), np.nan)
    for r in range(n):
        u = vu[r]
        if not (u > 0): continue
        i = ri[r]
        for k in range(len(bar)):
            unit = u * np.sqrt(bar[k, 0]); up = bar[k, 1]; dn = bar[k, 2]; end = min(rq[r] + int(bar[k, 0]), rf[r]); res = np.nan; last = C[i, nq[r]] / pb[r] - 1
            for j in range(nq[r], end + 1):
                x = C[i, j]
                if np.isnan(x): continue
                if L[i, j] / pb[r] - 1 <= -dn * unit:
                    res = -dn; break
                last = x / pb[r] - 1
                if last >= up * unit:
                    res = up; break
            if np.isnan(res): res = min(up, max(-dn, last / unit))
            out[r, k] = res
    return out


@nb.njit(cache=True)
def maxhigh(H, C, ri, rq, rf, nq, pb, hz):
    n = len(ri); out = np.full((n, len(hz)), np.nan)
    for r in range(n):
        i = ri[r]
        for k in range(len(hz)):
            mx = C[i, nq[r]]
            for j in range(nq[r], min(rq[r] + hz[k], rf[r]) + 1):
                if not np.isnan(H[i, j]) and H[i, j] > mx: mx = H[i, j]
            out[r, k] = mx / pb[r] - 1
    return out


@nb.njit(cache=True)
def uniq(code, q, tofl, spans):
    # average uniqueness of every row's label (Lopez de Prado): rows of one stock (same code, rows sorted by code) whose label windows (q, end] overlap share the weight; a row's value = mean over its window of 1 / rows alive in that minute.
    n = len(code); out = np.ones((n, len(spans))); a = 0
    while a < n:
        b = a
        while b < n and code[b] == code[a]: b += 1
        for k in range(len(spans)):
            c = np.zeros(NM + 2)
            for r in range(a, b):
                e = q[r] + tofl[r] if spans[k] == 0 else min(q[r] + spans[k], q[r] + tofl[r])
                c[q[r] + 1] += 1; c[e + 1] -= 1
            for j in range(1, NM + 2): c[j] += c[j - 1]
            for r in range(a, b):
                e = q[r] + tofl[r] if spans[k] == 0 else min(q[r] + spans[k], q[r] + tofl[r]); s_ = 0.0
                for j in range(q[r] + 1, e + 1): s_ += 1.0 / c[j]
                out[r, k] = s_ / max(e - q[r], 1)
        a = b
    return out


def day2(path, prev, types, hist, mprev, want=True):
    # returns (table or None, last closes of the day, summary of the day for the next days' history, regular closes of the market ETFs)
    d = load(path); MK = {}; mclose = {}
    for e in ETF:
        x = d[d.ticker == e]; a = np.full(NM, np.nan); a[x.m.values] = x.close.values; r = a[330:720]; r = r[~np.isnan(r)]; s_ = pd.Series(a).ffill()
        if len(r): mclose[e] = r[-1]
        p0 = mprev.get(e, np.nan); MK[e] = (s_.values / p0 - 1, (s_ / s_.shift(5) - 1).values, (s_ / s_.shift(30) - 1).values)
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
    # ---- whole-universe values of today (for the history of later days and for the per-minute places among all stocks)
    Hf = mat('high'); Lf = mat('low'); DVa = np.cumsum(np.nan_to_num(C) * V0, axis=1).astype(np.float32)
    with np.errstate(divide='ignore', invalid='ignore'):
        lr = np.log(C[:, 1:] / C[:, :-1]); ssq = np.nansum(lr * lr, axis=1); nr = (~np.isnan(lr)).sum(axis=1)
        dhi = np.nanmax(np.where(np.isnan(Hf), -np.inf, Hf), axis=1); dlo = np.nanmin(np.where(np.isnan(Lf), np.inf, Lf), axis=1)
    cl = np.array([lastc.get(t, np.nan) for t in tick]); summ = {'tick': tick, 'cdv': DVa, 'ssq': ssq, 'nr': nr, 'hi': dhi, 'lo': dlo, 'cl': cl, 'pc': pc}
    if not want: return None, lastc, summ, mclose
    # ---- history of the previous days (never today): aligned to today's tickers
    nh = np.zeros(n); A = np.zeros((n, NM), np.float32); SQ = np.zeros(n); NR = np.zeros(n); RG = np.zeros(n); H20 = np.full(n, -np.inf); L20 = np.full(n, np.inf); DVD = np.zeros(n); pdr = np.full(n, np.nan); pdg = np.full(n, np.nan)
    for k_, h in enumerate(hist):   # oldest first; the last one is the previous trading day
        j = np.searchsorted(h['tick'], tick); j[j >= len(h['tick'])] = 0; ok_ = h['tick'][j] == tick; jj = j[ok_]
        nh[ok_] += 1; A[ok_] += h['cdv'][jj]; SQ[ok_] += h['ssq'][jj]; NR[ok_] += h['nr'][jj]; rg = h['hi'][jj] / h['lo'][jj] - 1; RG[ok_] += np.where(np.isfinite(rg), rg, 0.0); DVD[ok_] += h['cdv'][jj, -1]
        H20[ok_] = np.fmax(H20[ok_], h['hi'][jj]); L20[ok_] = np.fmin(L20[ok_], h['lo'][jj])
        if k_ == len(hist) - 1: pdr[ok_] = h['cl'][jj] / h['pc'][jj] - 1; pdg[ok_] = rg
    with np.errstate(divide='ignore', invalid='ignore'):
        A = np.where(nh[:, None] >= MINH, A / nh[:, None], np.nan); vu = np.where((nh >= MINH) & (NR >= MINR), np.sqrt(SQ / NR), np.nan); rng20 = np.where(nh >= MINH, RG / nh, np.nan); dvn20 = np.where(nh >= MINH, np.log10(DVD / nh + 1), np.nan)
        H20 = np.where(nh >= MINH, H20, np.nan); L20 = np.where(nh >= MINH, L20, np.nan)
        RV = np.where(A > 0, DVa / A, np.nan); RNG = pd.DataFrame(Hf).cummax(axis=1).ffill(axis=1).values / pd.DataFrame(Lf).cummin(axis=1).ffill(axis=1).values - 1
    r_chg = pd.DataFrame(chg).rank(axis=0, pct=True).values.astype(np.float32); r_rv = pd.DataFrame(np.where(DVa > 0, RV, np.nan)).rank(axis=0, pct=True).values.astype(np.float32); r_rng = pd.DataFrame(RNG).rank(axis=0, pct=True).values.astype(np.float32)
    with np.errstate(divide='ignore', invalid='ignore'): brup = np.nansum(chg > 0, axis=0) / np.maximum(1, (~np.isnan(chg)).sum(axis=0))
    del Hf, Lf, RNG
    inl = np.zeros((n, NM), np.bool_); in2 = np.zeros((n, NM), np.bool_); N2 = 200; _K = np.zeros((10, n, NM), np.bool_)
    for m in range(NM):
        v = CV[:, m]; iv = np.where(v > 0)[0]; iv = iv[np.argsort(-v[iv], kind='stable')]; inl[iv[:TOPN], m] = True; in2[iv[:N2], m] = True; _K[4, iv[:TOPN], m] = True; _K[5, iv[:N2], m] = True
        c = chg[:, m]; ids = np.where(~np.isnan(c))[0]
        if len(ids) == 0: continue
        o = np.argsort(c[ids], kind='stable'); og_ = np.argsort(-c[ids], kind='stable'); inl[ids[og_[:TOPN]], m] = True; inl[ids[o[:TOPN]], m] = True; in2[ids[og_[:N2]], m] = True; in2[ids[o[:N2]], m] = True; _K[0, ids[og_[:TOPN]], m] = True; _K[1, ids[og_[:N2]], m] = True; _K[2, ids[o[:TOPN]], m] = True; _K[3, ids[o[:N2]], m] = True
    # post-market: Webull ranks gainers and losers by the change from the regular close (rank_type AFTER_MARKET), so add those two lists there
    rc = pd.DataFrame(C[:, 330:720]).ffill(axis=1).iloc[:, -1].values
    with np.errstate(divide='ignore', invalid='ignore'): chg2 = np.where((rc[:, None] > 0) & has[:, 720:].cumsum(axis=1).astype(bool), P[:, 720:] / rc[:, None] - 1, np.nan)
    for m in range(720, NM):
        c = chg2[:, m - 720]; ids = np.where(~np.isnan(c))[0]
        if len(ids) == 0: continue
        o = np.argsort(c[ids], kind='stable'); og_ = np.argsort(-c[ids], kind='stable'); inl[ids[og_[:TOPN]], m] = True; inl[ids[o[:TOPN]], m] = True; in2[ids[og_[:N2]], m] = True; in2[ids[o[:N2]], m] = True; _K[6, ids[og_[:TOPN]], m] = True; _K[7, ids[og_[:N2]], m] = True; _K[8, ids[o[:TOPN]], m] = True; _K[9, ids[o[:N2]], m] = True
    _CAP.update(tick=tick, inl=inl, in2=in2, comp=_K, chg=chg, cv=CV, chg2=chg2, has=has, c=C); ok = inl & has & (np.nan_to_num(C) >= MINP); R = np.zeros((n, NM), np.bool_); FL = np.zeros(NM, np.int64); SS = np.zeros(NM, np.int64)
    for s in range(3):
        a, b, fl = SESS[s]; w = ok[:, a:b + 1]; anyw = w.any(axis=1); first = a + w.argmax(axis=1); cols = np.arange(a, fl)[None, :]
        R[:, a:fl] = anyw[:, None] & (cols >= first[:, None]) & has[:, a:fl]; FL[a:fl] = fl; SS[a:fl] = s
    keep = R.any(axis=1)
    if not keep.any(): return None, lastc, summ, mclose
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
    ri, rq = np.nonzero(R); rf = FL[rq]; lab = labels(C, L, O, ri.astype(np.int64), rq.astype(np.int64), rf, TR, HZ); g0_ = ~np.isnan(lab[:, 0]); g = rq <= rf - 2; lab[~g0_, 2] = rq[~g0_]; ri = ri[g]; rq = rq[g]; rf = rf[g]; lab = lab[g]; s = SS[rq]
    f = {k: v[ri, rq].astype(np.float32) for k, v in M.items()}; f['m'] = rq.astype(np.float32); f['sess'] = s.astype(np.float32); f['c'] = C[ri, rq].astype(np.float32); f['tofl'] = (rf - rq).astype(np.float32)
    with np.errstate(divide='ignore', invalid='ignore'):
        f['gap'] = np.where(s >= 1, o330[ri] / pc[ri] - 1, np.nan).astype(np.float32); f['pmhic'] = np.where((s >= 1) & np.isfinite(pmhi[ri]), C[ri, rq] / pmhi[ri] - 1, np.nan).astype(np.float32)
    t = pd.DataFrame({k: f[k] for k in FEAT})
    for k_, nm in enumerate(LAB): t[nm] = lab[:, k_] if nm in ('pb', 'ps') else (lab[:, k_].astype(np.int16) if nm == 'nq' else lab[:, k_].astype(np.float32))
    gi_ = ki[ri]   # row -> index in the whole universe of today
    ql = np.maximum(rq - LAG, 0)   # WM_LAG: the minute the 21 new columns are read at (look-ahead check: with LAG=1 they may use nothing of minute q)
    with np.errstate(divide='ignore', invalid='ignore'):
        N = {'r_chg': r_chg[gi_, ql], 'r_rv': r_rv[gi_, ql], 'r_rng': r_rng[gi_, ql], 'brup': brup[ql], 'rv20': RV[gi_, ql], 'pd_ret': pdr[gi_], 'pd_rng': pdg[gi_], 'rng20': rng20[gi_], 'd20hi': (P[ri, ql] if LAG else C[ri, rq]) / H20[gi_] - 1, 'd20lo': (P[ri, ql] if LAG else C[ri, rq]) / L20[gi_] - 1, 'dvn20': dvn20[gi_], 'vu': vu[gi_]}
        for e in ETF:
            for k_, w in enumerate(('chg', 'r5', 'r30')): N['%s_%s' % (e.lower(), w)] = MK[e][k_][ql]
        lg = (lambda k: M[k][ri, ql].astype(np.float32)) if LAG else (lambda k: f[k])
        N['x_chg'] = lg('chg') - N['spy_chg']; N['x_r5'] = lg('ret5') - N['spy_r5']; N['x_r30'] = lg('ret30') - N['spy_r30']
    for k_ in NEW: t[k_] = np.asarray(N[k_], dtype=np.float32)
    lb2 = barriers(C, L, ri.astype(np.int64), rq.astype(np.int64), rf.astype(np.int64), lab[:, 2].astype(np.int64), lab[:, 0], np.asarray(N['vu'], np.float64), BAR)
    for k_, nm in enumerate(TG2[:len(BAR)]): t[nm] = lb2[:, k_].astype(np.float32)
    mh = maxhigh(H, C, ri.astype(np.int64), rq.astype(np.int64), rf.astype(np.int64), lab[:, 2].astype(np.int64), lab[:, 0], HZ); spy = 1 + MK['SPY'][0]
    for k_, hz_ in enumerate(HZ): t['u%d' % hz_] = mh[:, k_].astype(np.float32)
    with np.errstate(divide='ignore', invalid='ignore'):
        for hz_ in (10, 30): t['x%d' % hz_] = (t['h%d' % hz_].values - (spy[np.minimum(rq + hz_, rf)] / spy[rq] - 1)).astype(np.float32)
    for nm, src_ in RK: t[nm] = t.groupby('m')[src_].rank(pct=True).where(t.groupby('m')[src_].transform('count') >= RKMIN).astype(np.float32)
    t['sym'] = tick[ri]; t['in2'] = in2[ri, rq].astype(np.int8); t['hc30'] = (C >= Cf.T.rolling(30, min_periods=1).max().T.values)[ri, rq].astype(np.int8)
    if REF is not None: REF.add(t, os.path.basename(path)[:10], pc[ri])
    if TK is not None: TK.add(t, os.path.basename(path)[:10])
    return t, lastc, summ, mclose


def days2(src, types_path, first, last, warm=45):
    # yields (date, table) for every day file from `first` to `last`; earlier files (up to `warm` calendar days back) are read only for the previous close and the 20-day history
    last = min(last, HARD_LAST); types = _L1_READ_TYPES(types_path) if types_path != '-' else None
    files = sorted(glob.glob(os.path.join(src, '*.csv.gz'))); SP = {}; sp = os.path.join(os.path.dirname(src.rstrip('/')), 'reference', 'splits.jsonl')
    if os.path.exists(sp):
        for l in open(sp):
            j = json.loads(l); SP.setdefault(j['execution_date'], []).append((j['ticker'], j['split_from'] / j['split_to']))
    prev = {}; mprev = {}; hist = collections.deque(maxlen=NDAYS)
    for f in files:
        dt = os.path.basename(f)[:10]
        if dt > last: break
        if dt < first and (pd.Timestamp(first) - pd.Timestamp(dt)).days > warm: continue
        for tk, fac in SP.get(dt, []):
            if tk in prev: prev[tk] *= fac
            if tk in mprev: mprev[tk] *= fac
            for h in hist:
                j = np.searchsorted(h['tick'], tk)
                if j < len(h['tick']) and h['tick'][j] == tk:
                    for k in ('hi', 'lo', 'cl', 'pc'): h[k][j] *= fac
        want = dt >= first and not (SEAL[0] <= dt <= SEAL[1]); t, lastc, summ, mclose = day2(f, prev, types, hist, mprev, want=want)
        if want: yield dt, t
        prev.update(lastc); mprev.update(mclose); hist.append(summ)


HZMIN = {'h10': 10, 'h30': 30, 'h60': 60, 'd10': 10, 'd30': 30, 'd60': 60}   # horizon of a target in minutes; the other targets run to the flatten minute (feature tofl)
VCLIP = 5.0   # targets in volatility units are clipped to +-VCLIP


TSPAN = float(os.environ['WM_TSPAN']) if os.environ.get('WM_TSPAN') else None   # WM_TSPAN: fixed span in minutes for the trailing-exit targets (t05..t30) in volunits, capped by the minutes left; not set = minutes to the flatten


def volunits(y, tg, vu, tofl):
    # label in percent -> label in units of the stock's own usual move over the horizon (vu * sqrt(minutes)), clipped; NaN where vu is unknown
    with np.errstate(divide='ignore', invalid='ignore'):
        span = HZMIN[tg] if tg in HZMIN else np.minimum(np.maximum(tofl, 1), TSPAN) if TSPAN and tg[0] == 't' else np.maximum(tofl, 1)
        return np.clip(y / (vu * np.sqrt(span)), -VCLIP, VCLIP)


_T0 = None


def progress(job, done, total, state='running', cause=None):
    # training-progress-v1 for the System card: written to the path in WM_PROGRESS (nothing when unset), atomically (temp file in the same folder, then rename)
    global _T0
    p = os.environ.get('WM_PROGRESS'); now = pd.Timestamp.now(tz='America/New_York').isoformat(timespec='seconds'); _T0 = _T0 or now
    if not p: return
    j = {'schema_version': 1, 'job': job, 'state': state, 'done': int(min(done, total)), 'total': int(total), 'started_at': _T0, 'updated_at': now, 'cause': cause if state == 'failed' else None}
    with open(p + '.tmp', 'w') as f: json.dump(j, f)
    os.replace(p + '.tmp', p)

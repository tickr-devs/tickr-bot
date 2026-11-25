# Wide candidate table: every "starts rising" minute in pre-market, regular session and post-market, price from $0.10, rise from 5%.
# usage: python wide.py <minute_aggs dir> <out.csv.gz> <types.csv> [first] [last]
# Minute index m counts from 04:00 New York (09:30 = 330). Output column t = m - 330 (minutes after 09:30, negative in pre-market),
# so extfeat.py can be run on the output unchanged. All features use only bars up to and including the signal bar.
# A signal: close >= X above the lowest low of the last W minutes and the highest close of those W minutes (a new day high is NOT required), price >= MINP,
# the stock is in the per-minute top-200 by change, by fall or by volume, and the next bar comes within 2 minutes.
# Entry: next bar open, but never below the signal close. Exits: trailing 5/10/20/30% from the high after entry, or the session flatten time.
import sys, glob, os, json, collections, numpy as np, pandas as pd, numba as nb

TOPN = 200; MINP = 0.10; X = 0.05; W = 30; NM = 960
# session: (first signal minute, last signal minute, flatten minute); pre 04:00-09:20 flat 09:28, regular 09:35-15:30 flat 15:50, post 16:01-19:40 flat 19:55
SESS = np.array([[0, 320, 328], [335, 690, 710], [721, 940, 955]], np.int64)
TR = np.array([0.05, 0.10, 0.20, 0.30])


@nb.njit(cache=True)
def outcome(O, H, L, C, i, q0, e, tr, flat):
    # trade from bar q0 (entry price e) with trailing stop tr, forced exit at the first bar at or after `flat`; returns exit price, exit minute
    hi = max(H[i, q0], e); x = np.nan; xq = q0; last = q0
    for q in range(q0, NM):
        if np.isnan(C[i, q]):
            continue
        if q >= flat:
            if np.isnan(x):
                x = C[i, last] if q > flat + 5 else O[i, q]; xq = q
            break
        stop = hi * (1 - tr)
        if q > q0 and O[i, q] <= stop:
            x = O[i, q]; xq = q; break
        if q > q0 and L[i, q] <= stop:
            x = stop; xq = q; break
        if H[i, q] > hi:
            hi = H[i, q]
        last = q
    if np.isnan(x):
        x = C[i, last]; xq = last
    return x, xq


@nb.njit(cache=True)
def scan(O, H, L, C, inl, sess, trs, X, W, minp):
    out = []
    n = C.shape[0]
    for i in range(n):
        hiC = -1.0; nb_ = 0; block = -1
        for m in range(NM):
            c = C[i, m]
            if np.isnan(c):
                continue
            nb_ += 1
            newhi = c > hiC and nb_ > 5
            if c > hiC:
                hiC = c
            if nb_ <= 5 or m <= block or c < minp or not inl[i, m]:
                continue
            s = -1
            for k in range(3):
                if m >= sess[k, 0] and m <= sess[k, 1]:
                    s = k
            if s < 0:
                continue
            q0 = -1
            if m + 1 < NM and not np.isnan(C[i, m + 1]):
                q0 = m + 1
            elif m + 2 < NM and not np.isnan(C[i, m + 2]):
                q0 = m + 2
            if q0 < 0:
                continue
            lo = L[i, m]; hw = -1.0
            for j in range(max(0, m - W), m):
                if not np.isnan(L[i, j]) and L[i, j] < lo:
                    lo = L[i, j]
                if not np.isnan(C[i, j]) and C[i, j] > hw:
                    hw = C[i, j]
            if c / lo - 1 < X or c <= hw:
                continue
            flat = sess[s, 2]
            e = max(O[i, q0], c)
            mx = e; mn = e; f20 = 0.0; done = False; mx30 = e; mn30 = e; mxc = e
            for q in range(q0, flat):
                if np.isnan(C[i, q]):
                    continue
                if not done:
                    if L[i, q] <= e * 0.9:
                        done = True
                    elif H[i, q] >= e * 1.2:
                        f20 = 1.0; done = True
                if H[i, q] > mx:
                    mx = H[i, q]
                if C[i, q] > mxc:
                    mxc = C[i, q]
                if L[i, q] < mn:
                    mn = L[i, q]
                if q < q0 + 30:
                    if H[i, q] > mx30:
                        mx30 = H[i, q]
                    if L[i, q] < mn30:
                        mn30 = L[i, q]
            row = [float(i), float(m), float(s), float(q0), c, O[i, q0], e, c / lo - 1, mx / e - 1, mn / e - 1, f20, mx30 / e - 1, mn30 / e - 1, mxc / e - 1]
            for k in range(len(trs)):
                x, xq = outcome(O, H, L, C, i, q0, e, trs[k], flat)
                row.append(x / e - 1); row.append(float(xq - q0))
                if trs[k] == 0.20:
                    block = xq
            out.append(row)
    return out


OUT = 'i m sess q0 c o1 e2 rise dmax dmin up20 mfe30 mae30 dmaxc g5 h5 g10 h10 g20 h20 g30 h30'.split()
FEAT = ('chg dv dv5 dv30 ntr5 ntr30 tsz5 ret1 ret3 ret5 ret15 ret60 range30 range5 vwapd clv vspike hiage brk ddpre nbars act10 act30 up10 maxbar10 sigidx newhi '
        'rkchg rkdv nup10 nup30 medchg gap pm_dv pmhic rth_chg rth_rng rth_dv rth_cl').split()
DAYF = 'adv nhist ret1d ret5d ret20d hi20 lo20 maxret20 nrun20 rngy dow'.split()


def load(path):
    d = pd.read_csv(path, dtype={'ticker': str}, na_filter=False)
    ts = pd.to_datetime(d.window_start, unit='ns', utc=True).dt.tz_convert('America/New_York')
    d['m'] = (ts.dt.hour * 60 + ts.dt.minute - 240).astype(np.int32)
    return d[(d.m >= 0) & (d.m < NM)]


def day(path, prev, types):
    d = load(path)
    if types is not None:
        d = d[d.ticker.isin(types)]
    tick, idx = np.unique(d.ticker.values, return_inverse=True); n = len(tick); mm = d.m.values
    def mat(col):
        a = np.full((n, NM), np.nan); a[idx, mm] = d[col].values; return a
    O, H, L, C, V, N = mat('open'), mat('high'), mat('low'), mat('close'), mat('volume'), mat('transactions')
    pc = np.array([prev.get(t, np.nan) for t in tick])
    P = pd.DataFrame(C).ffill(axis=1).values
    V0 = np.nan_to_num(V); DVm = np.nan_to_num(C) * V0; CV = np.cumsum(V0, axis=1); DV = np.cumsum(DVm, axis=1); N0 = np.nan_to_num(N)
    with np.errstate(divide='ignore', invalid='ignore'):
        chg = np.where(pc[:, None] > 0, P / pc[:, None] - 1, np.nan)
    inl = np.zeros((n, NM), np.bool_)
    for m in range(NM):
        c = chg[:, m]; ids = np.where(~np.isnan(c))[0]
        if len(ids) == 0:
            continue
        o = np.argsort(c[ids]); inl[ids[o[-TOPN:]], m] = True; inl[ids[o[:TOPN]], m] = True
        v = CV[:, m]; iv = np.argsort(v)[-TOPN:]; inl[iv[v[iv] > 0], m] = True
    ev = scan(O, H, L, C, inl, SESS, TR, X, W, MINP)
    # daily summary for the history of later days: last regular-session close (or last close of the day), high, low, dollar volume
    reg = C[:, 330:720]; lastc = {}; daily = {}
    hh = np.nanmax(np.where(np.isnan(H), -np.inf, H), axis=1); ll = np.nanmin(np.where(np.isnan(L), np.inf, L), axis=1)
    for i in range(n):
        r = reg[i]; r = r[~np.isnan(r)]
        cl = r[-1] if len(r) else P[i, -1]
        if cl == cl:
            lastc[tick[i]] = cl; daily[tick[i]] = (cl, hh[i], ll[i], DV[i, -1])
    if not ev:
        return None, lastc, daily, n
    e = pd.DataFrame(np.array(ev), columns=OUT); e['sym'] = tick[e.i.astype(int).values]
    rows = []
    for i, m in zip(e.i.astype(int).values, e.m.astype(int).values):
        c = C[i, m]; b = np.where(~np.isnan(C[i, :m + 1]))[0]
        def back(k):
            j = b[b <= m - k]; return C[i, j[-1]] if len(j) else np.nan
        def win(a, k): return a[i, max(0, m - k + 1):m + 1]
        h30 = np.nanmax(win(H, 30)); l30 = np.nanmin(win(L, 30)); h5 = np.nanmax(win(H, 5)); l5 = np.nanmin(win(L, 5))
        dvc = DV[i, m]; vw = dvc / CV[i, m] if CV[i, m] > 0 else np.nan
        rng = H[i, m] - L[i, m]; pv = V0[i, max(0, m - 10):m]; pvm = pv.mean() if len(pv) else 0.0
        ph = H[i, :m]; okp = ~np.isnan(ph)
        if okp.any():
            hp = int(np.nanargmax(ph)); phm = ph[hp]; hiage = m - hp; brk = c / phm - 1; ddpre = np.nanmin(L[i, hp:m + 1]) / phm - 1
        else:
            hiage = brk = ddpre = np.nan
        c10 = win(C, 11); c10 = c10[~np.isnan(c10)]; d10 = np.diff(c10) / c10[:-1] if len(c10) > 1 else np.array([np.nan])
        cm = chg[:, m]; vm = DV[:, m]
        o330 = O[i, 330:m + 1]; o330 = o330[~np.isnan(o330)] if m >= 330 else np.array([])
        pmdv = DV[i, min(m, 329)]; pmhi = np.nanmax(H[i, :330]) if m >= 330 and (~np.isnan(H[i, :330])).any() else np.nan
        if m >= 720:
            rc = C[i, 330:720]; rc = rc[~np.isnan(rc)]; rth_cl = rc[-1] if len(rc) else np.nan
            rth_chg = rth_cl / pc[i] - 1 if pc[i] > 0 else np.nan; rth_dv = DV[i, 719] - DV[i, 329]
            rth_rng = (np.nanmax(H[i, 330:720]) / np.nanmin(L[i, 330:720]) - 1) if len(rc) else np.nan
        else:
            rth_cl = rth_chg = rth_dv = rth_rng = np.nan
        rows.append([cm[i], dvc, DVm[i, max(0, m - 4):m + 1].sum(), DVm[i, max(0, m - 29):m + 1].sum(), N0[i, max(0, m - 4):m + 1].sum(), N0[i, max(0, m - 29):m + 1].sum(),
                     V0[i, max(0, m - 4):m + 1].sum() / max(N0[i, max(0, m - 4):m + 1].sum(), 1.0), c / back(1) - 1, c / back(3) - 1, c / back(5) - 1, c / back(15) - 1, c / back(60) - 1,
                     h30 / l30 - 1, h5 / l5 - 1, c / vw - 1 if vw == vw else np.nan, (c - L[i, m]) / rng if rng > 0 else 1.0, V0[i, m] / pvm if pvm > 0 else np.nan,
                     hiage, brk, ddpre, float(len(b)), float((~np.isnan(win(C, 10))).sum()), float((~np.isnan(win(C, 30))).sum()), float((d10 > 0).mean()) if len(d10) else np.nan,
                     float(np.nanmax(d10)) if len(d10) else np.nan, 0.0, 1.0 if brk != brk or c > np.nanmax(C[i, :m]) else 0.0,
                     float(np.nansum(cm > cm[i])), float((vm > vm[i]).sum()), float(np.nansum(cm >= 0.10)), float(np.nansum(cm >= 0.30)), float(np.nanmedian(cm)),
                     o330[0] / pc[i] - 1 if len(o330) and pc[i] > 0 else np.nan, pmdv, c / pmhi - 1 if pmhi == pmhi else np.nan, rth_chg, rth_rng, rth_dv, c / rth_cl - 1 if rth_cl == rth_cl else np.nan])
    f = pd.DataFrame(rows, columns=FEAT, index=e.index); e = pd.concat([e, f], axis=1)
    e['sigidx'] = e.groupby('sym').cumcount(); e['pc'] = pc[e.i.astype(int).values]; e['t'] = e.m - 330; e['inlist'] = 1
    return e.drop(columns=['i']), lastc, daily, n


if __name__ == '__main__':
    import warnings; warnings.filterwarnings('ignore')
    src, out = sys.argv[1], sys.argv[2]
    types = set(pd.read_csv(sys.argv[3], dtype=str).ticker) if len(sys.argv) > 3 and sys.argv[3] != '-' else None
    first = sys.argv[4] if len(sys.argv) > 4 else '0'; last = sys.argv[5] if len(sys.argv) > 5 else '9'
    files = sorted(glob.glob(os.path.join(src, '*.csv.gz')))
    SP = {}; sp = os.path.join(os.path.dirname(src.rstrip('/')), 'reference', 'splits.jsonl')
    if os.path.exists(sp):
        for l in open(sp):
            j = json.loads(l); SP.setdefault(j['execution_date'], []).append((j['ticker'], j['split_from'] / j['split_to']))
    prev = {}; HD = collections.defaultdict(lambda: collections.deque(maxlen=21)); allr = []
    for f in files:
        dt = os.path.basename(f)[:10]
        if dt > last: break
        if dt < first and (pd.Timestamp(first) - pd.Timestamp(dt)).days > 45: continue
        for tk, fac in SP.get(dt, []):
            if tk in prev: prev[tk] *= fac
            if tk in HD: HD[tk] = collections.deque([(a * fac, b * fac, c_ * fac, d_) for a, b, c_, d_ in HD[tk]], maxlen=21)
        r, lastc, daily, n = day(f, prev, types)
        if r is not None and dt >= first:
            rows = []
            for sym, c in zip(r.sym, r.c):
                h = HD.get(sym)
                if not h or len(h) < 2:
                    rows.append([np.mean([x[3] for x in h]) if h else np.nan, float(len(h)) if h else 0.0] + [np.nan] * 8); continue
                a = np.array(h); cl = a[:, 0]; dr = cl[1:] / cl[:-1] - 1
                rows.append([a[-20:, 3].mean(), float(len(a)), dr[-1], cl[-1] / cl[-6] - 1 if len(cl) > 5 else np.nan, cl[-1] / cl[0] - 1 if len(cl) > 20 else np.nan,
                             c / a[:, 1].max() - 1, c / a[:, 2].min() - 1, dr.max(), float((a[1:, 1] / cl[:-1] - 1 >= 0.3).sum()), (a[-1, 1] - a[-1, 2]) / cl[-1]])
            for j_, nm in enumerate(DAYF[:-1]): r[nm] = [x[j_] for x in rows]
            r['dow'] = pd.Timestamp(dt).dayofweek; r['date'] = dt; allr.append(r)
            print(dt, n, 'tickers', len(r), 'signals', 'pre/reg/post', int((r.sess == 0).sum()), int((r.sess == 1).sum()), int((r.sess == 2).sum()),
                  'rise>=10%', int((r.rise >= 0.10).sum()), 'peak>=30%', int((r.dmax >= 0.3).sum()), flush=True)
        for k_, v_ in daily.items(): HD[k_].append(v_)
        prev.update(lastc)
    pd.concat(allr).to_csv(out, index=False)

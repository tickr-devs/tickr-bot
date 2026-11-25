# Tick columns for the layer-1 table: per row (stock, minute q) features made from the per-minute trades / quotes table of wm2tick.py (59 numbers per stock and New York minute, file <dir>/YYYY/YYYY-MM-DD.parquet).
# Everything uses minutes up to and including q only. W = windows in minutes; a window sum counts a minute without trades (or quotes) as zero; a ratio whose denominator is zero is empty.
#   per window w in W:  k_fl{w} signed shares (bought above the middle - sold below) / shares;  k_iso{w} intermarket-sweep shares / shares;  k_isofl{w} signed sweep shares / shares;  k_ret{w} retail (buy - sell) / shares;  k_retsh{w} retail shares / shares;
#       k_off{w} off-exchange share;  k_big{w} share of dollars in trades of 20,000+ dollars;  k_odd{w} odd-lot share of shares;  k_esp{w} effective half spread, share weighted;  k_out{w} (shares above the ask - below the bid) / quoted shares;
#       k_rv{w} sqrt of the sum of squared trade-to-trade log changes;  k_mrv{w} the same from the middle price;  k_spr{w} mean quoted spread;  k_imb{w} mean size imbalance at the best prices;  k_ofi{w} order flow imbalance / mean size at the best prices;
#       k_tn{w} log(1 + trades);  k_qn{w} log(1 + quote updates)
#   at minute q:  k_big5 k_big100 (5 minutes: share of dollars in trades of 5,000+ / 100,000+ dollars);  k_sprl spread at the last quote;  k_dep log10 dollars at the best bid + ask;  k_depimb (bid dollars - ask dollars) / sum;  k_gapq / k_gapt longest silence of quotes / trades in the minute, seconds;
#       k_burst largest one-second shares / shares of the minute;  k_twt mean second of the minute, share weighted;  k_l15 signed share of the last 15 seconds;  k_l15r price change of the last 15 seconds;  k_bad5 share of time without a valid quote, 5 minutes;  k_halt30 flagged quote updates, 30 minutes
#   kr_... = place 0..1 of the RANKED columns among the table rows of the same minute.
# A day without its file is refused (SystemExit), never filled. Minute index: table minute 0 = 4:00 New York = tick minute 240.
import os, numpy as np, pandas as pd
W = (1, 5, 30); M0 = 240; NM = 960
WCOLS = 'fl iso isofl ret retsh off big odd esp out rv mrv spr imb ofi tn qn'.split()
RANKED = 'k_fl5 k_fl30 k_isofl5 k_ret5 k_spr5 k_esp5 k_ofi5 k_tn5 k_rv5'.split()   # also given as the place 0..1 among the table rows of the same minute (kr_...)
TKCOLS = ['k_%s%d' % (c, w) for w in W for c in WCOLS] + 'k_big5 k_big100 k_sprl k_dep k_depimb k_gapq k_gapt k_burst k_twt k_l15 k_l15r k_bad5 k_halt30'.split() + ['kr' + c[1:] for c in RANKED]
RAW = 'tn tv tdv tbig toff tbuy tsell tvar twt tlast tbig5 tbig100 tvodd tviso tisob tisos tesp tabove tbelow tvq trb trs tp45 tv15l ts15l tburst tgap qn qspr qbs qas qimb qofi qgap qbad qbid qask qhalt qbd qad qmvar'.split()


class Tick:
    def __init__(self, d): self.d = d

    def add(self, t, date):
        import duckdb
        fn = os.path.join(self.d, date[:4], date + '.parquet')
        if not os.path.exists(fn): raise SystemExit('REFUSED: no tick file for ' + date + ' (' + fn + ')')
        k = duckdb.sql("SELECT sym, m - %d AS m, %s FROM read_parquet('%s') WHERE m >= %d AND m < %d" % (M0, ', '.join(RAW), fn, M0, M0 + NM)).df()
        syms = np.unique(t.sym.values.astype(str)); k = k[k.sym.isin(syms)]; si = np.searchsorted(syms, k.sym.values.astype(str)); mi = k.m.values.astype(np.int64); n = len(syms)
        def mat(c, fill=0.0):
            a = np.full((n, NM), fill); a[si, mi] = np.nan_to_num(k[c].values.astype(np.float64), nan=fill) if fill == fill else k[c].values.astype(np.float64); return a
        def ws(a, w):
            c = np.cumsum(a, axis=1); o = c.copy(); o[:, w:] -= c[:, :-w]; return o
        ri = np.searchsorted(syms, t.sym.values.astype(str)); rq = t.m.values.astype(np.int64); out = {}
        tv, tdv, tvq = mat('tv'), mat('tdv'), mat('tvq'); hasq = (mat('qn') > 0).astype(np.float64); A = {c: mat(c) for c in 'tbuy tsell tviso tisob tisos trb trs tvodd tabove tbelow tvar qmvar qofi tn qn qhalt'.split()}
        off, big, b5, b100, esp, spr, imb, dep = mat('toff') * tv, mat('tbig') * tdv, mat('tbig5') * tdv, mat('tbig100') * tdv, mat('tesp') * tvq, mat('qspr'), mat('qimb'), mat('qbs') + mat('qas')
        with np.errstate(divide='ignore', invalid='ignore'):
            for w in W:
                v = ws(tv, w); v = np.where(v > 0, v, np.nan); dv = ws(tdv, w); dv = np.where(dv > 0, dv, np.nan); vq = ws(tvq, w); vq = np.where(vq > 0, vq, np.nan); nq = ws(hasq, w); nq = np.where(nq > 0, nq, np.nan)
                X = {'fl': ws(A['tbuy'] - A['tsell'], w) / v, 'iso': ws(A['tviso'], w) / v, 'isofl': ws(A['tisob'] - A['tisos'], w) / v, 'ret': ws(A['trb'] - A['trs'], w) / v, 'retsh': ws(A['trb'] + A['trs'], w) / v, 'off': ws(off, w) / v, 'big': ws(big, w) / dv,
                     'odd': ws(A['tvodd'], w) / v, 'esp': ws(esp, w) / vq, 'out': ws(A['tabove'] - A['tbelow'], w) / vq, 'rv': np.sqrt(ws(A['tvar'], w)), 'mrv': np.sqrt(ws(A['qmvar'], w)), 'spr': ws(spr, w) / nq, 'imb': ws(imb, w) / nq,
                     'ofi': ws(A['qofi'], w) / (ws(dep, w) / nq), 'tn': np.log1p(ws(A['tn'], w)), 'qn': np.log1p(ws(A['qn'], w))}
                for c in WCOLS: out['k_%s%d' % (c, w)] = X[c][ri, rq]
                if w == 5: out['k_big5'] = (ws(b5, w) / dv)[ri, rq]; out['k_big100'] = (ws(b100, w) / dv)[ri, rq]; out['k_bad5'] = (ws(mat('qbad') * hasq, w) / nq)[ri, rq]
                if w == 30: out['k_halt30'] = ws(A['qhalt'], w)[ri, rq]
            ff = lambda c: pd.DataFrame(mat(c, np.nan)).ffill(axis=1).values   # the last minute that had a value, up to q
            bid, ask, qbd, qad = ff('qbid'), ff('qask'), ff('qbd'), ff('qad'); ok = (bid > 0) & (ask > bid)
            out['k_sprl'] = np.where(ok, (ask - bid) / ((ask + bid) / 2), np.nan)[ri, rq]; out['k_dep'] = np.where(qbd + qad > 0, np.log10(qbd + qad), np.nan)[ri, rq]; out['k_depimb'] = ((qbd - qad) / (qbd + qad))[ri, rq]
            out['k_gapq'] = mat('qgap', np.nan)[ri, rq]; out['k_gapt'] = mat('tgap', np.nan)[ri, rq]; tvn = np.where(tv > 0, tv, np.nan); out['k_burst'] = (mat('tburst') / tvn)[ri, rq]; out['k_twt'] = mat('twt', np.nan)[ri, rq]
            v15 = mat('tv15l'); out['k_l15'] = (mat('ts15l') / np.where(v15 > 0, v15, np.nan))[ri, rq]; out['k_l15r'] = (mat('tlast', np.nan) / mat('tp45', np.nan) - 1)[ri, rq]
        for c in RANKED: out['kr' + c[1:]] = pd.Series(out[c]).groupby(rq).rank(pct=True).values
        for c in TKCOLS: t[c] = np.asarray(out[c], dtype=np.float32)

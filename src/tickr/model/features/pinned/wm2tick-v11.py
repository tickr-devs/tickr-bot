# Tick layer, step 1 (runs on the cloud download machine): one day of Massive trades and quotes -> one small table per stock and minute; the raw day files are deleted right after.
# Research only; nothing here is read by the bot. Sealed days are refused (same rule as wm2.py). Credentials: NAMES only; values are read from the machine's secret store or the environment and never printed.
# usage: python wm2tick.py <first date> <last date> <work dir> <out>      out = s3://bucket/prefix or a local dir; one file <out>/YYYY/YYYY-MM-DD.parquet per day, a day that has a file is skipped (resumable)
#        python wm2tick.py local <trades.csv.gz> <quotes.csv.gz> <out.parquet>      (test mode: no download)
#        python wm2tick.py corr <date> <work dir>      (diagnostic: downloads the trades file of one open day, prints the number of trades and shares by correction code, deletes the file; nothing is written)
# env: TICK_SECRET (secret name holding MASSIVE_S3_ACCESS_KEY_ID / MASSIVE_S3_SECRET_ACCESS_KEY; default: the two names are read from the environment), TICK_THREADS, TICK_MEM (e.g. 12GB), TICK_BIG (dollars of one trade counted as large, default 20000), TICK_RAW (s3://bucket/prefix: the two raw day files are also copied there in the DEEP_ARCHIVE class before they are deleted locally), TICK_ORDER=desc (newest day first), TICK_PX (s3://bucket/prefix: also write the label-side price file of each day there, see 'px file' below), TICK_TYPES (csv with a ticker column, local path or s3://...: only these stocks plus SPY and QQQ are kept; not set = all tickers)
# Columns of the result (per sym, m = minute of the New York day 0..1439):
#   trades: tn trades, tv shares, tdv dollars, tmax largest trade in dollars, tbig share of dollars in large trades, toff share of shares reported off exchange, tbuy / tsell shares bought at or above the ask-side half / sold at or below the bid-side half
#           (trade price against the middle of the best bid and ask standing just before it; equal = neither), tvar sum of squared log changes trade to trade, tchg price changes, twt mean second of the minute weighted by shares, tlast last price,
#           (added 2026-11-20, column list frozen after this) topen first price, thi / tlo highest / lowest price, tbig5 / tbig100 share of dollars in trades of at least 5,000 / 100,000 dollars, todd / tvodd trades / shares in trades under 100 shares, tbuyn / tselln number of trades above / below the middle
#           (second and last addition 2026-11-20, list frozen) sale conditions are applied: trades that do not update volume are dropped; tlast topen tvar tchg tp15 tp45 use only trades that may set the last price, thi tlo only trades that may set high / low
#           (Form T counts outside the regular session only); tlasta last price of ALL trades, tne / tve trades / shares that may set the last price; tiso tviso tisob tisos intermarket sweep trades, their shares, shares above / below the middle;
#           tesp effective half spread |price - middle| / middle weighted by shares, tabove / tbelow / tin shares above the ask / below the bid / strictly inside, tvq shares that had a valid quote before them;
#           trb / trs retail buy / sell shares (Boehmer et al. 2021: off-exchange, price of at least 1 dollar with a fraction of a cent above 0.6 / under 0.4); tp15 last price of the first 15 seconds, tp45 last price before second 45
#           (return of the last 15 seconds = tlast / tp45 - 1), tv15f ts15f tv15l ts15l shares and signed shares in the first / last 15 seconds, tburst most shares in one second, tgap longest silence before a trade of this minute in seconds
#           (third and last change 2026-11-20, spec thread; 61 columns) trbq / trsq retail buy / sell shares signed by the quote (Barber et al. 2024): off-exchange trades of at least 1 dollar with a fraction of a cent,
#           buy if the price lies above 60% of the way from the best bid to the best ask standing just before the trade, sell if below 40%, the middle band is not signed. trb / trs stay as they were.
#           Live parity: a trade that was cancelled or corrected later (correction code 1, 7, 8) is KEPT as it was printed, in its minute: a live reader of the stream did not know about the later correction;
#           the later records about it (codes 10, 11, 12) are dropped. Counted on 2026-10-14: 1474 such pairs among 136.6M trades (0.1% of the shares).
#   quotes: qn updates, qspr mean (ask - bid) / middle weighted by time, qbs / qas mean best bid / ask size weighted by time, qimb mean (bid size - ask size) / (sum) weighted by time, qofi order flow imbalance (Cont et al.: change of size at the best prices, signed) in shares-or-lots as the file gives them,
#           qgap longest silence between two updates in seconds (a halt shows as a long silence in a stock that was quoting), qbad share of time with ask <= bid or an empty side, qbid / qask last best prices, qhalt updates whose indicators or conditions column is not empty (kept raw for the halt check),
#           (added 2026-11-20) qbsl / qasl last best bid / ask size, qbd / qad mean dollars at the best bid / ask weighted by time (size x price, size as the file gives it), qmvar sum of squared log changes of the middle price update to update (free of the bid-ask bounce)
# SIDE FILES (2026-11-20, owner + spec thread; LABEL / REPLAY SIDE, NOT MODEL INPUTS; the 61 columns above are untouched). Written when TICK_PX is set; s10 goes to TICK_S10 or to the sibling prefix s10. Same keys sym, m;
# one row per stock-minute with a trade, a quote update or an auction / official print. A quote state counts only when it is valid (bid > 0, ask > bid, both sizes > 0). COLUMN LIST FINAL (39 + 30): a new column needs the owner's yes.
# px:  pb<S> pa<S> pz<S> pbz<S> = best bid, best ask, ask size, bid size standing S seconds after the minute start, S = 0 2 5 15 (the state carried into the minute counts);
#      pblo pbhi palo pahi = lowest / highest bid and ask standing during the minute (state at second 0 included); pbl pal = bid / ask standing just before the last trade of the minute;
#      pblos pbhis = second of the minute when the lowest / highest bid first stood (0 = already at the start); xlos xhis = second of the first trade at the lowest / highest price (trades that may set high / low, as tlo / thi);
#      vleb0 vlem0 vgea0 = shares traded in the minute at or under the bid of second 0 / at or under the middle of second 0 / at or over the ask of second 0 (would a resting limit order there have seen trades);
#      qind qcond = the distinct quote indicator codes / quote condition codes seen in the minute, as text '1,23' (LULD limit states, halts and pauses are in these codes; Massive glossary 'conditions-indicators');
#      opx opv = price (largest print) and shares of the opening prints (trade condition 25), clx clv = same for the closing prints (8), ofo ofc = market center official open (16) / close (15) price of the largest such print
#      (the listing exchange's print; other exchanges send their own smaller ones), kept in the minute where the print arrived (the official close may come some minutes after 16:00);
#      tdlm tdlx = median / largest delay of the minute's trades between the exchange time and the SIP time, milliseconds.
# s10: six 10-second buckets k = 0..5 of the minute: l<k> last price (trades that may set the last price), v<k> shares, s<k> signed shares (side as in tbuy / tsell), b<k> a<k> = best bid / ask standing at the end of the bucket.
# (v11 CANDIDATE, 2026-11-20, NOT installed; px side only, the 61 tk columns and s10 are untouched) extra px columns, switched by TICK_V11 (default 'abc'; '' = exactly the v10 files):
#   a: pg<S> = age in milliseconds of the source quote of mark S (S = 0 2 5 15), pst<S> = its state: 0 no quote yet today, 1 two-sided valid, 2 bid only, 3 ask only, 4 locked (ask = bid), 5 crossed (ask < bid), 6 no side at all;
#      +10 when that quote carries any indicator or condition code (the codes of the minute stay in qind / qcond; which code means a halt is NOT decided here). Latest state wins, no search back, as before.
#   b: pblo5 = lowest valid bid standing from second 5 to the minute end (the state at second 5 included).
#   d: raw marks (spec thread + Codex 2026-11-20: v10 marks hide locked, crossed and one-sided quotes): qb<S> qa<S> qbz<S> qaz<S> = bid, ask, bid size, ask size of the quote update standing at mark S exactly as printed, in ANY state
#      (0 or empty where the update has none, exactly as the winning row gives it; also NULL when the stock has no quote update yet today: tell the two apart with pst<S>, 0 = no quote yet). Read them with pst<S>. The old pb / pa / pz / pbz columns stay as they were (two-sided ask > bid only).
#      pqi<S> pqc<S> = the indicator / condition codes of THAT standing update as printed (text, '' = none), pqt<S> = its SIP timestamp in nanoseconds (exact; pg<S> is the same as an age), pqs<S> = its sequence number (v11c). Two updates with one timestamp: the larger sequence number is the standing one.
#      pblo5s = second of the minute when the lowest valid bid from second 5 first stood (5 = already standing at second 5).
#   manifest: one JSON per day beside the px files (prefix 'manifest'): converter version, TICK_V11, md5 of this script, key / ETag / size / last-modified of the two source files, rows and stocks, bytes and md5 of the tk, px and s10 files.
#   c: one frozen passive buy order: plim = middle of mark 0 rounded DOWN to a tick (0.01 from $1, 0.0001 under $1), arrival 5 s after the minute start, expiry at the minute end. pvat / pvbl = shares printed at the limit / strictly under it,
#      ptat / ptbl = millisecond of the minute of the first such print, ptak = first millisecond with a non-empty ask at or under the limit (5000 = already at arrival). Prints counted: stamped strictly AFTER arrival, on an exchange (no TRF),
#      volume-updating, and without the conditions average price 2, cash 7, extended hours sold out of sequence 13, next day 20, price variation 21, contingent 52, qualified contingent 53, bunched sold 5, derivatively priced 10,
#      prior reference price 22, seller 29, sold out of sequence 33, closing / opening prints 8 / 25. Odd lots count. Prints at the limit are an upper bound (queue position unknown); prints under the limit mean a sure fill.
import sys, os, json, time, datetime, hashlib, threading, duckdb
VERSION = 'v11'; BUILD = '11c4'; V11 = os.environ.get('TICK_V11', 'abcd'); C_NOPASS = ['2', '7', '13', '20', '21', '52', '53', '5', '10', '22', '29', '33', '8', '25']
SEAL = ('2026-07-01', '2026-08-14'); HARD_LAST = '2026-11-19'   # same sealed periods as wm2.py: never read
TZ = 'America/New_York'; NS = 1000000000; MIN = 60 * NS; BIG = float(os.environ.get('TICK_BIG', '20000')); BIG5 = 5000.0; BIG100 = 100000.0; KB = int(os.environ.get('TICK_GROUPS', '24')); TRIES = int(os.environ.get('TICK_TRIES', '6')); PAUSE = int(os.environ.get('TICK_PAUSE', '30'))   # KB = number of stock groups worked one after another (memory)
# Sale conditions (Massive condition ids; update rules from massive.com/glossary/us/stocks/conditions-indicators, read 2026-11-20). A trade with several conditions: one "no" wins.
C_DROP = ['15', '16', '38']   # market center official close / open, corrected consolidated close: do not update volume -> not a trade of the minute at all
C_NOHL = ['2', '7', '13', '20', '21', '37', '52', '53']   # average price, cash, extended hours sold out of sequence, next day, price variation, odd lot, contingent, qualified contingent: volume yes, prices no
C_NOLAST = ['5', '10', '22', '29', '33']   # bunched sold, derivatively priced, prior reference price, seller, sold out of sequence: may set high / low, never the last price
CORR_LATER = [10, 11, 12]   # correction codes of records written LATER about an earlier trade (cancel record, error record, correction record): dropped. The original print (code 0, or 1 / 7 / 8 = later corrected / marked as error / cancelled) is kept.
C_FORMT = '12'; C_ISO = '14'; C_CLOSEP = '8'; C_OPENP = '25'; C_OFFC = '15'; C_OFFO = '16'; RTH = (570, 959)   # Form T sets prices only outside the regular session (Massive builds its pre / post minute bars the same way); RTH = minutes 9:30..15:59 of the New York day
def fmd5(fn):
    h = hashlib.md5()
    with open(fn, 'rb') as f:
        for b in iter(lambda: f.read(1 << 22), b''): h.update(b)
    return h.hexdigest()
def lst(x): return '[' + ', '.join("'%s'" % v for v in x) + ']'
LOGINERR = ('TokenRetrievalError', 'UnauthorizedSSOToken', 'SSOTokenLoadError', 'ExpiredToken', 'Token has expired'); LOGINWAIT = 60   # error names of an expired AWS login; seconds between checks while waiting for a new login
REDO_NOW = os.environ.get('TICK_REDO_FROM', '2026-05-01'); PXSEC = (0, 2, 5, 15); PXNOTE = 'LABEL / REPLAY SIDE; NOT MODEL INPUTS (the 10-second file s10 may become inputs only by a later decision of the plan): executable prices for rebuilding labels and replaying fills'   # px file: seconds after the minute start at which the standing bid / ask are stored
TCOLS = ['ticker', 'conditions', 'correction', 'exchange', 'price', 'size', 'sip_timestamp', 'sequence_number', 'trf_id']
QCOLS = ['ticker', 'ask_price', 'ask_size', 'bid_price', 'bid_size', 'sip_timestamp', 'sequence_number', 'conditions', 'indicators']

def con():
    c = duckdb.connect(); c.execute("SET threads=%d" % int(os.environ.get('TICK_THREADS', os.cpu_count() or 1))); c.execute("SET memory_limit='%s'" % os.environ.get('TICK_MEM', '10GB')); c.execute("SET preserve_insertion_order=false"); c.execute("SET TimeZone='UTC'")
    return c

def header(c, fn, need):
    have = [r[0] for r in c.execute("DESCRIBE SELECT * FROM read_csv('%s', header=true, all_varchar=true, delim=',', quote='\"', escape='\"', sample_size=100)" % fn).fetchall()]
    miss = [x for x in need if x not in have]
    if miss: raise SystemExit('REFUSED: file %s has no columns %s (it has %s)' % (os.path.basename(fn), miss, have))

FILLX = "(coalesce(qd.bp, 0) > 0 AND coalesce(qd.bs, 0) > 0 AND NOT (coalesce(qd.ap, 0) > 0 AND coalesce(qd.az, 0) > 0 AND qd.ap < qd.bp) AND NOT list_has_any(string_split(coalesce(qd.qi, ''), ','), ['2', '4', '8']) AND NOT list_contains(string_split(coalesce(qd.qc, ''), ','), '20'))"
STE = 'CAST(CASE WHEN r.ts IS NULL THEN 0 WHEN (coalesce(r.bp, 0) > 0 AND coalesce(r.ap, 0) > coalesce(r.bp, 0) AND coalesce(r.bs, 0) > 0 AND coalesce(r.az, 0) > 0) THEN 1 WHEN (coalesce(r.bp, 0) > 0 AND coalesce(r.bs, 0) > 0) AND NOT (coalesce(r.ap, 0) > 0 AND coalesce(r.az, 0) > 0) THEN 2 WHEN (coalesce(r.ap, 0) > 0 AND coalesce(r.az, 0) > 0) AND NOT (coalesce(r.bp, 0) > 0 AND coalesce(r.bs, 0) > 0) THEN 3 WHEN NOT (coalesce(r.bp, 0) > 0 AND coalesce(r.bs, 0) > 0) THEN 6 WHEN coalesce(r.ap, 0) = coalesce(r.bp, 0) THEN 4 ELSE 5 END + CASE WHEN coalesce(r.fl, false) THEN 10 ELSE 0 END AS TINYINT)'

def side(c, first, mexpr, tp, w):
    # side files of one group of stocks (tables q = quotes, ta = trades joined with the quote before them). LABEL / REPLAY SIDE, NOT MODEL INPUTS. One row per stock-minute with a trade, a quote update or an auction / official print.
    OK = "(bp > 0 AND ap > bp AND bs > 0 AND az > 0)"; marks = sorted(set(PXSEC) | set(range(10, 70, 10)))
    c.execute("CREATE OR REPLACE TEMP TABLE qd AS SELECT sym, ts, r.bp AS bp, r.ap AS ap, r.bs AS bs, r.az AS az, r.flag AS fl, r.qi AS qi, r.qc AS qc, r.sq AS sq FROM (SELECT sym, ts, arg_max(struct_pack(bp := bp, ap := ap, bs := bs, az := az, flag := flag, qi := qi, qc := qc, sq := sq), sq) AS r FROM q GROUP BY sym, ts)")   # (v11c) ONE winning row (highest sequence number) per stock and timestamp, every field from that row; arg_max per field skipped empty fields and could mix two updates
    c.execute("CREATE OR REPLACE TEMP TABLE au AS SELECT sym, ts // %d AS mk, arg_max(px, sz) FILTER (WHERE c25) AS opx, sum(sz) FILTER (WHERE c25) AS opv, arg_max(px, sz) FILTER (WHERE c8) AS clx, sum(sz) FILTER (WHERE c8) AS clv, arg_max(px, sz) FILTER (WHERE c16) AS ofo, arg_max(px, sz) FILTER (WHERE c15) AS ofc FROM read_parquet('%s')%s AND (c8 OR c25 OR c15 OR c16) GROUP BY 1, 2" % (MIN, tp, w))
    c.execute("CREATE OR REPLACE TEMP TABLE kk AS SELECT sym, mk, mk * %d AS t0 FROM (SELECT DISTINCT sym, ts // %d AS mk FROM q UNION SELECT DISTINCT sym, ts // %d AS mk FROM ta UNION SELECT sym, mk FROM au)" % (MIN, MIN, MIN))
    # quote state standing S seconds after the minute start (mark 60 = the last state of the minute)
    c.execute("""CREATE OR REPLACE TEMP TABLE spt AS WITH ks AS (SELECT kk.sym, kk.mk, s.s, kk.t0 + CAST(s.s AS BIGINT) * %d - CASE WHEN s.s = 60 THEN 1 ELSE 0 END AS tq FROM kk, (SELECT unnest(%s) AS s) s),
        sn AS (SELECT ks.sym, ks.mk, ks.s, (qd.bp > 0 AND qd.ap > qd.bp AND qd.bs > 0 AND qd.az > 0) AS ok, qd.bp, qd.ap, qd.bs, qd.az, qd.qi, qd.qc, qd.ts AS qts, qd.sq AS qsq, CASE WHEN (coalesce(qd.bp, 0) > 0 AND coalesce(qd.bs, 0) > 0 AND NOT (coalesce(qd.ap, 0) > 0 AND coalesce(qd.az, 0) > 0 AND qd.ap < qd.bp) AND NOT list_has_any(string_split(coalesce(qd.qi, ''), ','), ['2', '4', '8']) AND NOT list_contains(string_split(coalesce(qd.qc, ''), ','), '20')) THEN qd.bp END AS fbid, CAST(round((ks.tq - qd.ts) / 1000000.0) AS BIGINT) AS age,
            CAST(CASE WHEN qd.ts IS NULL THEN 0 WHEN (coalesce(qd.bp, 0) > 0 AND coalesce(qd.ap, 0) > coalesce(qd.bp, 0) AND coalesce(qd.bs, 0) > 0 AND coalesce(qd.az, 0) > 0) THEN 1 WHEN (coalesce(qd.bp, 0) > 0 AND coalesce(qd.bs, 0) > 0) AND NOT (coalesce(qd.ap, 0) > 0 AND coalesce(qd.az, 0) > 0) THEN 2 WHEN (coalesce(qd.ap, 0) > 0 AND coalesce(qd.az, 0) > 0) AND NOT (coalesce(qd.bp, 0) > 0 AND coalesce(qd.bs, 0) > 0) THEN 3 WHEN NOT (coalesce(qd.bp, 0) > 0 AND coalesce(qd.bs, 0) > 0) THEN 6 WHEN coalesce(qd.ap, 0) = coalesce(qd.bp, 0) THEN 4 ELSE 5 END + CASE WHEN coalesce(qd.fl, false) THEN 10 ELSE 0 END AS TINYINT) AS st FROM ks ASOF LEFT JOIN qd ON ks.sym = qd.sym AND ks.tq >= qd.ts)
        SELECT sym, mk, %s, %s, max(CASE WHEN s = 5 AND ap > 0 AND az > 0 THEN ap END) AS ra5, max(CASE WHEN s = 0 THEN fbid END) AS fb0, max(CASE WHEN s = 5 THEN fbid END) AS fb5, %s FROM sn GROUP BY sym, mk""" % (NS, marks, ', '.join("max(CASE WHEN s = %d THEN age END) AS pg%d, max(CASE WHEN s = %d THEN st END) AS pst%d" % ((v, v) * 2) for v in PXSEC), ', '.join("max(CASE WHEN s = %d THEN bp END) AS qb%d, max(CASE WHEN s = %d THEN ap END) AS qa%d, max(CASE WHEN s = %d THEN bs END) AS qbz%d, max(CASE WHEN s = %d THEN az END) AS qaz%d, max(CASE WHEN s = %d THEN qi END) AS pqi%d, max(CASE WHEN s = %d THEN qc END) AS pqc%d, max(CASE WHEN s = %d THEN qts END) AS pqt%d, max(CASE WHEN s = %d THEN qsq END) AS pqs%d" % ((v, v) * 8) for v in PXSEC), ', '.join("max(CASE WHEN s = %d AND ok THEN bp END) AS pb%d, max(CASE WHEN s = %d AND ok THEN ap END) AS pa%d, max(CASE WHEN s = %d AND ok THEN az END) AS pz%d, max(CASE WHEN s = %d AND ok THEN bs END) AS pbz%d" % ((v, v) * 4) for v in marks)))
    c.execute("""CREATE OR REPLACE TEMP TABLE rg AS SELECT sym, ts // %d AS mk, min(bp) AS blo, max(bp) AS bhi, min(ap) AS alo, max(ap) AS ahi, arg_min((ts %% %d) / %d.0, (bp, ts)) AS blos, arg_max((ts %% %d) / %d.0, (bp, -ts)) AS bhis, min(bp) FILTER (WHERE ts %% %d > 5 * CAST(%d AS BIGINT)) AS blo5, arg_min((ts %% %d) / %d.0, (bp, ts)) FILTER (WHERE ts %% %d > 5 * CAST(%d AS BIGINT)) AS blo5s FROM qd WHERE %s GROUP BY 1, 2""" % (MIN, MIN, NS, MIN, NS, MIN, NS, MIN, NS, MIN, NS, OK))
    # (v11c4) FILLABLE bid = bid > 0, size > 0, state two-sided / bid only / locked (not crossed), indicator not 2, 4, 8, condition not 20. Lows over fillable bids: whole minute (pblof, second pblofs; the state standing at second 0 included) and from second 5 (pblo5, pblo5s; the state at second 5 included).
    c.execute("""CREATE OR REPLACE TEMP TABLE rgf AS SELECT sym, ts // %d AS mk, min(bp) AS blof, arg_min((ts %% %d) / %d.0, (bp, ts)) AS blofs, min(bp) FILTER (WHERE ts %% %d > 5 * CAST(%d AS BIGINT)) AS blof5, arg_min((ts %% %d) / %d.0, (bp, ts)) FILTER (WHERE ts %% %d > 5 * CAST(%d AS BIGINT)) AS blof5s FROM qd WHERE %s GROUP BY 1, 2""" % (MIN, MIN, NS, MIN, NS, MIN, NS, MIN, NS, FILLX))
    # (v11c4) TERMINAL MARK: the LAST quote update of the minute (the one whose prices and sizes tk stores as qbid qask qbsl qasl): its state, codes, exact SIP timestamp and sequence number. Only for minutes with a quote update.
    c.execute("""CREATE OR REPLACE TEMP TABLE le AS SELECT sym, mk, %s AS pstE, r.qi AS pqiE, r.qc AS pqcE, r.ts AS pqtE, r.sq AS pqsE, r.bp AS ebp, r.ap AS eap, r.bs AS ebs, r.az AS eaz FROM (SELECT sym, ts // %d AS mk, arg_max(qd, ts) AS r FROM qd GROUP BY 1, 2)""" % (STE, MIN))
    c.execute("""CREATE OR REPLACE TEMP TABLE qcd AS SELECT sym, ts // %d AS mk, array_to_string(list_sort(list_distinct(flatten(list(string_split(qi, ',')) FILTER (WHERE qi <> '')))), ',') AS qind, array_to_string(list_sort(list_distinct(flatten(list(string_split(qc, ',')) FILTER (WHERE qc <> '')))), ',') AS qcond FROM q WHERE qi <> '' OR qc <> '' GROUP BY 1, 2""" % MIN)
    c.execute("""CREATE OR REPLACE TEMP TABLE tx AS SELECT ta.sym, ta.ts // %d AS mk, max(ta.ts) AS lts, arg_min(sec, (px, ta.ts)) FILTER (WHERE ehl) AS xlos, arg_max(sec, (px, -ta.ts)) FILTER (WHERE ehl) AS xhis,
        CASE WHEN any_value(spt.pb0) IS NOT NULL THEN sum(CASE WHEN px <= spt.pb0 THEN sz ELSE 0 END) END AS vleb0, CASE WHEN any_value(spt.pb0) IS NOT NULL THEN sum(CASE WHEN px <= (spt.pb0 + spt.pa0) / 2 THEN sz ELSE 0 END) END AS vlem0, CASE WHEN any_value(spt.pa0) IS NOT NULL THEN sum(CASE WHEN px >= spt.pa0 THEN sz ELSE 0 END) END AS vgea0,
        median((ta.ts - pt) / 1000000.0) FILTER (WHERE pt > 0) AS tdlm, max((ta.ts - pt) / 1000000.0) FILTER (WHERE pt > 0) AS tdlx, %s
        FROM ta LEFT JOIN spt ON ta.sym = spt.sym AND ta.ts // %d = spt.mk GROUP BY 1, 2""" % (MIN, ', '.join("arg_max(px, (ta.ts, sq)) FILTER (WHERE el AND CAST(floor(sec / 10) AS INTEGER) = %d) AS l%d, sum(sz) FILTER (WHERE CAST(floor(sec / 10) AS INTEGER) = %d) AS v%d, sum(sd * sz) FILTER (WHERE CAST(floor(sec / 10) AS INTEGER) = %d) AS s%d" % ((k, k) * 3) for k in range(6)), MIN))
    c.execute("""CREATE OR REPLACE TEMP TABLE lq AS SELECT tx.sym, tx.mk, CASE WHEN %s THEN qd.bp END AS pbl, CASE WHEN %s THEN qd.ap END AS pal FROM tx ASOF LEFT JOIN qd ON tx.sym = qd.sym AND tx.lts > qd.ts""" % (OK.replace('bp', 'qd.bp').replace('ap', 'qd.ap').replace('bs', 'qd.bs').replace('az', 'qd.az'), OK.replace('bp', 'qd.bp').replace('ap', 'qd.ap').replace('bs', 'qd.bs').replace('az', 'qd.az')))
    LK = "CAST(CASE WHEN (pb0 + pa0) / 2 >= 1 THEN floor((pb0 + pa0) / 2 * 100 + 1e-7) * 100 ELSE floor((pb0 + pa0) / 2 * 10000 + 1e-7) END AS BIGINT)"   # the limit in 1 / 10000 of a dollar
    if 'c' in V11:
        c.execute("CREATE OR REPLACE TEMP TABLE lm AS SELECT sym, mk, %s AS lk, ra5 FROM spt WHERE pb0 IS NOT NULL" % LK)
        c.execute("""CREATE OR REPLACE TEMP TABLE pf AS SELECT ta.sym, ta.ts // %d AS mk, sum(CASE WHEN CAST(round(px * 10000) AS BIGINT) = lm.lk THEN sz ELSE 0 END) AS pvat, sum(CASE WHEN CAST(round(px * 10000) AS BIGINT) < lm.lk THEN sz ELSE 0 END) AS pvbl,
            min(CASE WHEN CAST(round(px * 10000) AS BIGINT) = lm.lk THEN (ta.ts %% %d) // 1000000 END) AS ptat, min(CASE WHEN CAST(round(px * 10000) AS BIGINT) < lm.lk THEN (ta.ts %% %d) // 1000000 END) AS ptbl
            FROM ta JOIN lm ON ta.sym = lm.sym AND ta.ts // %d = lm.mk WHERE ta.pe AND ta.ts %% %d > 5 * CAST(%d AS BIGINT) GROUP BY 1, 2""" % (MIN, MIN, MIN, MIN, MIN, NS))
        c.execute("""CREATE OR REPLACE TEMP TABLE pk AS SELECT qd.sym, qd.ts // %d AS mk, min((qd.ts %% %d) // 1000000) AS tak FROM qd JOIN lm ON qd.sym = lm.sym AND qd.ts // %d = lm.mk WHERE qd.ap > 0 AND qd.az > 0 AND CAST(round(qd.ap * 10000) AS BIGINT) <= lm.lk AND qd.ts %% %d > 5 * CAST(%d AS BIGINT) GROUP BY 1, 2""" % (MIN, MIN, MIN, MIN, NS))
    XC = (', ' + ', '.join("spt.pg%d, spt.pst%d" % (v, v) for v in PXSEC) if 'a' in V11 else '') + (', least(rgf.blof5, spt.fb5) AS pblo5, least(rgf.blof, spt.fb0) AS pblof, CASE WHEN spt.fb0 IS NOT NULL AND (rgf.blof IS NULL OR spt.fb0 <= rgf.blof) THEN 0 ELSE rgf.blofs END AS pblofs, le.pstE, le.pqiE, le.pqcE, le.pqtE, le.pqsE, le.ebp AS _ebp, le.eap AS _eap, le.ebs AS _ebs, le.eaz AS _eaz' if 'b' in V11 else '') + ((', CASE WHEN spt.fb5 IS NOT NULL AND (rgf.blof5 IS NULL OR spt.fb5 <= rgf.blof5) THEN 5 ELSE rgf.blof5s END AS pblo5s, ' + ', '.join('spt.qb%d, spt.qa%d, spt.qbz%d, spt.qaz%d, spt.pqi%d, spt.pqc%d, spt.pqt%d, spt.pqs%d' % ((v,) * 8) for v in PXSEC)) if 'd' in V11 else '') + (", lm.lk / 10000.0 AS plim, CASE WHEN lm.lk IS NOT NULL THEN coalesce(pf.pvat, 0) END AS pvat, CASE WHEN lm.lk IS NOT NULL THEN coalesce(pf.pvbl, 0) END AS pvbl, pf.ptat, pf.ptbl, CASE WHEN lm.ra5 IS NOT NULL AND CAST(round(lm.ra5 * 10000) AS BIGINT) <= lm.lk THEN 5000 ELSE pk.tak END AS ptak" if 'c' in V11 else '')
    JX = (" LEFT JOIN lm ON kk.sym = lm.sym AND kk.mk = lm.mk LEFT JOIN pf ON kk.sym = pf.sym AND kk.mk = pf.mk LEFT JOIN pk ON kk.sym = pk.sym AND kk.mk = pk.mk" if 'c' in V11 else '')
    J = "FROM kk LEFT JOIN spt ON kk.sym = spt.sym AND kk.mk = spt.mk LEFT JOIN rg ON kk.sym = rg.sym AND kk.mk = rg.mk LEFT JOIN tx ON kk.sym = tx.sym AND kk.mk = tx.mk LEFT JOIN lq ON kk.sym = lq.sym AND kk.mk = lq.mk LEFT JOIN qcd ON kk.sym = qcd.sym AND kk.mk = qcd.mk LEFT JOIN au ON kk.sym = au.sym AND kk.mk = au.mk LEFT JOIN rgf ON kk.sym = rgf.sym AND kk.mk = rgf.mk LEFT JOIN le ON kk.sym = le.sym AND kk.mk = le.mk"
    c.execute(("CREATE TEMP TABLE pxm AS " if first else "INSERT INTO pxm ") + """SELECT sym, %s AS m, * EXCLUDE (sym, ts) FROM (SELECT kk.sym, kk.t0 AS ts, %s,
        least(rg.blo, spt.pb0) AS pblo, greatest(rg.bhi, spt.pb0) AS pbhi, least(rg.alo, spt.pa0) AS palo, greatest(rg.ahi, spt.pa0) AS pahi, lq.pbl, lq.pal,
        CASE WHEN spt.pb0 IS NOT NULL AND (rg.blo IS NULL OR spt.pb0 <= rg.blo) THEN 0 ELSE rg.blos END AS pblos, CASE WHEN spt.pb0 IS NOT NULL AND (rg.bhi IS NULL OR spt.pb0 >= rg.bhi) THEN 0 ELSE rg.bhis END AS pbhis, tx.xlos, tx.xhis, tx.vleb0, tx.vlem0, tx.vgea0, qcd.qind, qcd.qcond, au.opx, au.opv, au.clx, au.clv, au.ofo, au.ofc, tx.tdlm, tx.tdlx%s %s%s)""" % (mexpr, ', '.join("spt.pb%d, spt.pa%d, spt.pz%d, spt.pbz%d" % ((v,) * 4) for v in PXSEC), XC, J, JX))
    c.execute(("CREATE TEMP TABLE s10m AS " if first else "INSERT INTO s10m ") + """SELECT sym, %s AS m, * EXCLUDE (sym, ts) FROM (SELECT kk.sym, kk.t0 AS ts, %s %s)""" % (mexpr, ', '.join("tx.l%d, tx.v%d, tx.s%d, spt.pb%d AS b%d, spt.pa%d AS a%d" % (k, k, k, 10 * (k + 1), k, 10 * (k + 1), k) for k in range(6)), J))

OUTOPTS = ('TICK_V11', 'TICK_TYPES', 'TICK_RAWSUB', 'TICK_RAWSUB_DAYS', 'TICK_RAWLIST')   # the options that change what a day's files hold

def manmatch(man, accept, opts, sizes, etags=None):
    # (v11c4) is a day's manifest the work of an ACCEPTED build with today's options, and are its files still the ones it recorded? accept = set of accepted script md5s; opts = today's effective options; sizes = {'tk' / 'px' / 's10': bytes of the stored object or None}.
    # Returns '' when it matches, otherwise the reason (the day is then requeued).
    if not isinstance(man, dict): return 'no readable manifest'
    if man.get('script_md5') not in accept: return 'made by another build (script md5 %s)' % man.get('script_md5')
    mo = man.get('options') or {}
    for k in OUTOPTS:
        if (mo.get(k) or None) != (opts.get(k) or None): return 'option %s differs' % k
    cols = man.get('columns') or {}; files = man.get('files') or {}
    for k in ('tk', 'px', 's10'):
        if not cols.get(k): return 'no column list for %s' % k
        if k not in files or sizes.get(k) != files[k].get('bytes'): return '%s object is not the one the manifest recorded' % k
        if files[k].get('etag') and (etags or {}).get(k) != files[k]['etag']: return '%s object has another ETag than the manifest recorded' % k
    return ''

def rawsub(fn, out, lst, tmp):
    # (v11c, OFF unless TICK_RAWSUB is set) lossless copy of the raw rows of the listed stocks: every column of the source file as text, nothing cast, nothing dropped; parquet zstd. Returns the row count.
    c = duckdb.connect(); c.execute("SET temp_directory='%s'" % tmp); c.execute("SET threads=%d" % int(os.environ.get('TICK_RAWSUB_THREADS', '4')))
    rd = "read_csv('%s', header=true, all_varchar=true, delim=',', quote='\"', escape='\"')"
    c.execute("COPY (SELECT r.* FROM %s r WHERE r.ticker IN (SELECT ticker FROM %s)) TO '%s' (FORMAT parquet, COMPRESSION zstd)" % (rd % fn, rd % lst, out)); n = c.execute("SELECT count(*) FROM read_parquet('%s')" % out).fetchall()[0][0]; c.close(); return n

TIES = {}   # per day: trades that share their SIP timestamp with a quote update of the same stock (the join below takes the last quote STRICTLY EARLIER than the trade, so such a quote is never attached); written into the manifest

def day(tfn, qfn, out, tmp, types=None, pxout=None, s10out=None):
    TIES.clear(); TIES.update(trades=0, trades_sharing_a_timestamp_with_a_quote_of_the_stock=0)
    # step 1: one streaming pass over each raw file -> a local columnar copy with only the needed columns (low memory); step 2: the stocks are cut into KB alphabetical groups of about equal size and each group is worked in memory
    c = con(); c.execute("SET temp_directory='%s'" % tmp); header(c, tfn, TCOLS + (['participant_timestamp'] if pxout else [])); header(c, qfn, QCOLS); qp, tp = os.path.join(tmp, 'q.parquet'), os.path.join(tmp, 't.parquet')
    keep = ""
    if types: c.execute("CREATE TEMP TABLE u AS SELECT DISTINCT ticker FROM read_csv('%s', header=true, all_varchar=true, delim=',', quote='\"', escape='\"') UNION SELECT 'SPY' UNION SELECT 'QQQ'" % types); keep = " AND ticker IN (SELECT ticker FROM u)"
    mexpr = "CAST(date_part('hour', timezone('%s', make_timestamp(ts // 1000) AT TIME ZONE 'UTC')) * 60 + date_part('minute', timezone('%s', make_timestamp(ts // 1000) AT TIME ZONE 'UTC')) AS INTEGER)" % (TZ, TZ)
    c.execute("""COPY (SELECT ticker AS sym, CAST(sip_timestamp AS BIGINT) AS ts, CAST(sequence_number AS BIGINT) AS sq, CAST(bid_price AS DOUBLE) AS bp, CAST(ask_price AS DOUBLE) AS ap, CAST(bid_size AS DOUBLE) AS bs, CAST(ask_size AS DOUBLE) AS az,
        (coalesce(conditions, '') <> '' OR coalesce(indicators, '') <> '') AS flag%s FROM read_csv('%s', header=true, all_varchar=true, delim=',', quote='"', escape='"') WHERE true%s) TO '%s' (FORMAT parquet, COMPRESSION lz4, ROW_GROUP_SIZE 1000000)""" % (", coalesce(conditions, '') AS qc, coalesce(indicators, '') AS qi" if pxout else "", qfn, keep, qp))
    TSEL = "sym, ts, sq, px, sz, off, nohl, nolast, ft, iso"   # the columns of the trade table as before; with the side files the local copy also holds the dropped prints (kp = false) and a few extra columns
    if pxout:
        cl = "string_split(coalesce(conditions, ''), ',')"
        c.execute("""COPY (SELECT ticker AS sym, CAST(sip_timestamp AS BIGINT) AS ts, CAST(sequence_number AS BIGINT) AS sq, CAST(price AS DOUBLE) AS px, CAST(size AS DOUBLE) AS sz, (coalesce(trf_id, '') NOT IN ('', '0')) AS off,
            list_has_any(%s, %s) AS nohl, list_has_any(%s, %s) AS nolast, list_contains(%s, '%s') AS ft, list_contains(%s, '%s') AS iso, (CAST(size AS DOUBLE) > 0 AND NOT list_has_any(%s, %s)) AS kp, coalesce(TRY_CAST(participant_timestamp AS BIGINT), 0) AS pt,
            list_contains(%s, '%s') AS c8, list_contains(%s, '%s') AS c25, list_contains(%s, '%s') AS c15, list_contains(%s, '%s') AS c16, (coalesce(trf_id, '') IN ('', '0') AND NOT list_has_any(%s, %s)) AS pe
            FROM read_csv('%s', header=true, all_varchar=true, delim=',', quote='"', escape='"') WHERE coalesce(TRY_CAST(correction AS INTEGER), 0) NOT IN (%s) AND CAST(price AS DOUBLE) > 0%s) TO '%s' (FORMAT parquet, COMPRESSION lz4, ROW_GROUP_SIZE 1000000)""" % (cl, lst(C_NOHL), cl, lst(C_NOLAST), cl, C_FORMT, cl, C_ISO, cl, lst(C_DROP), cl, C_CLOSEP, cl, C_OPENP, cl, C_OFFC, cl, C_OFFO, cl, lst(C_NOPASS), tfn, ', '.join(str(v) for v in CORR_LATER), keep, tp))
        TSEL += ", pt, pe"
    else:
        c.execute("""COPY (SELECT ticker AS sym, CAST(sip_timestamp AS BIGINT) AS ts, CAST(sequence_number AS BIGINT) AS sq, CAST(price AS DOUBLE) AS px, CAST(size AS DOUBLE) AS sz, (coalesce(trf_id, '') NOT IN ('', '0')) AS off,
            list_has_any(string_split(coalesce(conditions, ''), ','), %s) AS nohl, list_has_any(string_split(coalesce(conditions, ''), ','), %s) AS nolast, list_contains(string_split(coalesce(conditions, ''), ','), '%s') AS ft, list_contains(string_split(coalesce(conditions, ''), ','), '%s') AS iso
            FROM read_csv('%s', header=true, all_varchar=true, delim=',', quote='"', escape='"') WHERE coalesce(TRY_CAST(correction AS INTEGER), 0) NOT IN (%s) AND CAST(price AS DOUBLE) > 0 AND CAST(size AS DOUBLE) > 0 AND NOT list_has_any(string_split(coalesce(conditions, ''), ','), %s)%s) TO '%s' (FORMAT parquet, COMPRESSION lz4, ROW_GROUP_SIZE 1000000)""" % (lst(C_NOHL), lst(C_NOLAST), C_FORMT, C_ISO, tfn, ', '.join(str(v) for v in CORR_LATER), lst(C_DROP), keep, tp))
    cnt = c.execute("SELECT sym, count(*) FROM read_parquet('%s') GROUP BY 1 ORDER BY 1" % qp).fetchall(); tot = sum(x[1] for x in cnt); cuts = []; acc = 0
    for sym, n in cnt:
        if acc >= (len(cuts) + 1) * tot / KB and len(cuts) < KB - 1: cuts.append(sym)
        acc += n
    edges = [None] + cuts + [None]; first = True
    for lo, hi in zip(edges[:-1], edges[1:]):
        w = " WHERE true" + (" AND sym >= '%s'" % lo.replace("'", "''") if lo is not None else "") + (" AND sym < '%s'" % hi.replace("'", "''") if hi is not None else "")
        c.execute("CREATE OR REPLACE TEMP TABLE q AS SELECT * FROM read_parquet('%s')%s" % (qp, w)); c.execute("CREATE OR REPLACE TEMP TABLE t AS SELECT %s FROM read_parquet('%s')%s%s" % (TSEL, tp, w, " AND kp" if pxout else ""))
        # quotes per minute; time weights: each update stands until the next one of the same stock, counted inside its own minute only (cut at the minute end)
        c.execute(("CREATE TEMP TABLE qm AS " if first else "INSERT INTO qm ") + """WITH a AS (SELECT *, %s AS m, lead(ts) OVER w AS nts, lag(bp) OVER w AS pbp, lag(ap) OVER w AS pap, lag(bs) OVER w AS pbs, lag(az) OVER w AS paz, lag(ts) OVER w AS pts FROM q WINDOW w AS (PARTITION BY sym ORDER BY ts, sq)),
        b AS (SELECT *, (least(coalesce(nts, ts), (ts // %d + 1) * %d) - ts) / %d.0 AS d, (bp > 0 AND ap > bp AND bs > 0 AND az > 0) AS ok,
            CASE WHEN pbp IS NULL THEN 0 ELSE (CASE WHEN bp >= pbp THEN bs ELSE 0 END) - (CASE WHEN bp <= pbp THEN pbs ELSE 0 END) - (CASE WHEN ap <= pap OR pap <= 0 THEN az ELSE 0 END) + (CASE WHEN ap >= pap AND pap > 0 THEN paz ELSE 0 END) END AS e FROM a)
        SELECT sym, m, count(*) AS qn, sum(CASE WHEN ok THEN d * (ap - bp) / ((ap + bp) / 2) END) / nullif(sum(CASE WHEN ok THEN d END), 0) AS qspr, sum(CASE WHEN ok THEN d * bs END) / nullif(sum(CASE WHEN ok THEN d END), 0) AS qbs, sum(CASE WHEN ok THEN d * az END) / nullif(sum(CASE WHEN ok THEN d END), 0) AS qas,
            sum(CASE WHEN ok THEN d * (bs - az) / (bs + az) END) / nullif(sum(CASE WHEN ok THEN d END), 0) AS qimb, sum(e) AS qofi, max((ts - pts) / %d.0) AS qgap, sum(CASE WHEN NOT ok THEN d ELSE 0 END) / nullif(sum(d), 0) AS qbad,
            arg_max(struct_pack(bp := bp, ap := ap, bs := bs, az := az), (ts, sq)).bp AS qbid, arg_max(struct_pack(bp := bp, ap := ap, bs := bs, az := az), (ts, sq)).ap AS qask, sum(CAST(flag AS INTEGER)) AS qhalt,
            sum(CASE WHEN ok AND pbp > 0 AND pap > pbp THEN power(ln((ap + bp) / (pap + pbp)), 2) ELSE 0 END) AS qmvar, arg_max(struct_pack(bp := bp, ap := ap, bs := bs, az := az), (ts, sq)).bs AS qbsl, arg_max(struct_pack(bp := bp, ap := ap, bs := bs, az := az), (ts, sq)).az AS qasl, sum(CASE WHEN ok THEN d * bs * bp END) / nullif(sum(CASE WHEN ok THEN d END), 0) AS qbd, sum(CASE WHEN ok THEN d * az * ap END) / nullif(sum(CASE WHEN ok THEN d END), 0) AS qad FROM b GROUP BY sym, m""" % (mexpr, MIN, MIN, NS, NS))
        # trades per minute; side by the quote standing just before the trade
        # (v9, 2026-11-20) several quote updates can carry the same timestamp: the one with the highest sequence number is the standing quote. Before v9 the as-of join took an arbitrary one of them,
        # so two runs of the same day differed in the quote-signed columns (2026-11-11: 3% of the rows). No column added or removed.
        r_ = c.execute("SELECT (SELECT count(*) FROM t), count(*) FROM t SEMI JOIN (SELECT DISTINCT sym, ts FROM q) q2 ON t.sym = q2.sym AND t.ts = q2.ts").fetchall()[0]; TIES['trades'] += int(r_[0]); TIES['trades_sharing_a_timestamp_with_a_quote_of_the_stock'] += int(r_[1])
        TSQL = """WITH j AS (SELECT t.*, q.bp, q.ap FROM t ASOF LEFT JOIN (SELECT sym, ts, r.bp AS bp, r.ap AS ap FROM (SELECT sym, ts, arg_max(struct_pack(bp := bp, ap := ap), sq) AS r FROM q GROUP BY sym, ts)) q ON t.sym = q.sym AND t.ts > q.ts),
        a0 AS (SELECT *, %s AS m, (ts %% %d) / %d.0 AS sec, CASE WHEN bp > 0 AND ap > bp THEN (ap + bp) / 2 END AS mid, CAST(round(px * 10000) AS BIGINT) %% 100 AS sub FROM j),
        a1 AS (SELECT *, (NOT nohl AND NOT (ft AND m BETWEEN %d AND %d)) AS ehl FROM a0),
        a AS (SELECT *, (ehl AND NOT nolast) AS el, lag(px) OVER (PARTITION BY sym, (ehl AND NOT nolast) ORDER BY ts, sq) AS ppx, lag(ts) OVER (PARTITION BY sym ORDER BY ts, sq) AS pts, CASE WHEN px > mid THEN 1 WHEN px < mid THEN -1 ELSE 0 END AS sd FROM a1),
        s AS (SELECT sym, m, max(v) AS tburst FROM (SELECT sym, m, CAST(floor(sec) AS INTEGER) AS s1, sum(sz) AS v FROM a GROUP BY 1, 2, 3) GROUP BY 1, 2),
        g AS (SELECT sym, m, count(*) AS tn, sum(sz) AS tv, sum(sz * px) AS tdv, max(sz * px) AS tmax, sum(CASE WHEN sz * px >= %r THEN sz * px ELSE 0 END) / sum(sz * px) AS tbig, sum(CASE WHEN off THEN sz ELSE 0 END) / sum(sz) AS toff,
            sum(CASE WHEN sd > 0 THEN sz ELSE 0 END) AS tbuy, sum(CASE WHEN sd < 0 THEN sz ELSE 0 END) AS tsell, sum(CASE WHEN el AND ppx > 0 THEN power(ln(px / ppx), 2) ELSE 0 END) AS tvar, sum(CASE WHEN el AND ppx IS NOT NULL AND px <> ppx THEN 1 ELSE 0 END) AS tchg,
            sum(sec * sz) / sum(sz) AS twt, arg_max(px, (ts, sq)) FILTER (WHERE el) AS tlast,
            arg_min(px, (ts, sq)) FILTER (WHERE el) AS topen, max(px) FILTER (WHERE ehl) AS thi, min(px) FILTER (WHERE ehl) AS tlo, sum(CASE WHEN sz * px >= %r THEN sz * px ELSE 0 END) / sum(sz * px) AS tbig5, sum(CASE WHEN sz * px >= %r THEN sz * px ELSE 0 END) / sum(sz * px) AS tbig100,
            sum(CASE WHEN sz < 100 THEN 1 ELSE 0 END) AS todd, sum(CASE WHEN sz < 100 THEN sz ELSE 0 END) AS tvodd, sum(CASE WHEN sd > 0 THEN 1 ELSE 0 END) AS tbuyn, sum(CASE WHEN sd < 0 THEN 1 ELSE 0 END) AS tselln,
            arg_max(px, (ts, sq)) AS tlasta, sum(CAST(el AS INTEGER)) AS tne, sum(CASE WHEN el THEN sz ELSE 0 END) AS tve,
            sum(CAST(iso AS INTEGER)) AS tiso, sum(CASE WHEN iso THEN sz ELSE 0 END) AS tviso, sum(CASE WHEN iso AND sd > 0 THEN sz ELSE 0 END) AS tisob, sum(CASE WHEN iso AND sd < 0 THEN sz ELSE 0 END) AS tisos,
            sum(CASE WHEN mid IS NOT NULL THEN sz * abs(px - mid) / mid END) / nullif(sum(CASE WHEN mid IS NOT NULL THEN sz END), 0) AS tesp, sum(CASE WHEN mid IS NOT NULL AND px > ap THEN sz ELSE 0 END) AS tabove, sum(CASE WHEN mid IS NOT NULL AND px < bp THEN sz ELSE 0 END) AS tbelow,
            sum(CASE WHEN mid IS NOT NULL AND px > bp AND px < ap THEN sz ELSE 0 END) AS tin, sum(CASE WHEN mid IS NOT NULL THEN sz ELSE 0 END) AS tvq,
            sum(CASE WHEN off AND px >= 1 AND sub > 60 THEN sz ELSE 0 END) AS trb, sum(CASE WHEN off AND px >= 1 AND sub > 0 AND sub < 40 THEN sz ELSE 0 END) AS trs,
            sum(CASE WHEN off AND px >= 1 AND sub > 0 AND mid IS NOT NULL AND (px - bp) / (ap - bp) > 0.6 THEN sz ELSE 0 END) AS trbq, sum(CASE WHEN off AND px >= 1 AND sub > 0 AND mid IS NOT NULL AND (px - bp) / (ap - bp) < 0.4 THEN sz ELSE 0 END) AS trsq,
            arg_max(px, (ts, sq)) FILTER (WHERE el AND sec < 15) AS tp15, arg_max(px, (ts, sq)) FILTER (WHERE el AND sec < 45) AS tp45, sum(CASE WHEN sec < 15 THEN sz ELSE 0 END) AS tv15f, sum(CASE WHEN sec < 15 THEN sd * sz ELSE 0 END) AS ts15f,
            sum(CASE WHEN sec >= 45 THEN sz ELSE 0 END) AS tv15l, sum(CASE WHEN sec >= 45 THEN sd * sz ELSE 0 END) AS ts15l, max((ts - pts) / %d.0) AS tgap FROM a GROUP BY sym, m)
        SELECT g.*, s.tburst FROM g JOIN s ON g.sym = s.sym AND g.m = s.m""" % (mexpr, MIN, NS, RTH[0], RTH[1], BIG, BIG5, BIG100, NS)
        if pxout:   # with the side files the joined trade table is kept (ta) and used for the 61 columns and for the side files alike: same statements, same numbers
            TA, TB = TSQL.split(",\n        s AS (SELECT sym, m, max(v) AS tburst", 1); c.execute("CREATE OR REPLACE TEMP TABLE ta AS " + TA + " SELECT * FROM a")
            c.execute(("CREATE TEMP TABLE tm AS " if first else "INSERT INTO tm ") + "WITH s AS (SELECT sym, m, max(v) AS tburst" + TB.replace(" FROM a GROUP BY", " FROM ta GROUP BY"))
        else: c.execute(("CREATE TEMP TABLE tm AS " if first else "INSERT INTO tm ") + TSQL)
        if pxout: side(c, first, mexpr, tp, w)
        first = False
    c.execute("COPY (SELECT coalesce(tm.sym, qm.sym) AS sym, coalesce(tm.m, qm.m) AS m, tm.* EXCLUDE (sym, m), qm.* EXCLUDE (sym, m) FROM tm FULL JOIN qm ON tm.sym = qm.sym AND tm.m = qm.m ORDER BY 1, 2) TO '%s' (FORMAT parquet, COMPRESSION zstd)" % out)
    EXC = ''
    if pxout and 'b' in V11:   # (v11c4) the terminal mark must be the very update whose prices and sizes tk stores; checked for every stock-minute with a quote update, then the four helper columns are left out of the file
        bad, nq, ne = c.execute("SELECT sum(CASE WHEN p.pqtE IS NULL OR NOT (p._ebp IS NOT DISTINCT FROM q.qbid AND p._eap IS NOT DISTINCT FROM q.qask AND p._ebs IS NOT DISTINCT FROM q.qbsl AND p._eaz IS NOT DISTINCT FROM q.qasl) THEN 1 ELSE 0 END), count(*), (SELECT count(pqtE) FROM pxm) FROM qm q LEFT JOIN pxm p ON p.sym = q.sym AND p.m = q.m").fetchall()[0]
        if bad or nq != ne: raise SystemExit('REFUSED: terminal mark differs from the tk last quote in %s of %s stock-minutes (terminal marks written: %s)' % (bad, nq, ne))
        TIES['terminal_marks_equal_to_tk_last_quote'] = int(nq); EXC = ' EXCLUDE (_ebp, _eap, _ebs, _eaz)'
    for tb, fn in (('pxm', pxout), ('s10m', s10out)):
        if not fn: continue
        ex = EXC if tb == 'pxm' else ''
        try: c.execute("COPY (SELECT *%s FROM %s ORDER BY 1, 2) TO '%s' (FORMAT parquet, COMPRESSION zstd, KV_METADATA {note: '%s'})" % (ex, tb, fn, PXNOTE))
        except duckdb.Error: c.execute("COPY (SELECT *%s FROM %s ORDER BY 1, 2) TO '%s' (FORMAT parquet, COMPRESSION zstd)" % (ex, tb, fn))
    n = c.execute("SELECT count(*), count(DISTINCT sym) FROM read_parquet('%s')" % out).fetchone(); c.close(); os.remove(qp); os.remove(tp); return n

def creds():
    import boto3
    if os.environ.get('TICK_SECRET'): s = json.loads(boto3.client('secretsmanager').get_secret_value(SecretId=os.environ['TICK_SECRET'])['SecretString']); return s['MASSIVE_S3_ACCESS_KEY_ID'], s['MASSIVE_S3_SECRET_ACCESS_KEY']
    return os.environ['MASSIVE_S3_ACCESS_KEY_ID'], os.environ['MASSIVE_S3_SECRET_ACCESS_KEY']

def main():
    if sys.argv[1] == 'local':
        t0 = time.time(); n = day(sys.argv[2], sys.argv[3], sys.argv[4], os.path.dirname(os.path.abspath(sys.argv[4])), os.environ.get('TICK_TYPES')); print('DONE rows', n[0], 'stocks', n[1], 'sec', round(time.time() - t0)); return
    import boto3, botocore, boto3.s3.transfer
    if sys.argv[1] == 'corr':
        dt, work = sys.argv[2], sys.argv[3]; os.makedirs(work, exist_ok=True)
        if SEAL[0] <= dt <= SEAL[1] or dt > HARD_LAST: raise SystemExit('REFUSED: sealed day')
        k, s = creds(); src = boto3.client('s3', endpoint_url='https://files.massive.com', aws_access_key_id=k, aws_secret_access_key=s, config=botocore.config.Config(signature_version='s3v4')); del k, s
        fn = os.path.join(work, dt + '-trades-corr.csv.gz'); src.download_file('flatfiles', 'us_stocks_sip/trades_v1/%s/%s/%s.csv.gz' % (dt[:4], dt[5:7], dt), fn); c = con(); c.execute("SET temp_directory='%s'" % work)
        cols = [r[0] for r in c.execute("DESCRIBE SELECT * FROM read_csv('%s', header=true, all_varchar=true, delim=',', quote='\"', escape='\"', sample_size=100)" % fn).fetchall()]; print('columns', cols)
        rd = "read_csv('%s', header=true, all_varchar=true, delim=',', quote='\"', escape='\"')" % fn
        for r in c.execute("SELECT coalesce(correction, '') AS code, count(*) AS n, sum(CAST(size AS DOUBLE)) AS shares, sum(CAST(CAST(price AS DOUBLE) > 0 AND CAST(size AS DOUBLE) > 0 AS INTEGER)) AS valid, count(DISTINCT ticker) AS stocks FROM %s GROUP BY 1 ORDER BY 2 DESC" % rd).fetchall(): print('CORR code %r trades %d shares %.0f with price and size %d stocks %d' % r)
        if 'id' in cols: print('CORR (ticker, id) pairs seen more than once: %d; of them with different codes: %d' % c.execute("SELECT count(*), sum(CAST(k > 1 AS INTEGER)) FROM (SELECT ticker, id, count(*) AS n, count(DISTINCT coalesce(correction, '')) AS k FROM %s GROUP BY 1, 2 HAVING count(*) > 1)" % rd).fetchone())
        c.close(); os.remove(fn); print('DONE'); return
    first, last, work, out = sys.argv[1:5]; os.makedirs(work, exist_ok=True); k, s = creds()
    src = boto3.client('s3', endpoint_url='https://files.massive.com', aws_access_key_id=k, aws_secret_access_key=s, config=botocore.config.Config(signature_version='s3v4')); del k, s
    dst = boto3.client('s3') if out.startswith('s3://') else None; bucket, _, prefix = out[5:].partition('/') if dst else ('', '', '')
    types = os.environ.get('TICK_TYPES'); raw = os.environ.get('TICK_RAW')
    # TICK_RAWSUB = s3://bucket/prefix: keep a lossless raw subset (trades and quotes of the stocks of TICK_RAWLIST, default the TICK_TYPES file) for the days of TICK_RAWSUB_DAYS=from:to (default every day). Keys <prefix>/<trades|quotes>/YYYY/DATE.parquet. Unset = nothing is kept.
    rsub = os.environ.get('TICK_RAWSUB'); rsb, _, rsp = rsub[5:].partition('/') if rsub else ('', '', ''); rsd = (os.environ.get('TICK_RAWSUB_DAYS') or '0000:9999').split(':')
    def rskey(kind, dt): return '%s/%s/%s/%s.parquet' % (rsp.rstrip('/'), kind, dt[:4], dt)
    def rswant(dt): return bool(rsub) and rsd[0] <= dt <= rsd[1]
    ACCEPT = set(x for x in (os.environ.get('TICK_ACCEPT_MD5') or hashlib.md5(open(os.path.abspath(__file__), 'rb').read()).hexdigest()).split(',') if x)   # accepted builds: this very script, unless TICK_ACCEPT_MD5 lists the accepted script md5s
    def manbad(dt):   # '' when the day's manifest and stored files match an accepted build, else the reason
        key = '%s/%s.parquet' % (dt[:4], dt); c3 = boto3.client('s3')
        try: man = json.loads(c3.get_object(Bucket=pxb, Key=mkey(dt))['Body'].read())
        except Exception: man = None
        sizes = {}; etags = {}
        for nm_, b_, k_ in (('tk', bucket, prefix.rstrip('/') + '/' + key), ('px', pxb, pxp.rstrip('/') + '/' + key), ('s10', s1b, s1p.rstrip('/') + '/' + key)):
            try: h_ = c3.head_object(Bucket=b_, Key=k_); sizes[nm_] = h_['ContentLength']; etags[nm_] = h_['ETag'].strip('"')
            except Exception: sizes[nm_] = None
        return manmatch(man, ACCEPT, {k: os.environ.get(k) for k in OUTOPTS}, sizes, etags)
    def rsok(dt):   # both subset objects exist with the sizes the day's manifest recorded
        try:
            man = json.loads(boto3.client('s3').get_object(Bucket=pxb, Key=mkey(dt))['Body'].read()); r = man.get('rawsub') or {}
            return all(k in r and boto3.client('s3').head_object(Bucket=rsb, Key=r[k]['key'])['ContentLength'] == r[k]['bytes'] for k in ('trades', 'quotes'))
        except Exception: return False
    RS = {'last': None}   # the raw-subset thread of the day before (threads are not daemons: the script does not end before the last upload has finished)
    if types and types.startswith('s3://'): b, _, k = types[5:].partition('/'); types = os.path.join(work, 'types.csv'); boto3.client('s3').download_file(b, k, types)
    d = datetime.date.fromisoformat(first); end = datetime.date.fromisoformat(min(last, HARD_LAST)); days = []
    while d <= end:
        if d.weekday() < 5 and not (SEAL[0] <= d.isoformat() <= SEAL[1]): days.append(d.isoformat())
        d += datetime.timedelta(days=1)
    if os.environ.get('TICK_ORDER') == 'desc': days.reverse()   # newest day first
    if os.environ.get('TICK_PART'): k, n = [int(x) for x in os.environ['TICK_PART'].split('/')]; days = days[k::n]   # TICK_PART=k/N: every N-th day starting at k, so N copies can run side by side (each with its own work dir)
    if os.environ.get('TICK_FIRST'): f0, f1 = os.environ['TICK_FIRST'].split(':'); days = [x for x in days if f0 <= x <= f1] + [x for x in days if not (f0 <= x <= f1)]   # TICK_FIRST=YYYY-MM-DD:YYYY-MM-DD: the days of this range are worked first (in the queue's own order), then the rest
    px = os.environ.get('TICK_PX'); pxb, _, pxp = px[5:].partition('/') if px else ('', '', ''); late = []; s10 = (os.environ.get('TICK_S10') or (px.rstrip('/')[:-2] + 's10' if px.rstrip('/').endswith('px') else px.rstrip('/') + '-s10')) if px else None; s1b, _, s1p = s10[5:].partition('/') if s10 else ('', '', '')   # TICK_PX = s3://bucket/prefix of the label-side price files (same day keys); a day that has its tk file but no px file is redone at the END of the queue and only its px file is uploaded
    if px and not (dst and px.startswith('s3://')): raise SystemExit('REFUSED: TICK_PX needs s3 outputs')
    def put(fn, b, k_, extra=None):   # (v10) upload that waits for a new AWS login instead of ending the job: an expired SSO login stopped the run twice on 2026-11-20. A new client per attempt picks up the refreshed login. Only login errors wait; anything else is raised.
        while True:
            try: boto3.client('s3').upload_file(fn, b, k_, ExtraArgs=extra); return
            except Exception as e:
                if not any(w in (type(e).__name__ + ' ' + str(e)) for w in LOGINERR): raise
                print('LOGIN NEEDED', datetime.datetime.utcnow().strftime('%H:%M:%SZ'), 'upload of', k_, 'waits: run  aws sso login --profile tickr  (checked again every %d s)' % LOGINWAIT, flush=True); time.sleep(LOGINWAIT)
    def has(b, k_):
        while True:
            try: boto3.client('s3').head_object(Bucket=b, Key=k_); return True
            except botocore.exceptions.ClientError as e:
                if not any(w in str(e) for w in LOGINERR): return False
            except Exception as e:
                if not any(w in (type(e).__name__ + ' ' + str(e)) for w in LOGINERR): raise
            print('LOGIN NEEDED', datetime.datetime.utcnow().strftime('%H:%M:%SZ'), 'check of', k_, 'waits: run  aws sso login --profile tickr  (checked again every %d s)' % LOGINWAIT, flush=True); time.sleep(LOGINWAIT)
    def work1(i, total, dt, pxonly):
        key = '%s/%s.parquet' % (dt[:4], dt); t0 = time.time()
        tfn, qfn, ofn, pfn, sfn = [os.path.join(work, dt + x) for x in ('-trades.csv.gz', '-quotes.csv.gz', '.parquet', '.px.parquet', '.s10.parquet')]
        try:
            for kind, fn in (('trades_v1', tfn), ('quotes_v1', qfn)):
                for attempt in range(1, TRIES + 1):   # a dropped connection is retried after a pause; a refusal by the server (ClientError) is not
                    if os.path.exists(fn): break
                    try: src.download_file('flatfiles', 'us_stocks_sip/%s/%s/%s/%s.csv.gz' % (kind, dt[:4], dt[5:7], dt), fn)
                    except botocore.exceptions.ClientError: raise
                    except Exception as e:
                        print('connection lost on', dt, kind, 'attempt', attempt, type(e).__name__, flush=True)
                        if attempt == TRIES: raise SystemExit('REFUSED: download of %s %s failed %d times' % (dt, kind, TRIES))
                        time.sleep(PAUSE * attempt)
        except botocore.exceptions.ClientError as e:
            code = e.response['Error']['Code']; [os.remove(x) for x in (tfn, qfn) if os.path.exists(x)]
            if code in ('404', 'NoSuchKey'): print('no file', dt, '(holiday)', flush=True); return
            raise SystemExit('REFUSED: download of %s failed with code %s' % (dt, code))
        man = {'date': dt, 'converter': VERSION, 'TICK_V11': V11, 'script_md5': hashlib.md5(open(os.path.abspath(__file__), 'rb').read()).hexdigest(), 'made_utc': datetime.datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ'), 'source': {}, 'files': {}}
        for kind in ('trades_v1', 'quotes_v1'):
            sk = 'us_stocks_sip/%s/%s/%s/%s.csv.gz' % (kind, dt[:4], dt[5:7], dt)
            try: h = src.head_object(Bucket='flatfiles', Key=sk); man['source'][kind] = {'key': sk, 'etag': h['ETag'].strip('"'), 'bytes': h['ContentLength'], 'last_modified': h['LastModified'].strftime('%Y-%m-%dT%H:%M:%SZ')}
            except Exception as e: man['source'][kind] = {'key': sk, 'error': type(e).__name__}
        rs = {}; cutdone = threading.Event(); th = None
        if rswant(dt):   # the raw subset is CUT beside the conversion (own thread, own database connection) and UPLOADED after it, overlapping the next day's download and conversion. Order kept by a chain: the thread of day N first waits for the thread of day N-1
            # (so at most two days of subset files are on the disk), cuts, signals, uploads, deletes its files. The main line waits only for the cut (it needs the raw files), never for the upload. A subset whose upload did not finish is found missing by the next run and the day is redone.
            prev = RS['last']
            def cut():
                try:
                    if prev: prev.join()
                    outs = []
                    for kind, fn in (('trades', tfn), ('quotes', qfn)):
                        o = os.path.join(work, '%s.rawsub-%s.parquet' % (dt, kind)); t1 = time.time(); nn = rawsub(fn, o, os.environ.get('TICK_RAWLIST') or types, work); rs[kind] = {'key': rskey(kind, dt), 'rows': int(nn), 'bytes': os.path.getsize(o), 'md5': fmd5(o), 'cut_sec': round(time.time() - t1)}; outs.append((o, kind))
                except BaseException as e: rs['error'] = '%s %s' % (type(e).__name__, e); cutdone.set(); return
                cutdone.set(); t2 = time.time()
                try:
                    for o, kind in outs: put(o, rsb, rskey(kind, dt), {'StorageClass': os.environ.get('TICK_RAWSUB_CLASS', 'DEEP_ARCHIVE')}); os.remove(o)
                    print('rawsub', dt, 'uploaded, GB %.2f, upload sec %d' % (sum(rs[k]['bytes'] for k in ('trades', 'quotes')) / 1e9, time.time() - t2), flush=True)
                except BaseException as e: print('rawsub', dt, 'UPLOAD FAILED (the day will be redone by the next run):', type(e).__name__, e, flush=True)
            th = threading.Thread(target=cut); RS['last'] = th; th.start()
        gb = (os.path.getsize(tfn) + os.path.getsize(qfn)) / 1e9; n = day(tfn, qfn, ofn, work, types, pfn if px else None, sfn if px else None)
        if th:
            cutdone.wait()
            if 'error' in rs: raise SystemExit('REFUSED: raw subset of %s failed: %s' % (dt, rs['error']))
            man['rawsub'] = rs
        if raw and not pxonly:
            rb, _, rp = raw[5:].partition('/')
            for kind, fn in (('trades', tfn), ('quotes', qfn)): boto3.client('s3').upload_file(fn, rb, '%s/%s/%s/%s.csv.gz' % (rp.rstrip('/'), kind, dt[:4], dt), ExtraArgs={'StorageClass': 'DEEP_ARCHIVE'}, Config=boto3.s3.transfer.TransferConfig(multipart_chunksize=256 * 1024 * 1024, max_concurrency=8))
        os.remove(tfn); os.remove(qfn); pmb = 0.0; smb = 0.0; tmb = os.path.getsize(ofn) / 1e6
        if pxonly and dst: put(ofn, bucket, prefix.rstrip('/') + '/' + key)   # (v9) a day converted before v9 has run-dependent quote-signed columns: its tk file is replaced too, BEFORE the side files (the s10 file marks the redo as finished)
        man['rows'] = int(n[0]); man['stocks'] = int(n[1]); man['ties'] = dict(TIES); man['options'] = {k: os.environ.get(k) for k in ('TICK_V11', 'TICK_TYPES', 'TICK_ORDER', 'TICK_PX', 'TICK_S10', 'TICK_RAWSUB', 'TICK_RAWSUB_DAYS', 'TICK_RAWLIST', 'TICK_RAWSUB_CLASS', 'TICK_PART', 'TICK_FIRST') if os.environ.get(k)}; man['build'] = BUILD
        for nm_, fn_ in (('tk', ofn), ('px', pfn), ('s10', sfn)):
            if os.path.exists(fn_): man.setdefault('columns', {})[nm_] = [r_[0] for r_ in duckdb.sql("DESCRIBE SELECT * FROM read_parquet('%s')" % fn_).fetchall()]
        for nm_, fn_ in (('tk', ofn), ('px', pfn), ('s10', sfn)):
            if os.path.exists(fn_): man['files'][nm_] = {'bytes': os.path.getsize(fn_), 'md5': fmd5(fn_)}
        if px:   # px, then s10, then the manifest, then tk: the tk file marks the day as done, the MANIFEST marks its side files as done by this converter version (v11; before v11 the s10 file was the mark)
            pmb = os.path.getsize(pfn) / 1e6; smb = os.path.getsize(sfn) / 1e6; put(pfn, pxb, pxp.rstrip('/') + '/' + key); os.remove(pfn); put(sfn, s1b, s1p.rstrip('/') + '/' + key); os.remove(sfn)
            # (v11c4) the ETag S3 gave each stored object goes into the manifest (a checksum of the stored bytes without a download). px and s10 always; tk when it was uploaded before the manifest (every redo of an existing day); on a first conversion tk is uploaded after the manifest and only its size is recorded.
            for nm_, b_, k_ in (('px', pxb, pxp.rstrip('/') + '/' + key), ('s10', s1b, s1p.rstrip('/') + '/' + key)) + ((('tk', bucket, prefix.rstrip('/') + '/' + key),) if pxonly and dst else ()):
                if nm_ in man['files']: man['files'][nm_]['etag'] = boto3.client('s3').head_object(Bucket=b_, Key=k_)['ETag'].strip('"')
            mfn = os.path.join(work, dt + '.manifest.json'); open(mfn, 'w').write(json.dumps(man, indent=1)); put(mfn, pxb, mkey(dt)); os.remove(mfn)
        if pxonly: os.remove(ofn)
        elif dst: put(ofn, bucket, prefix.rstrip('/') + '/' + key); os.remove(ofn)
        else: os.makedirs(os.path.dirname(os.path.join(out, key)), exist_ok=True); os.replace(ofn, os.path.join(out, key))
        print('redo' if pxonly else 'day', dt, '%d of %d' % (i + 1, total), 'raw GB %.1f' % gb, 'rows', n[0], 'stocks', n[1], 'sec', round(time.time() - t0), ('MB tk %.0f px %.0f s10 %.0f' % (tmb, pmb, smb)) if px else '', flush=True)
    def mkey(dt): return (pxp.rstrip('/')[:-2] + 'manifest' if pxp.rstrip('/').endswith('px') else pxp.rstrip('/') + '-manifest') + '/%s/%s.json' % (dt[:4], dt)
    def reload():   # TICK_RELOAD = path of a marker file: when it exists at a day boundary the script ends with code 75 and the wrapper starts the newest tested script
        if os.environ.get('TICK_RELOAD') and os.path.exists(os.environ['TICK_RELOAD']): print('reload asked, ending at the day boundary', flush=True); sys.exit(75)
    FAILS = {'row': 0}
    def safe1(i, total, dt, pxonly):
        # (v11c4) a refused or failed DAY never ends the process (the wrapper would fall back to the old v5 script without price files): it is logged, its raw files are removed and the queue goes on. After 2 such days in a row
        # nothing more is downloaded: a PAUSED line every minute until the reload marker appears, which ends the process with code 75 as usual.
        try: work1(i, total, dt, pxonly); FAILS['row'] = 0; return
        except SystemExit as e:
            if e.code in (0, 75): raise
            why = str(e.code)
        except Exception as e: why = '%s %s' % (type(e).__name__, e)
        print('DAY FAILED', dt, why.replace('\n', ' ')[:600], flush=True); FAILS['row'] += 1
        for x in ('-trades.csv.gz', '-quotes.csv.gz', '.parquet', '.px.parquet', '.s10.parquet', '.manifest.json'):
            try: os.remove(os.path.join(work, dt + x))
            except OSError: pass
        while FAILS['row'] >= 2:
            print('PAUSED', datetime.datetime.utcnow().strftime('%H:%M:%SZ'), '2 days failed in a row; nothing is downloaded; waiting for the reload marker', flush=True); reload(); time.sleep(60)
    for i, dt in enumerate(days):
        reload(); key = '%s/%s.parquet' % (dt[:4], dt)
        if dst:
            if has(bucket, prefix.rstrip('/') + '/' + key):
                why = ('no manifest' if not has(pxb, mkey(dt)) else manbad(dt) or ('raw subset missing' if rswant(dt) and not rsok(dt) else '')) if px else ''
                if why:   # v11c4: a day is done only when its manifest is the work of an accepted build with today's options and its files are the recorded ones; anything else is requeued
                    print('requeue', dt, '(%s)' % why, flush=True)
                    if dt >= REDO_NOW: safe1(i, len(days), dt, True)   # a finished day of the check periods gets its side files at once (the real-price replay needs them), older ones at the end of the queue
                    else: late.append(dt); print('skip', dt, '(side files at the end)', flush=True)
                else: print('skip', dt, flush=True)
                continue
        elif os.path.exists(os.path.join(out, key)): print('skip', dt, flush=True); continue
        safe1(i, len(days), dt, False)
    for i, dt in enumerate(late): reload(); safe1(i, len(late), dt, True)
    print('END', flush=True)

if __name__ == '__main__': main()

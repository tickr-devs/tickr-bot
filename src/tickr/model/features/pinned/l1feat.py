# Layer 1 FEATURE TABLE, one day (requirements text of 2026-11-23, section B; feature list: l1-feature-list-rulings-2026-11-23.md b9cf732e and the rulings of 05:36Z..05:39Z).
# ROWS. The rows are the rows of the day's label table, by construction: the table is built by the label builder's own call (l1labels: real_inputs, history, the pinned table code wm2_l1.day2 with the
#   declared replacements), from the same bar files, types and splits. The keys (sym, m) are then compared with the label table, in order; any difference refuses the day.
# FEATURES, 191 columns in ten named ablation groups (every group is tested with / without; nothing here decides what stays):
#   bars    41  wm.py FEAT without the three list columns          (pinned table code, unchanged)
#   lists    3  inl nin tofl                                        (the same reconstructed lists that define the cohort)
#   market   9, cross 4, hist 8   wm2.py NEW                        (pinned table code, unchanged)
#   company 15  wm2ref16.py = wm2ref.py 96a19cf3 (after the audit of 2026-11-20) with ONE rule added (Codex 061): a split factor over a span that holds a sealed-window day is missing, and so are
#               so_log, mcap, si_pct, si_chg where they need it; the label job's folder holds an older 13-column wm2ref.py that it does not use
#   tk      73  wm2tk.py e480668b on the 41 tk61 columns it reads   (pinned, formulas unchanged; ONE declared replacement of a column name, see TKPATCH: k_bigw5)
#   tk2     21  l1tk2.py: the 20 tk61 columns wm2tk never reads
#   bz      13  l1bz.py: Benzinga news and past earnings (paid group)
#   fl       4  l1fl.py: SEC public float (free group)
# TIME. A row of minute m uses data stamped not later than the decision mark of m = the END of minute m (bar m and tick minute m complete; the entry is at +5 s of m + 1): the label builder's convention.
#   Per group: bars / lists / market / cross / hist: minute bars of the day up to m and whole earlier days. company: records dated at least SO_LAG / SI_LAG days before the day, news at least NW_LAG minutes old.
#   tk, tk2: tick minutes of the day up to m. bz: articles known (published + bz_lag_s) at or before the mark, reports timed at or before it. fl: facts known (filed + fl_lag_days) on or before the day.
#   `python l1feat.py prefix ...` shows it on a real day: every input of the day cut after a minute, every feature of the rows up to that minute compared.
# SEALED DATES. No sealed file is listed or opened by this script (sealed bar days as HISTORY of later days are read by the pinned table code, by name, as in the label build). A value whose news window touches
#   a sealed date is missing: the bz group as l1bz.py says; the five nw_* columns of the company group when a sealed date lies in the three calendar days before the day or is the day (their 72-hour window).
#   The company reference files are made by `refprep` from OPEN-ONLY shards (refshard.py, run once where the mixed originals lie) and their cover file: a file that holds one row of a sealed
#   date counts as sealed and is never opened here. A shard is opened only after its name, its wholly open interval, its bytes and its md5 were checked against the cover; a row dated outside
#   its shard's interval refuses the run. NO exception (Codex 061): the splits and the SEC cover facts come from open-only shards too (refshard2b.py, cover2.json); the label builder's mixed
#   splits.jsonl is not fetched or opened by the feature build. The splits the table code applies are the open ones: the builder applies a split only on a day of its bar history, which never
#   holds a sealed day, so the rows are the label tables' rows (keys_equal refuses any day where they are not).
# USER INDEPENDENCE. The builder has no profile argument and no setting about a user: position size, dollar caps, loss limits, slots, account or trade history cannot reach it. The settings file may hold
#   only the keys of SETKEYS (an unknown key refuses), and the variables the table code reads refuse the run when set (l1labels.env_refuse).
# MISSING. A missing value stays missing (NaN); nothing is filled, clipped or defaulted. The record counts the missing values of every feature of the day.
# NO FITTED TRANSFORM. Nothing here is fitted on data: the only cross-sectional values are places among the rows of the same minute. Bins belong to the trainer.
# OUTPUT. <out>/<day>.parquet (date, sym as text, m, sess, excl-free: labels and exclusions stay in the label table) and <out>/<day>.json, the record, written LAST: bytes and md5 of every input and of the table,
#   md5 of every code file, the feature list by group, missing counts. A day is published only with its record.
# usage:
#   python l1feat.py refprep <reference source folder> <out folder>
#   python l1feat.py day <bar folder> <types file> <day> <tk dir> <px dir> <manifest dir> <early-close file> <prepared reference folder> <benzinga open folder> <fl-facts.csv> <settings.json> <label table> <out dir>
#   python l1feat.py prefix <the same 13 arguments> <HH:MM> [<HH:MM> ...]
#   python l1feat.py test
import sys, os, json, hashlib, shutil, tempfile, datetime, gzip, numpy as np
import l1labels as L, l1tk2, l1bz, l1fl

PIN = {'wm2ref16.py': '961333483dc8c06dd72a6de6142ff139'}   # the audited company reference code with the sealed-window rule for split factors, kept under its own name beside the label builder's pinned (older, unused) wm2ref.py
OWN = ('l1feat.py', 'l1tk2.py', 'l1bz.py', 'l1fl.py', 'l1labels.py', 'wm2ref16.py', 'wm2tk.py', 'wm2tk_l1.py', 'wm2.py', 'wm.py', 'wide.py')
LISTS = ['inl', 'nin', 'tofl']; NW = ['nw_n', 'nw_3d', 'nw_min', 'nw_any', 'nw_rk']; NW_DAYS = 3   # calendar days before the day that the 72-hour news window of wm2ref (H72) can reach
SETKEYS = l1bz.KEYS + l1fl.KEYS
COUNTS = {'bars': 41, 'lists': 3, 'market': 9, 'cross': 4, 'hist': 8, 'company': 15, 'tk': 73, 'tk2': 21, 'bz': 13, 'fl': 4}   # the pinned number of features of every group (191); another number refuses
# ONE DECLARED REPLACEMENT in the pinned wm2tk.py (spec ruling 05:48Z 2026-11-23), written to wm2tk_l1.py beside the builder, formulas untouched: wm2tk's column list holds the name k_big5 TWICE, as the
# window column "big" of 5 minutes (share of dollars in trades of 20,000+ dollars) and as the 5,000+ dollar column; its add() kept the second, so the 20,000+ share of 5 minutes was lost
# (every earlier tick sample had 72 columns). Here the 20,000+ window column of 5 minutes is named k_bigw5; k_big5 stays the 5,000+ one; the group has its 73 columns.
TKPATCH = (('the 20,000+ window column of 5 minutes gets its own name (list)', "TKCOLS = ['k_%s%d' % (c, w) for w in W for c in WCOLS]", "def _nm(c, w): return 'k_bigw5' if (c, w) == ('big', 5) else 'k_%s%d' % (c, w)\nTKCOLS = [_nm(c, w) for w in W for c in WCOLS]"),
           ('the 20,000+ window column of 5 minutes gets its own name (value)', "for c in WCOLS: out['k_%s%d' % (c, w)] = X[c][ri, rq]", "for c in WCOLS: out[_nm(c, w)] = X[c][ri, rq]"))
# THE SECOND DECLARED REPLACEMENT, in the pinned wm2.py as the label builder loads it (wm2_l1.py), written to wm2_l1f.py (spec ruling 06:09Z 2026-11-23): r_rv, the place by relative volume among all stocks of
# the minute. The pinned code gave relative volume 0 (not missing) to a stock with no trade yet today, but only when the stock had a bar LATER the same day (a stock is in the day's universe when the day's
# bar file holds it), so the place of every stock depended on the future. Measured on 2026-11-17 by the cut test, mean absolute difference of r_rv between the whole day and the day cut after a minute:
# 0.247 at 04:30, 0.008 at 09:45, 0.004 at 12:00, 0.0015 at 15:30 (the other 190 features equal). Here the place of minute m is taken among the stocks that have traded by the end of m (cumulative dollar
# volume of today above zero); a stock with no trade yet today has r_rv missing; nothing after m is read. rv20 (the relative volume itself) and every other column are untouched.
# Nothing in the label path reads r_rv: l1labels.py does not name it, and the cohort, vu and the rk label are built from other columns.
WM2PATCH = (('r_rv is the place among the stocks that have traded by the end of the minute', "r_rv = pd.DataFrame(RV).rank(axis=0, pct=True)", "r_rv = pd.DataFrame(np.where(DVa > 0, RV, np.nan)).rank(axis=0, pct=True)"),)
RRV_MEASURED = 'mean absolute difference of r_rv, whole day against the day cut after a minute, 2026-11-17, before the replacement: 04:30 0.247, 09:45 0.008, 12:00 0.004, 15:30 0.0015; the other 190 features equal'
COVER = 'cover.json'; COVER2 = 'cover2.json'; SPLITS = 'splits.jsonl'; REFKINDS = {'shares-outstanding-asof': 'date', 'short-interest': 'settlement_date'}; KINDS2 = ('splits', 'sec-cover-facts'); SECKEYS = ('c', 'u', 'form', 'val', 'end', 'filed', 'accn')   # the cover files of refshard.py and refshard2b.py; splits.jsonl here is the open-only file refprep joins from the splits shards; the fields a SEC fact may hold


def refuse(cause, text=''): raise SystemExit('REFUSED: %s%s' % (cause, (': ' + text) if text else ''))
def ident(p): return [os.path.getsize(p), L.md5(p)]


def settings(path):
    S = json.load(open(path)); keys = [k for k in S if not k.startswith('_')]
    for k in keys:
        if k not in SETKEYS: refuse('setting_unknown', k)
        if not str(S.get('_' + k, '')).strip(): refuse('setting_no_comment', k)
    for k in SETKEYS:
        if k not in S: refuse('setting_missing', k)
    l1bz.settings_ok(S); return S


def modules():
    here = os.path.dirname(os.path.abspath(__file__)); L.pins_ok(here, PIN)
    if L.md5(os.path.join(here, 'wm2tk.py')) != L.PIN['wm2tk.py']: refuse('table_code_md5', 'wm2tk.py')
    src = open(os.path.join(here, 'wm2tk.py')).read(); new = src
    for nm, old, rp in TKPATCH:
        if src.count(old) != 1 or new.count(old) != 1: refuse('table_code_replacement', 'the declared replacement "%s" does not apply exactly once' % nm)
        new = new.replace(old, rp)
    p = os.path.join(here, 'wm2tk_l1.py')
    if not os.path.isfile(p) or open(p).read() != new: open(p + '.part', 'w').write(new); os.replace(p + '.part', p)
    import wm2ref16, wm2tk_l1
    if open(wm2tk_l1.__file__).read() != new: refuse('table_code_replacement', 'wm2tk_l1.py is not the text that was written')
    return wm2ref16, wm2tk_l1


def load_wm2f():
    # the label builder's table code (its own declared replacements, its checks) plus WM2PATCH; the label builder's module wm2_l1 itself is not changed
    w0, wmd = L.load_wm2(); here = os.path.dirname(os.path.abspath(__file__)); src = open(os.path.join(here, 'wm2_l1.py')).read(); new = src
    if 'r_rv' in open(os.path.join(here, 'l1labels.py')).read(): refuse('label_path_reads_r_rv')
    for nm, old, rp in WM2PATCH:
        if src.count(old) != 1 or new.count(old) != 1: refuse('table_code_replacement', 'the declared replacement "%s" does not apply exactly once' % nm)
        new = new.replace(old, rp)
    p = os.path.join(here, 'wm2_l1f.py')
    if not os.path.isfile(p) or open(p).read() != new: open(p + '.part', 'w').write(new); os.replace(p + '.part', p)
    import wm2_l1f
    if open(wm2_l1f.__file__).read() != new: refuse('table_code_replacement', 'wm2_l1f.py is not the text that was written')
    if not hasattr(wm2_l1f, '_CAP'): wm2_l1f._CAP = {}
    wm2_l1f.load = L.bar_load; wm2_l1f._L1_READ_TYPES = L.read_types; L.settings_ok(wm2_l1f)
    return wm2_l1f, [wmd, L.md5b(new.encode('utf-8'))]


def groups(w, wm2ref16, wm2tk):
    G = {'bars': [c for c in w.FEAT if c not in LISTS], 'lists': list(LISTS)}; G.update({k: list(v) for k, v in w.GROUPS.items()})
    G.update({'company': list(wm2ref16.REFCOLS), 'tk': list(wm2tk.TKCOLS), 'tk2': list(l1tk2.TK2COLS), 'bz': list(l1bz.BZCOLS), 'fl': list(l1fl.FLCOLS)})
    cols = [c for g in G.values() for c in g]
    dup = sorted({c for c in cols if cols.count(c) > 1})
    if dup: refuse('feature_name_twice', ' '.join(dup))                                    # refusal level: every feature name is unique across ALL groups
    if {g: len(v) for g, v in G.items()} != COUNTS: refuse('feature_group_count', str({g: len(v) for g, v in G.items() if len(v) != COUNTS.get(g)}))
    if sorted(G['bars'] + G['lists'] + G['market'] + G['cross'] + G['hist']) != sorted(w.FEAT2): refuse('feature_list', 'the table code\'s list differs')
    return G, cols


# ---- the company reference folder: prepared once, open dates only ----
def shard_ok(src, nm, x, first=None):
    # a shard is opened only after: its name is its interval, the interval is wholly open, its bytes and md5 are the cover's
    kind, a, b = x['kind'], x['first'], x['last']; p = os.path.join(src, nm)
    if nm != '%s-%s_%s.jsonl' % (kind, a, b) or len(a) != 10 or len(b) != 10: refuse('reference_shard_name', nm)
    if a > b or l1bz.span_sealed(a, b) or (first and a < first): refuse('sealed_date', 'the interval of shard %s is not wholly open' % nm)
    if not os.path.isfile(p): refuse('reference_file_missing', nm)
    if ident(p) != [x['bytes'], x['md5']]: refuse('reference_shard_not_the_cover_s', nm)
    return kind, a, b, p


def refprep(src, out):
    # src holds ONLY open-only shards and the two cover files that name them (refshard.py: news, shares, short interest; refshard2b.py: splits, SEC cover facts). No mixed file is opened here:
    # a shard is read only after its name, its interval (wholly open) and its bytes and md5 were checked against its cover; a row dated outside its shard's interval refuses the run (never
    # dropped, never printed). The splits file of the prepared folder is the splits shards joined in name order: the open splits, and nothing else, reach the table code and the company columns.
    os.makedirs(out, exist_ok=True); man = {'what': 'layer 1 company reference, from open-only shards', 'source': {}, 'files': {}}; plain = [SPLITS]
    for f in (COVER, COVER2):
        if not os.path.isfile(os.path.join(src, f)): refuse('reference_file_missing', f)
        man['source'][f] = ident(os.path.join(src, f))
    cover = json.load(open(os.path.join(src, COVER))); shards = cover.get('shards') if isinstance(cover, dict) else None
    if not isinstance(shards, dict) or {x.get('kind') for x in shards.values()} != set(REFKINDS) | {'news'}: refuse('reference_cover', 'the cover file must name shards of news, shares-outstanding-asof and short-interest')
    cover2 = json.load(open(os.path.join(src, COVER2))); shards2 = cover2.get('shards') if isinstance(cover2, dict) else None
    if not isinstance(shards2, dict) or {x.get('kind') for x in shards2.values()} != set(KINDS2) or cover2.get('fact_fields') != list(SECKEYS): refuse('reference_cover', 'the second cover file must name shards of splits and sec-cover-facts, and the fact fields')
    if set(shards) & set(shards2): refuse('reference_cover', 'a shard named by both covers')
    seen = set(); months = {}; kept = 0; rows = {k: 0 for k in REFKINDS}; rows.update(splits=0, sec_facts=0, sec_company_lines=0)
    for nm, x in sorted(shards.items()):
        kind, a, b, p = shard_ok(src, nm, x, L.ARCH_FIRST if x.get('kind') == 'news' else None); man['source'][nm] = ident(p)
        for l in open(p):
            j = json.loads(l)
            if kind == 'news':
                if 'id' not in j or 'published_utc' not in j or j['id'] in seen: refuse('reference_news_row', 'a row of %s has no id or time, or an id seen before' % nm)
                seen.add(j['id']); d = datetime.datetime.fromisoformat(j['published_utc'].replace('Z', '+00:00')).astimezone(l1bz.NY).date().isoformat()
            else: d = j.get(REFKINDS[kind])
            if not isinstance(d, str) or not (a <= d <= b): refuse('row_outside_shard_interval', nm)
            if kind == 'news': months.setdefault(d[:7], []).append(json.dumps({'id': j['id'], 'published_utc': j['published_utc'], 'publisher': {'name': (j.get('publisher') or {}).get('name')}, 'tickers': j.get('tickers') or []}, sort_keys=True)); kept += 1
            else: rows[kind] += 1
        if kind != 'news': shutil.copyfile(p, os.path.join(out, nm + '.part')); os.replace(os.path.join(out, nm + '.part'), os.path.join(out, nm)); plain.append(nm)
    sp = b''; sec = []
    for nm, x in sorted(shards2.items()):
        kind, a, b, p = shard_ok(src, nm, x); man['source'][nm] = ident(p)
        if kind == 'splits':
            body = open(p, 'rb').read()
            for l in body.split(b'\n')[:-1]:
                d = json.loads(l).get('execution_date')
                if not isinstance(d, str) or not (a <= d <= b): refuse('row_outside_shard_interval', nm)
                rows['splits'] += 1
            if not body.endswith(b'\n') and body: refuse('reference_shard_form', nm)
            sp += body
        else:
            for l in open(p):
                j = json.loads(l)
                if set(j) != {'cik', 'facts'} or not j['facts']: refuse('reference_sec_row', 'a company line of %s holds another field, or no fact' % nm)
                for f in j['facts']:
                    if not set(f) <= set(SECKEYS): refuse('reference_sec_row', 'a fact of %s holds a field the float script does not read' % nm)
                    if not isinstance(f.get('filed'), str) or not (a <= f['filed'] <= b): refuse('row_outside_shard_interval', nm)
                    if not isinstance(f.get('end'), str) or L.sealed(f['end']) or f['end'] > f['filed']: refuse('sec_fact_not_open', 'a fact of %s has a period end that is not an open date, or later than its filing' % nm)
                    rows['sec_facts'] += 1
                rows['sec_company_lines'] += 1
            shutil.copyfile(p, os.path.join(out, nm + '.part')); os.replace(os.path.join(out, nm + '.part'), os.path.join(out, nm)); sec.append(nm)
    L.splits_parse(sp); open(os.path.join(out, SPLITS + '.part'), 'wb').write(sp); os.replace(os.path.join(out, SPLITS + '.part'), os.path.join(out, SPLITS))   # every line is a split record, or the run refuses
    for m, v in sorted(months.items()):
        p = os.path.join(out, 'news-%s.jsonl' % m); open(p + '.part', 'w').write('\n'.join(v) + '\n'); os.replace(p + '.part', p)
    for f in sorted(os.listdir(out)):
        if f != 'manifest.json' and not f.endswith('.part'): man['files'][f] = ident(os.path.join(out, f))
    man.update(plain=sorted(plain), sec=sorted(sec), news_kept=kept, months=len(months), rows=rows, shards=len(shards) + len(shards2), splits_md5=man['files'][SPLITS][1])
    open(os.path.join(out, 'manifest.json.part'), 'w').write(json.dumps(man, sort_keys=True)); os.replace(os.path.join(out, 'manifest.json.part'), os.path.join(out, 'manifest.json'))
    print('refprep: %d shards; %d news items in %d month files; shares rows %d, short interest rows %d; open splits %d (md5 %s); SEC facts %d in %d company lines; no row outside its shard; manifest md5 %s' % (len(shards) + len(shards2), kept, len(months), rows['shares-outstanding-asof'], rows['short-interest'], rows['splits'], man['splits_md5'], rows['sec_facts'], rows['sec_company_lines'], L.md5(os.path.join(out, 'manifest.json'))))


def open_splits(ref, src):
    # the md5 of the open-only splits file of the prepared reference folder; the splits file beside the bar folder (where the label builder's code looks for it) must be that file.
    # Returned as the pins for l1labels.real_inputs: the builder's own check then runs against the open file instead of its mixed one.
    mp = os.path.join(ref, 'manifest.json')
    if not os.path.isfile(mp): refuse('reference_manifest_missing')
    man = json.load(open(mp)); p = os.path.join(ref, SPLITS)
    if 'splits_md5' not in man or not os.path.isfile(p) or ident(p) != man['files'].get(SPLITS) or man['files'][SPLITS][1] != man['splits_md5']: refuse('reference_file_changed', SPLITS)
    if man['splits_md5'] == L.SPLITS_MD5: refuse('mixed_splits_file', 'the prepared splits file is the label builder\'s mixed one')
    return (L.TYPES_MD5, man['splits_md5'])


def ref_view(ref, day, wd):
    # a folder for wm2ref16.Ref with exactly: the plain files (splits, the shares and short-interest shards) and the news months that the day's 72-hour window can reach. Every file is checked against the manifest of refprep.
    mp = os.path.join(ref, 'manifest.json')
    if not os.path.isfile(mp): refuse('reference_manifest_missing')
    man = json.load(open(mp)); v = os.path.join(wd, 'ref-%s-%d' % (day, os.getpid())); os.makedirs(v); used = {}
    touch = l1bz.span_sealed(l1bz.day_add(day, -NW_DAYS), day); need = list(man['plain'])
    if not touch: need += sorted({'news-%s.jsonl' % l1bz.day_add(day, -k)[:7] for k in range(NW_DAYS + 1)})
    for f in need:
        p = os.path.join(ref, f)
        if f not in man['files'] or not os.path.isfile(p): refuse('reference_file_missing', f)
        if ident(p) != man['files'][f]: refuse('reference_file_changed', f)
        os.symlink(os.path.abspath(p), os.path.join(v, f)); used[f] = man['files'][f]
    return v, used, touch, L.md5(mp)


def assemble(t, pc, day, tkf, ref, bzdir, flf, S, wd, w, wm2ref16, wm2tk):
    # t = the day's table from the table code (sym, m, sess, the 65 bar features ...); pc = previous close of every row. -> frame of keys + features, feature groups, record parts
    import pandas as pd
    G, cols = groups(w, wm2ref16, wm2tk); cal = L.calendar(); f = pd.DataFrame({'sym': t['sym'].values, 'm': t['m'].values, 'chg': t['chg'].values}); rec = {}
    v, used, touch, mmd5 = ref_view(ref, day, wd)
    try: R = wm2ref16.Ref(v, (wm2ref16._day(L.SEAL[0]), wm2ref16._day(L.SEAL[1]))); R.add(f, day, np.asarray(pc, np.float64))
    finally: shutil.rmtree(v, ignore_errors=True)
    if touch:
        for c in NW: f[c] = np.float32(np.nan)
    rec['reference'] = {'manifest_md5': mmd5, 'files': used, 'news_window_touches_sealed': touch, 'age': R.stats}   # Codex 061: the age of the reference records used and what the sealed-window rule left missing, per day, for later live comparisons
    tv = os.path.join(wd, 'tk-%s-%d' % (day, os.getpid())); os.makedirs(os.path.join(tv, day[:4])); os.symlink(os.path.abspath(tkf), os.path.join(tv, day[:4], day + '.parquet'))
    try: wm2tk.Tick(tv).add(f, day)
    finally: shutil.rmtree(tv, ignore_errors=True)
    l1tk2.add(f, tkf)
    N = l1bz.News(bzdir, cal, day, S); N.add(f); E = l1bz.Earnings(bzdir, cal, day, S); E.add(f); rec['bz'] = {'news_window_touches_sealed': N.touch, 'articles_in_window': N.n, 'reports_without_time': E.skipped, 'reports_dropped_same_ticker_and_date': E.same_date, 'known_limit': l1bz.LIMIT}
    used = l1fl.Fl(flf).add(f, day); rec['fl'] = {'facts': ident(flf), 'rows_by_form_of_the_fact_used': used, 'known_limit': l1fl.LIMIT, 'fl_age_days': {'rows_with_a_fact': int((f['fl_has'].values == 1).sum()), 'median': (float(np.nanmedian(f['fl_age'].values)) if (f['fl_has'].values == 1).any() else None), 'max': (float(np.nanmax(f['fl_age'].values)) if (f['fl_has'].values == 1).any() else None)}}
    d = {'date': np.full(len(t), day), 'sym': t['sym'].values.astype(str), 'm': t['m'].values.astype(np.int16), 'sess': t['sess'].values.astype(np.int8)}; f = f.copy()
    for g, cs in G.items():
        src = t if g in ('bars', 'lists', 'market', 'cross', 'hist') else f
        for c in cs:
            if c not in d: d[c] = src[c].values.astype(np.float32)   # minute and session are keys and features at once: stored once, in their own integer type
    o = pd.DataFrame(d)
    if list(o.columns) != ['date', 'sym', 'm', 'sess'] + [c for c in cols if c not in ('m', 'sess')]: refuse('feature_columns')
    rec['missing'] = {c: int(np.isnan(o[c].values.astype(np.float64)).sum()) for c in cols}; rec['features'] = G; rec['feature_count'] = len(cols); rec['declared_replacements'] = {'wm2tk.py %s' % L.PIN['wm2tk.py']: [x[0] for x in TKPATCH] + ['earlier tick samples had 72 tick columns: the name k_big5 was held twice and the 20,000+ share of 5 minutes was lost'], 'wm2.py %s' % L.PIN['wm2.py']: [x[0] for x in WM2PATCH] + ['the pinned place counted a stock with no trade yet only when it traded later the same day: the place depended on the future', RRV_MEASURED]}; rec['rows'] = len(o)
    return o, rec


def keys_equal(o, labf):
    import duckdb
    k = duckdb.sql("SELECT sym, m FROM read_parquet('%s')" % labf).df()
    if len(k) != len(o) or (k['sym'].values.astype(str) != o['sym'].values).any() or (k['m'].values.astype(np.int64) != o['m'].values.astype(np.int64)).any(): refuse('rows_not_the_label_tables', 'label table %d rows, feature table %d' % (len(k), len(o)))


def table_of(src, types, day, w, ref):
    # the label builder's own table of the day (as l1labels.table), keeping the previous closes for the company group
    need, sp = L.real_inputs(src, types, day, pins=open_splits(ref, src)); tys = L.read_types(types); brec = L.bars_readable(src, need)
    (prev, mprev, hist), opened = L.history(src, tys, need, w.day2, sp); f = os.path.join(src, need[-1] + '.csv.gz'); opened.append(f)
    t = w.day2(f, prev, tys, hist, mprev, want=True)[0]; L.opened_ok(opened, need); L.rows_ok(t, day); L.files_same(brec, opened); t = t.reset_index(drop=True)
    return t, np.array([prev.get(s, np.nan) for s in t['sym'].values], np.float64), brec, sp


def write(o, rec, out, day):
    import duckdb
    os.makedirs(out, exist_ok=True); p = os.path.join(out, day + '.parquet'); r = os.path.join(out, day + '.json')
    for x in (p, r):
        if os.path.exists(x): refuse('output_exists', x)
    con = duckdb.connect(); con.register('o', o); con.execute("COPY (SELECT * FROM o) TO '%s' (FORMAT parquet, COMPRESSION zstd)" % (p + '.part')); con.close(); os.replace(p + '.part', p)
    rec['table'] = ident(p); rec['outcome'] = 'published'; open(r + '.part', 'w').write(json.dumps(rec, sort_keys=True)); os.replace(r + '.part', r)   # the record is written last


def day_(src, types, day, tkd, pxd, mand, earlyf, ref, bzdir, flf, setf, labf, out):
    L.env_refuse(); L.day_ok(day)
    if day in L.early_days(earlyf): refuse('early_close', day)
    S = settings(setf); wm2ref16, wm2tk = modules(); tkf, pxf, mff, mf, got = L.tick_inputs(day, tkd, pxd, mand); w, wmd = load_wm2f()
    t, pc, brec, sp = table_of(src, types, day, w, ref); wd = tempfile.mkdtemp(prefix='l1feat-')
    try: o, rec = assemble(t, pc, day, tkf, ref, bzdir, flf, S, wd, w, wm2ref16, wm2tk)
    finally: shutil.rmtree(wd, ignore_errors=True)
    keys_equal(o, labf)
    if os.path.getsize(tkf) != json.loads(open(mff).read())['files']['tk']['bytes'] if isinstance(mf['files'], dict) else False: refuse('tick_files_changed')
    if L.md5(tkf) != got['tk']: refuse('tick_files_changed', 'the tick file changed during its use')
    here = os.path.dirname(os.path.abspath(__file__))
    rec.update(day=day, what='layer 1 feature table', code={k: L.md5(os.path.join(here, k)) for k in OWN}, wm2_l1_md5=wmd[0], wm2_l1f_md5=wmd[1], settings=ident(setf), label_table=ident(labf),
               inputs={'bars': [list(x) for x in brec], 'tk': got['tk'], 'manifest': got['manifest'], 'types': L.md5b(L.raw(types)), 'splits': sp.digest})
    write(o, rec, out, day)
    print('feature table %s: %d rows, %d features in %d groups; missing cells %d; table md5 %s' % (day, rec['rows'], rec['feature_count'], len(rec['features']), sum(rec['missing'].values()), rec['table'][1]))


def same(a, b):
    a = np.asarray(a, np.float64); b = np.asarray(b, np.float64); return (a == b) | (np.isnan(a) & np.isnan(b))


def cut_inputs(day, c, tkf, bzdir, wd, ref=None):
    # the day's tick file without the minutes after c, the benzinga folder without what becomes known after the mark of c (reports: timed after it), and (ref given) the prepared reference
    # folder with the day's month of news cut at the same mark (the nw_* columns of the company group read it); the other reference files are dated by day and stay whole
    import duckdb
    tk2 = os.path.join(wd, 'tk-cut.parquet'); duckdb.sql("COPY (SELECT * FROM read_parquet('%s') WHERE m <= %d) TO '%s' (FORMAT parquet)" % (tkf, c + l1tk2.M0, tk2))
    bz2 = os.path.join(wd, 'bz-cut'); os.makedirs(bz2); mark = l1bz.ny_s(day, 4) + (c + 1) * 60
    for f in sorted(os.listdir(bzdir)):
        if not f.endswith('.jsonl.gz'): continue
        a, b = f[:-len('.jsonl.gz')].split('-', 1)[1].split('_')
        if not (a <= day <= b): os.symlink(os.path.abspath(os.path.join(bzdir, f)), os.path.join(bz2, f)); continue
        with gzip.open(os.path.join(bz2, f), 'wt') as g:
            for j in l1bz.rows(os.path.join(bzdir, f)):
                if f.startswith('news-'): late = l1bz.utc_s(j['published']) > mark
                else: late = bool(j.get('date') and j.get('time')) and l1bz.ny_s(j['date'], *(int(x) for x in j['time'].split(':'))) > mark
                if not late: g.write(json.dumps(j) + '\n')
    if ref is None: return tk2, bz2
    ref2 = os.path.join(wd, 'ref-cut'); os.makedirs(ref2); nf = 'news-%s.jsonl' % day[:7]; dropped = 0
    for f in sorted(os.listdir(ref)):
        if f == 'manifest.json': continue
        if f != nf: os.symlink(os.path.abspath(os.path.join(ref, f)), os.path.join(ref2, f)); continue
        with open(os.path.join(ref2, f), 'w') as g:
            for l in open(os.path.join(ref, f)):
                if l.strip() and l1bz.utc_s(json.loads(l)['published_utc']) > mark: dropped += 1
                else: g.write(l)
    man = json.load(open(os.path.join(ref, 'manifest.json')))   # the cut folder's own manifest: the cut month's identity, everything else as prepared
    if nf in man['files'] and os.path.isfile(os.path.join(ref2, nf)): man['files'][nf] = ident(os.path.join(ref2, nf))
    open(os.path.join(ref2, 'manifest.json'), 'w').write(json.dumps(man, sort_keys=True)); return tk2, bz2, ref2, dropped


def prefix(src, types, day, tkd, pxd, mand, earlyf, ref, bzdir, flf, setf, labf, out, *cuts):
    # the future-row test on a real day: every input of the day cut after a minute; every feature of every row up to that minute must be the same as from the whole day
    L.env_refuse(); S = settings(setf); wm2ref16, wm2tk = modules(); tkf = L.tick_inputs(day, tkd, pxd, mand)[0]; w, wmd = load_wm2f(); need, sp = L.real_inputs(src, types, day, pins=open_splits(ref, src)); tys = L.read_types(types)
    st, opened = L.history(src, tys, need, w.day2, sp); f = os.path.join(src, day + '.csv.gz'); G, cols = groups(w, wm2ref16, wm2tk); bad = 0
    def build(barf, tk_, bz_, wd, ref=ref):
        cap, keys, t = L.one_table(w, barf, st, tys); t = t.reset_index(drop=True); pc = np.array([st[0].get(s, np.nan) for s in t['sym'].values], np.float64)
        return assemble(t, pc, day, tk_, ref, bz_, flf, S, wd, w, wm2ref16, wm2tk)[0]
    print('inputs cut at the mark: minute bars, tick minutes, benzinga news and reports, reference news (nw_*). Whole-day by nature, dated by day and known before the session: shares, short interest, splits, float facts, types, history sessions')
    wd = tempfile.mkdtemp(prefix='l1feat-prefix-')
    try:
        full = build(f, tkf, bzdir, wd); print('feature prefix test %s: whole day %d rows, %d features' % (day, len(full), len(cols)))
        for hm in cuts:
            c = L.cut_minute(hm); cw = tempfile.mkdtemp(dir=wd); cb = os.path.join(cw, day + '.csv.gz'); L.cut_file(f, c, cb); tk2, bz2, ref2, nwd = cut_inputs(day, c, tkf, bzdir, cw, ref); cut = build(cb, tk2, bz2, cw, ref2); L.forget(cb)
            a = full[full.m <= c].reset_index(drop=True); b = cut[cut.m <= c].reset_index(drop=True); diff = {}
            if len(a) != len(b) or (a.sym.values != b.sym.values).any() or (a.m.values != b.m.values).any(): bad += 1; print('  cut %s (minute %d): ROWS DIFFER: %d against %d' % (hm, c, len(a), len(b))); continue
            for col in cols:
                n = int((~same(a[col].values, b[col].values)).sum())
                if n: diff[col] = n
            bad += len(diff); print('  cut %s (minute %d): rows up to the cut %d (after it %d in the whole day, %d in the cut one); reference news items cut away %d; features that differ: %s' % (hm, c, len(a), len(full) - len(a), len(cut) - len(b), nwd, diff or 'none'))
    finally: shutil.rmtree(wd, ignore_errors=True)
    print('FEATURE PREFIX TEST %s' % ('PASSED' if not bad else 'FAILED')); sys.exit(1 if bad else 0)


def test():
    import pandas as pd, duckdb, types as _types
    n = [0]
    def ck(c, t):
        n[0] += 1
        if not c: sys.exit('feat selftest failed: ' + t)
    def refuses(f, cause, t):
        try: f()
        except SystemExit as e: ck(str(e).startswith('REFUSED: ' + cause), '%s (got %s)' % (t, e)); return str(e)
        ck(False, t + ' (no refusal)')
    wm2ref16, wm2tk = modules(); import wm2 as w0
    w = _types.SimpleNamespace(FEAT=list(w0.FEAT), FEAT2=list(w0.FEAT2), GROUPS=w0.GROUPS)
    G, cols = groups(w, wm2ref16, wm2tk)
    ck([len(G[g]) for g in ('bars', 'lists', 'market', 'cross', 'hist', 'company', 'tk', 'tk2', 'bz', 'fl')] == [41, 3, 9, 4, 8, 15, 73, 21, 13, 4] and len(cols) == 191 and G['tk'].count('k_big5') == 1 and 'k_bigw5' in G['tk'], 'ten groups, 191 features: %s' % [len(v) for v in G.values()])
    ck('m' in G['bars'] and 'sess' in G['bars'] and G['lists'] == ['inl', 'nin', 'tofl'] and set(NW) <= set(G['company']), 'session and minute are features; the list columns are a group of their own')
    refuses(lambda: groups(_types.SimpleNamespace(FEAT=w.FEAT, FEAT2=w.FEAT2, GROUPS=w.GROUPS), wm2ref16, _types.SimpleNamespace(TKCOLS=list(wm2tk.TKCOLS[:-1]) + ['k_big5'])), 'feature_name_twice', 'a feature name twice in one group')
    refuses(lambda: groups(_types.SimpleNamespace(FEAT=w.FEAT, FEAT2=w.FEAT2, GROUPS=w.GROUPS), wm2ref16, _types.SimpleNamespace(TKCOLS=list(wm2tk.TKCOLS[:-1]) + ['mcap'])), 'feature_name_twice', 'a feature name twice across groups')
    refuses(lambda: groups(_types.SimpleNamespace(FEAT=w.FEAT, FEAT2=w.FEAT2, GROUPS=w.GROUPS), wm2ref16, _types.SimpleNamespace(TKCOLS=list(wm2tk.TKCOLS[:-1]))), 'feature_group_count', 'a group with another number of features')
    import wm2tk as _pinned; ck(len(_pinned.TKCOLS) == 73 and len(set(_pinned.TKCOLS)) == 72 and _pinned.TKCOLS.count('k_big5') == 2 and len(set(wm2tk.TKCOLS)) == 73 and [c for c in wm2tk.TKCOLS if c not in _pinned.TKCOLS] == ['k_bigw5'], 'the pinned wm2tk names k_big5 twice (72 distinct); the declared replacement gives 73, one new name')
    td = tempfile.mkdtemp()
    try:
        # the synthetic minute of the ruling: one trade of 6,000 dollars moves k_big5 (5,000+) and not k_bigw5 (20,000+)
        zr = {c: 0.0 for c in wm2tk.RAW}; zr.update(tn=1.0, tv=600.0, tdv=6000.0, tbig=0.0, tbig5=1.0, tbig100=0.0, tlast=10.0, tp45=10.0, qn=1.0, qbid=9.99, qask=10.01)
        zf = os.path.join(td, '2024'); os.makedirs(zf); duckdb.sql("COPY (SELECT * FROM (VALUES ('AAA', %d, %s)) v(sym, m, %s)) TO '%s' (FORMAT parquet)" % (240 + 400, ', '.join('%r::DOUBLE' % zr[c] for c in wm2tk.RAW), ', '.join(wm2tk.RAW), os.path.join(zf, '2024-03-06.parquet')))
        zt = pd.DataFrame({'sym': ['AAA', 'AAA'], 'm': [400, 404]}); wm2tk.Tick(td).add(zt, '2024-03-06'); ck(zt.k_big5.tolist() == [1.0, 1.0] and zt.k_bigw5.tolist() == [0.0, 0.0] and zt.k_big1[0] == 0 and zt.k_big100[0] == 0, 'a 6,000 dollar trade: k_big5 (5,000+) is 1, k_bigw5 (20,000+, 5 minutes) is 0')
        zp = pd.DataFrame({'sym': ['AAA', 'AAA'], 'm': [400, 404]}); _pinned.Tick(td).add(zp, '2024-03-06'); ck(all(same(zp[c].values, zt[c].values).all() for c in set(_pinned.TKCOLS)) and 'k_bigw5' not in zp, 'every column of the pinned code has the same value from the replaced one; only the lost column is new')
        shutil.rmtree(zf)
        # settings: comments, unknown keys (no profile can be passed), missing keys
        S0 = {'bz_lag_s': 60, 'bz_n60_s': 3600, 'bz_n1d_s': 86400, 'bz_n3d_s': 259200, 'bz_hist_days': 20, 'bz_rk_floor': 0.05, 'er_surp_floor': 0.01, 'er_gap_reports': 4, 'er_min_reports': 3, 'fl_lag_days': 3, 'fl_forms': ['10-K'],
              'bz_channels': {'earn': ['earnings'], 'anl': ['price target'], 'deal': ['fda'], 'mov': ['movers']}}
        def sf(d): p = os.path.join(td, 'set.json'); x = dict(d); x.update({'_' + k: 'what, unit, reader' for k in d}); open(p, 'w').write(json.dumps(x)); return p
        ck(settings(sf(S0))['bz_lag_s'] == 60, 'the settings file'); refuses(lambda: settings(sf(dict(S0, position_usd=1000))), 'setting_unknown', 'a user setting cannot be passed to the builder')
        refuses(lambda: settings(sf({k: v for k, v in S0.items() if k != 'fl_lag_days'})), 'setting_missing', 'a missing setting'); p = sf(S0); x = json.load(open(p)); del x['_bz_lag_s']; open(p, 'w').write(json.dumps(x)); refuses(lambda: settings(p), 'setting_no_comment', 'a setting without its comment')
        # reference news (nw_*) and the time of an item (Codex 060, point 4): wm2ref16 cuts the stamp to its minute a and counts the item only in rows of minute a + NW_LAG or later.
        # The mark of such a row is the END of its minute, so an item is never counted sooner than NW_LAG whole minutes after its true stamp: cutting only delays, it never lets an item in early.
        import wm2ref16
        nd = os.path.join(td, 'nwt'); os.makedirs(nd); open(os.path.join(nd, 'news-x.jsonl'), 'w').write('\n'.join(json.dumps({'id': i, 'published_utc': ts, 'publisher': {'name': 'GlobeNewswire Inc.'}, 'tickers': [k]}) for i, ts, k in (('p', '2024-03-06T15:00:59.900Z', 'LATE'), ('q', '2024-03-06T15:00:00Z', 'EQU'))) + '\n')
        nt = pd.DataFrame({'sym': ['LATE', 'LATE', 'EQU', 'EQU'], 'm': [361, 362, 361, 362], 'chg': [0.0] * 4}); wm2ref16.Ref(nd, (wm2ref16._day(L.SEAL[0]), wm2ref16._day(L.SEAL[1]))).add(nt, '2024-03-06', np.ones(4))   # m 361 = minute 10:01 (mark 10:02:00), m 362 = minute 10:02 (mark 10:03:00)
        ck(wm2ref16.NW_LAG == 2 and nt.nw_n.tolist() == [0.0, 1.0, 0.0, 1.0], 'reference news: an item stamped 10:00:59.9 or 10:00:00 is absent at the 10:02:00 mark and counts at the 10:03:00 mark (never under 120 s after its stamp)')
        shutil.rmtree(nd)
        # refprep reads open-only shards (made here by the frozen splitter refshard.py from mixed files): months, manifest, refusals; a view holds only the months of the window
        import refshard, io, contextlib
        mix = os.path.join(td, 'mixed'); os.makedirs(mix); src = os.path.join(td, 'refsrc'); ref = os.path.join(td, 'ref')
        open(os.path.join(mix, 'shares-outstanding-asof.jsonl'), 'w').write(json.dumps({'ticker': 'AAA', 'date': '2024-01-02', 'share_class_shares_outstanding': 1e6, 'weighted_shares_outstanding': 1e6, 'list_date': '2010-01-04', 'sic_code': '2834'}) + '\n' + json.dumps({'ticker': 'NA', 'date': '2024-01-02', 'share_class_shares_outstanding': 2e6, 'list_date': '2015-01-05', 'sic_code': '2834'}) + '\n' + json.dumps({'ticker': 'AAA', 'date': '2026-07-20', 'share_class_shares_outstanding': 9e6}) + '\n')
        open(os.path.join(mix, 'short-interest.jsonl'), 'w').write(json.dumps({'settlement_date': '2024-01-31', 'ticker': 'AAA', 'short_interest': 1e5, 'days_to_cover': 2.0}) + '\n' + json.dumps({'settlement_date': '2026-07-31', 'ticker': 'AAA', 'short_interest': 9e5, 'days_to_cover': 9.0}) + '\n')
        nw = lambda i, ts, tk, pub='GlobeNewswire Inc.': json.dumps({'id': i, 'published_utc': ts, 'publisher': {'name': pub}, 'tickers': tk, 'title': 'never kept'})
        open(os.path.join(mix, 'news-2021-11-16_2026-08-13.jsonl'), 'w').write('\n'.join([nw('a', '2024-03-06T14:00:00Z', ['AAA']), nw('b', '2024-03-05T20:00:00Z', ['AAA', 'NA']), nw('c', '2026-07-15T14:00:00Z', ['AAA']), nw('d', '2021-11-01T14:00:00Z', ['AAA']), nw('a', '2024-03-06T14:00:00Z', ['AAA'])]) + '\n')
        open(os.path.join(mix, 'news-2026-08-14_2026-11-14.jsonl'), 'w').write(nw('e', '2026-08-14T14:00:00Z', ['AAA']) + '\n' + nw('f', '2026-08-15T14:00:00Z', ['AAA']) + '\n' + nw('g', '2026-08-18T12:00:00Z', ['AAA']) + '\n')
        with contextlib.redirect_stdout(io.StringIO()): refshard.split(mix, src, os.path.join(td, 'notopen'))
        ck(sorted(os.listdir(src)) == ['cover.json', 'news-2024-03-01_2024-03-31.jsonl', 'news-2026-08-15_2026-08-31.jsonl', 'shares-outstanding-asof-1900-01-01_2026-06-30.jsonl', 'short-interest-1900-01-01_2026-06-30.jsonl'], 'the splitter\'s open folder: shards and the cover, nothing else')
        refuses(lambda: refprep(src, ref), 'reference_file_missing', 'no second cover file'); shutil.rmtree(ref, ignore_errors=True); keep = L.SPLITS_MD5
        # the second cover: splits and SEC cover facts, split by the frozen refshard2b.py from mixed files
        import refshard2b
        spl = lambda d, tk='AAA': json.dumps({'execution_date': d, 'ticker': tk, 'split_from': 1, 'split_to': 2})
        msp = os.path.join(mix, 'splits.jsonl'); open(msp, 'w').write('\n'.join([spl('2020-01-02'), spl('2026-07-10'), spl('2026-09-01', 'SPL'), spl('2027-01-13')]) + '\n')
        fct = lambda filed, end, **kw: dict({'c': 'EntityPublicFloat', 'u': 'USD', 'form': '10-K', 'val': 6e8, 'end': end, 'filed': filed, 'accn': 'a' + filed, 'frame': 'never kept'}, **kw)
        msc = os.path.join(mix, 'sec.jsonl'); open(msc, 'w').write(json.dumps({'cik': 1, 'name': 'never kept', 'facts': [fct('2024-02-20', '2023-06-30'), fct('2026-07-20', '2025-12-31'), fct('2026-09-20', '2026-07-31'), fct('2026-09-21', '2026-06-30')]}) + '\n')
        k2 = refshard2b.SPLITS_MD5; refshard2b.SPLITS_MD5 = L.md5(msp)
        try:
            with contextlib.redirect_stdout(io.StringIO()): refshard2b.split(msp, msc, os.path.join(td, 'open2'), os.path.join(td, 'notopen2'))
        finally: refshard2b.SPLITS_MD5 = k2
        for f_ in os.listdir(os.path.join(td, 'open2')): shutil.move(os.path.join(td, 'open2', f_), os.path.join(src, f_))
        S2 = ['sec-cover-facts-1900-01-01_2026-06-30.jsonl', 'sec-cover-facts-2026-08-15_2026-11-19.jsonl']; P2 = ['splits-1900-01-01_2026-06-30.jsonl', 'splits-2026-08-15_2026-11-19.jsonl']
        ck(sorted(f_ for f_ in os.listdir(src) if f_.startswith(('sec-', 'splits', 'cover2'))) == ['cover2.json'] + S2 + P2, 'the second splitter\'s open folder: two shards of each kind and the cover; no mixed splits file')
        try:
            open(os.path.join(src, 'news-2021-11-16_2026-08-13.jsonl'), 'w').write('a mixed file that lies in the folder is never opened: this line is not JSON\n')
            shutil.rmtree(ref, ignore_errors=True); refprep(src, ref); man = json.load(open(os.path.join(ref, 'manifest.json'))); PL = ['shares-outstanding-asof-1900-01-01_2026-06-30.jsonl', 'short-interest-1900-01-01_2026-06-30.jsonl', 'splits.jsonl']
            ck(man['news_kept'] == 4 and man['plain'] == PL and man['rows'] == {'shares-outstanding-asof': 2, 'short-interest': 1, 'splits': 2, 'sec_facts': 2, 'sec_company_lines': 2} and sorted(f for f in man['files'] if f.startswith('news')) == ['news-2024-03.jsonl', 'news-2026-08.jsonl'], 'refprep: four open items in two month files, the open shares and short-interest rows, two open splits, two open SEC facts; the mixed file was not opened')
            ck(man['sec'] == S2 and open(os.path.join(ref, SPLITS)).read() == spl('2020-01-02') + '\n' + spl('2026-09-01', 'SPL') + '\n' and man['splits_md5'] == L.md5(os.path.join(ref, SPLITS)) and man['shards'] == 8, 'the prepared splits file is the open splits shards joined in name order; the SEC shards are copied')
            ck(all(x not in open(os.path.join(ref, f)).read() for f in os.listdir(ref) for x in ('2026-07-10', '2027-01-13', '2026-07-20', '2026-07-31', 'never kept')), 'no split of the window or after the last open day, no fact filed or dated in the window, and no other field is in the prepared folder')
            # the builder's own splits check runs against the open file: pins from the manifest; the mixed pinned file is refused by name of its md5
            bsrc = os.path.join(td, 'bars', 'minute_aggs'); ck(open_splits(ref, bsrc) == (L.TYPES_MD5, man['splits_md5']), 'the pins for the label builder\'s input check: its types pin and the open splits file')
            L.SPLITS_MD5 = man['splits_md5']; refuses(lambda: open_splits(ref, bsrc), 'mixed_splits_file', 'a prepared splits file that is the builder\'s mixed one is refused'); L.SPLITS_MD5 = keep
            open(os.path.join(ref, SPLITS), 'a').write(spl('2026-07-10') + '\n'); refuses(lambda: open_splits(ref, bsrc), 'reference_file_changed', 'a splits file that is not the manifest\'s'); shutil.rmtree(ref); refprep(src, ref)
            # the second cover's refusals
            cp2 = os.path.join(src, 'cover2.json'); c20 = open(cp2).read(); s20 = open(os.path.join(src, S2[1])).read(); p20 = open(os.path.join(src, P2[1])).read()
            def cov2(f, top=None):
                c = json.loads(c20); f(c['shards']); (top or (lambda c: None))(c); open(cp2, 'w').write(json.dumps(c))
            def again2(cause, t): shutil.rmtree(ref, ignore_errors=True); refuses(lambda: refprep(src, ref), cause, t)
            fix = lambda nm: (lambda sh: sh[nm].update(bytes=os.path.getsize(os.path.join(src, nm)), md5=L.md5(os.path.join(src, nm))))
            open(os.path.join(src, P2[1]), 'a').write(spl('2026-07-10') + '\n'); again2('reference_shard_not_the_cover_s', 'a splits shard whose bytes are not the cover\'s'); cov2(fix(P2[1])); again2('row_outside_shard_interval', 'a split of the sealed window inside a shard: refused, not dropped'); open(os.path.join(src, P2[1]), 'w').write(p20)
            def sec_with(**kw): open(os.path.join(src, S2[1]), 'w').write(json.dumps({'cik': 1, 'facts': [dict({k_: v_ for k_, v_ in fct('2026-09-21', '2026-06-30').items() if k_ != 'frame'}, **kw)]}) + '\n'); cov2(fix(S2[1]))
            sec_with(filed='2026-07-20'); again2('row_outside_shard_interval', 'a fact filed in the sealed window inside a shard'); sec_with(end='2026-07-31'); again2('sec_fact_not_open', 'a fact whose period end lies in the sealed window'); sec_with(end='2026-09-22'); again2('sec_fact_not_open', 'a period end after its filing')
            sec_with(frame='x'); again2('reference_sec_row', 'a fact with a field the float script does not read'); open(os.path.join(src, S2[1]), 'w').write(json.dumps({'cik': 1, 'name': 'x', 'facts': [{k_: v_ for k_, v_ in fct('2026-09-21', '2026-06-30').items() if k_ != 'frame'}]}) + '\n'); cov2(fix(S2[1])); again2('reference_sec_row', 'a company line with another field')
            open(os.path.join(src, S2[1]), 'w').write(s20); cov2(lambda sh: None, lambda c: c.update(fact_fields=list(SECKEYS) + ['frame'])); again2('reference_cover', 'a second cover that names other fact fields'); cov2(lambda sh: sh.pop(P2[0]) and sh.pop(P2[1])); again2('reference_cover', 'a second cover without the splits')
            open(cp2, 'w').write(c20); shutil.rmtree(ref, ignore_errors=True); refprep(src, ref)
            # wm2ref16: a factor over the sealed window is missing (Codex 061)
            rv = os.path.join(td, 'rv'); os.makedirs(rv); jl = lambda f_, rws: open(os.path.join(rv, f_), 'w').write(''.join(json.dumps(r_) + '\n' for r_ in rws))
            jl('splits.jsonl', [{'execution_date': '2026-06-10', 'ticker': 'OLD', 'split_from': 1, 'split_to': 2}])
            jl('shares-outstanding-asof-1900-01-01_2026-06-30.jsonl', [{'ticker': 'OLD', 'date': '2026-06-01', 'share_class_shares_outstanding': 1e6, 'list_date': '2010-01-04', 'sic_code': '2834'}, {'ticker': 'NEW', 'date': '2026-06-01', 'share_class_shares_outstanding': 1e6, 'sic_code': '2834'}])
            jl('shares-outstanding-asof-2026-08-15_2026-11-19.jsonl', [{'ticker': 'NEW', 'date': '2026-08-20', 'share_class_shares_outstanding': 3e6, 'sic_code': '2834'}])
            jl('short-interest-1900-01-01_2026-06-30.jsonl', [{'settlement_date': '2026-06-15', 'ticker': x_, 'short_interest': 1e5, 'days_to_cover': 2.0} for x_ in ('OLD', 'NEW')] + [{'settlement_date': '2026-06-30', 'ticker': x_, 'short_interest': 2e5, 'days_to_cover': 3.0} for x_ in ('OLD', 'NEW')])
            jl('short-interest-2026-08-15_2026-11-19.jsonl', [{'settlement_date': '2026-08-31', 'ticker': 'NEW', 'short_interest': 3e5, 'days_to_cover': 4.0}, {'settlement_date': '2026-09-15', 'ticker': 'NEW', 'short_interest': 6e5, 'days_to_cover': 5.0}])
            cl = (wm2ref16._day(L.SEAL[0]), wm2ref16._day(L.SEAL[1])); RR = wm2ref16.Ref(rv, cl); mk = lambda: pd.DataFrame({'sym': ['OLD', 'NEW', 'OLD'], 'm': [359, 359, 360], 'chg': [0.0, 0.0, 0.0]}); pc3 = np.array([10.0, 10.0, 10.0])
            q = mk(); RR.add(q, '2026-06-30', pc3); ck(abs(q.so_log[0] - np.log10(2e6)) < 1e-6 and abs(q.mcap[0] - np.log10(2e7)) < 1e-6 and abs(q.si_pct[0] - 1e5 / 2e6) < 1e-9 and sum(RR.stats['cells_missing_because_a_split_factor_would_cross_the_sealed_window'].values()) == 0, 'before the window nothing changes: the open split of 06-10 is applied to the shares record of 06-01 (not to the settlement of 06-15, taken after it)')
            q = mk(); RR.add(q, '2026-09-10', pc3); st_ = RR.stats; cut_ = st_['cells_missing_because_a_split_factor_would_cross_the_sealed_window']
            ck(q.so_log.isna()[[0, 2]].all() and q.mcap.isna()[[0, 2]].all() and q.si_pct.isna()[[0, 2]].all() and q.sic2[0] == 28 and q.age[0] == q.age[0] and q.si_dtc[0] == 3.0, 'after the window, a shares record and a settlement from before it: so_log, mcap and si_pct missing (the splits of the window are not known); age, sic2 and si_dtc stay')
            ck(abs(q.so_log[1] - np.log10(3e6)) < 1e-6 and q.si_pct.isna()[1] and abs(q.si_chg[1] - 1.0) < 1e-6 and q.si_dtc[1] == 3.0, 'a shares record after the window with a settlement from before it (the one of 08-31 is not yet 14 days old): so_log known, si_pct missing; si_chg of two settlements on the same side is known')
            ck(cut_ == {'so_log': 2, 'mcap': 2, 'si_pct': 3, 'si_chg': 0} and st_['shares_record']['rows_with_a_record'] == 3 and st_['shares_record']['age_days_max'] == wm2ref16._day('2026-09-10') - wm2ref16._day('2026-06-01') and st_['shares_record']['age_days_min'] == 21 and st_['short_interest_settlement']['age_days_median'] == 72.0, 'the day\'s numbers: cells the rule left missing per column, age of the records used')
            q = mk(); RR.add(q, '2026-09-14', pc3); ck(abs(q.si_pct[1] - 3e5 / 3e6) < 1e-9 and q.si_chg.isna()[1] and RR.stats['cells_missing_because_a_split_factor_would_cross_the_sealed_window']['si_chg'] == 1, 'the first settlement after the window: si_pct known, si_chg missing (the settlement before it lies on the other side)')
            q = mk(); RR.add(q, '2026-09-29', pc3); ck(abs(q.si_chg[1] - 1.0) < 1e-9 and abs(q.si_pct[1] - 6e5 / 3e6) < 1e-9, 'two settlements after the window: si_chg known again')
            ck(RR.adj('OLD', cl[0] - 2, cl[0] - 1) == 1.0 and RR.adj('OLD', cl[0] - 1, cl[0]) != RR.adj('OLD', cl[0] - 1, cl[0]) and RR.adj('OLD', cl[1] - 1, cl[1] + 5) != RR.adj('OLD', cl[1] - 1, cl[1] + 5) and RR.adj('OLD', cl[1], cl[1] + 5) == 1.0, 'the edges: a span is unknown exactly when it holds a day of the window')
            shutil.rmtree(rv)
            ck(all(x not in open(os.path.join(ref, f)).read() for f in os.listdir(ref) for x in ('2026-07-15', '2026-07-20', '2026-07-31', 'never kept')), 'no sealed row and no title is in the prepared folder')
            os.remove(os.path.join(src, 'news-2021-11-16_2026-08-13.jsonl')); cp = os.path.join(src, 'cover.json'); c0 = open(cp).read(); sn = 'shares-outstanding-asof-1900-01-01_2026-06-30.jsonl'; s0 = open(os.path.join(src, sn)).read()
            def cov(f):
                c = json.loads(c0); f(c['shards']); open(cp, 'w').write(json.dumps(c))
            def again(cause, t): shutil.rmtree(ref, ignore_errors=True); refuses(lambda: refprep(src, ref), cause, t)
            open(os.path.join(src, sn), 'a').write(json.dumps({'ticker': 'AAA', 'date': '2026-07-20'}) + '\n'); again('reference_shard_not_the_cover_s', 'a shard whose bytes are not the cover\'s')
            cov(lambda sh: sh[sn].update(bytes=os.path.getsize(os.path.join(src, sn)), md5=L.md5(os.path.join(src, sn)))); again('row_outside_shard_interval', 'a sealed row inside a shard, the cover agreeing with the bytes: refused, not dropped')
            open(os.path.join(src, sn), 'w').write(s0); bad = 'shares-outstanding-asof-1900-01-01_2026-07-01.jsonl'; os.rename(os.path.join(src, sn), os.path.join(src, bad))
            cov(lambda sh: sh.update({bad: dict(sh.pop(sn), last='2026-07-01')})); again('sealed_date', 'a shard whose interval holds a sealed day is refused before it is opened')
            cov(lambda sh: sh.update({bad: dict(sh.pop(sn))})); again('reference_shard_name', 'a shard whose name is not its interval'); os.rename(os.path.join(src, bad), os.path.join(src, sn))
            cov(lambda sh: sh.pop('short-interest-1900-01-01_2026-06-30.jsonl')); again('reference_cover', 'a cover without one of the three kinds'); open(cp, 'w').write(c0); shutil.rmtree(ref, ignore_errors=True); refprep(src, ref)
            v, used, touch, mm = ref_view(ref, '2024-03-06', td); ck(not touch and sorted(os.listdir(v)) == sorted(PL + ['news-2024-03.jsonl']), 'the view of a day: the plain files and the month of its window'); shutil.rmtree(v)
            v, used, touch, mm = ref_view(ref, '2026-08-17', td); ck(touch and sorted(os.listdir(v)) == PL, '2026-08-17: the window touches 2026-08-14, no news file is opened'); shutil.rmtree(v)
            v, used, touch, mm = ref_view(ref, '2026-08-18', td); ck(not touch and 'news-2026-08.jsonl' in os.listdir(v), '2026-08-18: the window is clear'); shutil.rmtree(v)
            refuses(lambda: ref_view(ref, '2024-04-02', td), 'reference_file_missing', 'a month of the window without its file'); open(os.path.join(ref, 'news-2024-03.jsonl'), 'a').write('\n'); refuses(lambda: ref_view(ref, '2024-03-06', td), 'reference_file_changed', 'a file that is not the manifest\'s'); shutil.rmtree(ref); refprep(src, ref)
            for x in os.listdir(td):
                if x.startswith('ref-'): shutil.rmtree(os.path.join(td, x))
            # assemble on a hand-made table: columns, order, types, missing counts, groups from the right source
            D = '2024-03-06'; rows = [('AAA', 359, 1), ('NA', 359, 1), ('AAA', 360, 1), ('NA', 360, 1), ('ZZZ', 360, 1)]; t = pd.DataFrame({'sym': [r[0] for r in rows], 'm': np.array([r[1] for r in rows], np.int16), 'sess': np.array([r[2] for r in rows], np.int8)})
            for k, c in enumerate(w.FEAT2):
                if c not in ('m', 'sess'): t[c] = np.float32(k) + np.arange(len(t), dtype=np.float32) / 10
            t['old_label'] = 9.0; tkf = os.path.join(td, 'tk.parquet'); R = wm2tk.RAW; allc = sorted(set(R) | set(l1tk2.RAW))
            tr = [(s, m + 240, {c: float(1 + i + j) for j, c in enumerate(allc)}) for i, (s, m) in enumerate((('AAA', 358), ('AAA', 359), ('AAA', 360), ('NA', 359), ('NA', 360), ('AAA', 361)))]
            def tkw(rws, p): duckdb.sql("COPY (SELECT * FROM (VALUES %s) v(sym, m, %s)) TO '%s' (FORMAT parquet)" % (', '.join("('%s', %d, %s)" % (s, m, ', '.join('%r::DOUBLE' % r[c] for c in allc)) for s, m, r in rws), ', '.join(allc), p))
            tkw(tr, tkf); bz = os.path.join(td, 'bz'); os.makedirs(bz)
            def gz(name, rws):
                with gzip.open(os.path.join(bz, name), 'wt') as g: g.write(''.join(json.dumps(r) + '\n' for r in rws))
            gz('news-2024-02-01_2024-02-29.jsonl.gz', []); gz('news-2024-03-01_2024-03-31.jsonl.gz', [{'benzinga_id': '1', 'published': '2024-03-06T14:30:00Z', 'tickers': ['NA'], 'channels': ['movers']}, {'benzinga_id': '2', 'published': '2024-03-06T15:00:30Z', 'tickers': ['NA'], 'channels': ['fda']}])
            gz('earnings-2024-01-01_2024-12-31.jsonl.gz', [{'benzinga_id': 'e1', 'ticker': 'NA', 'date': '2024-03-05', 'time': '16:05:00', 'last_updated': '2024-03-05T21:06:00Z', 'actual_eps': 0.3, 'estimated_eps': 0.2}])
            flf = os.path.join(td, 'fl.csv'); open(flf, 'w').write(','.join(l1fl.HEAD) + '\nNA,1,2023-06-30,2024-02-20,2024-02-23,600000000.0,0.5,1,10-K\n')
            pc = np.array([10.0, 20.0, 10.0, 20.0, 5.0]); o, rec = assemble(t, pc, D, tkf, ref, bz, flf, S0, td, w, wm2ref16, wm2tk)
            ck(list(o.columns) == ['date', 'sym', 'm', 'sess'] + [c for c in cols if c not in ('m', 'sess')] and len(o.columns) == 193 and len(o) == 5, 'the table: four keys (minute and session are also features) and the other 189 features in group order')
            ck(all(str(o[c].dtype) == 'float32' for c in cols if c not in ('m', 'sess')) and str(o.m.dtype) == 'int16' and str(o.sess.dtype) == 'int8' and o.sym.tolist()[1] == 'NA' and 'old_label' not in o, 'types pinned; the stock NA is text; no label column is copied')
            ck(all((o[c].values == t[c].values.astype(np.float32)).all() for g in ('bars', 'lists', 'market', 'cross', 'hist') for c in G[g]), 'the 65 bar features are the table code\'s values, untouched')
            a = o.set_index(['sym', 'm']); ck(abs(a.loc[('NA', 359), 'mcap'] - np.log10(2e6 * 20.0)) < 1e-5 and a.loc[('AAA', 359), 'nw_n'] == 1 and a.loc[('NA', 359), 'nw_n'] == 0 and a.loc[('NA', 359), 'nw_3d'] == 1 and a.loc[('NA', 360), 'sic2'] == 28, 'company group: shares x the previous close of the row; news of the window')
            ck(a.loc[('NA', 359), 'bz_n60'] == 1 and a.loc[('NA', 360), 'bz_n60'] == 1 and a.loc[('NA', 359), 'bz_c_mov'] == 1 and a.loc[('NA', 360), 'bz_c_deal'] == 0 and a.loc[('NA', 359), 'er_since'] == 1 and abs(a.loc[('NA', 359), 'er_surp'] - 0.5) < 1e-6, 'bz group: the article of 10:00:30 (known 10:01:30) is in neither row; yesterday\'s report')
            ck(a.loc[('NA', 359), 'fl_has'] == 1 and a.loc[('AAA', 359), 'fl_has'] == 0 and abs(a.loc[('NA', 360), 'fl_share'] - 0.5) < 1e-6 and a.loc[('NA', 360), 'fl_age'] == 12, 'fl group')
            x = tr[1][2]; ck(abs(a.loc[('AAA', 359), 'k2_open'] - (x['topen'] / x['tlast'] - 1)) < 1e-6 and np.isnan(a.loc[('ZZZ', 360), 'k2_open']) and np.isfinite(a.loc[('AAA', 359), 'k_tn5']), 'tick groups: tk2 at the row\'s own minute; a stock without ticks is missing')
            ck(rec['rows'] == 5 and rec['feature_count'] == 191 and rec['missing']['k2_open'] == 1 and rec['missing']['m'] == 0 and rec['missing']['fl_share'] == 3 and set(rec['missing']) == set(cols) and rec['features'] == G, 'the record: missing cells per feature, the groups')
            ck(not [x for x in os.listdir(td) if x.startswith(('ref-', 'tk-'))], 'the views of the reference and the tick file are removed')
            # a future row changes nothing: tick minutes after the row, an article and a report after the mark
            tkw(tr[:2] + tr[3:4], os.path.join(td, 'tkc.parquet')); bzc = cut_inputs(D, 359, tkf, bz, td)[1]; o2, _ = assemble(t.iloc[:2].reset_index(drop=True), pc[:2], D, os.path.join(td, 'tkc.parquet'), ref, bzc, flf, S0, td, w, wm2ref16, wm2tk)
            ck(all(same(o[c].values[:2], o2[c].values).all() for c in cols if c not in G['cross'] + ['nw_rk', 'sx_n', 'sx_med', 'sx_chg'] and not c.startswith('kr_')), 'rows of minute 359 are the same without the tick minutes, the article and the rows after their mark')
            ck(len(list(l1bz.rows(os.path.join(bzc, 'news-2024-03-01_2024-03-31.jsonl.gz')))) == 1, 'the cut benzinga folder holds only what was published by the mark')
            rx = os.path.join(td, 'refx'); cx = os.path.join(td, 'cutx'); os.makedirs(rx); os.makedirs(cx); open(os.path.join(rx, 'news-%s.jsonl' % D[:7]), 'w').write(nw('x1', D + 'T15:00:00Z', ['NA']) + '\n' + nw('x2', D + 'T15:00:01Z', ['NA']) + '\n'); open(os.path.join(rx, 'news-2024-02.jsonl'), 'w').write(nw('x0', '2024-02-27T15:00:00Z', ['NA']) + '\n'); open(os.path.join(rx, 'manifest.json'), 'w').write(json.dumps({'files': {f_: ident(os.path.join(rx, f_)) for f_ in os.listdir(rx)}}))
            r2, nd = cut_inputs(D, 359, tkf, bz, cx, rx)[2:]; kept = [json.loads(l)['id'] for l in open(os.path.join(r2, 'news-%s.jsonl' % D[:7]))]
            ck(nd == 1 and kept == ['x1'] and json.load(open(os.path.join(r2, 'manifest.json')))['files']['news-%s.jsonl' % D[:7]] == ident(os.path.join(r2, 'news-%s.jsonl' % D[:7])) and json.load(open(os.path.join(r2, 'manifest.json')))['files']['news-2024-02.jsonl'] == ident(os.path.join(rx, 'news-2024-02.jsonl')) and os.path.islink(os.path.join(r2, 'news-2024-02.jsonl')) and not os.path.islink(os.path.join(r2, 'news-%s.jsonl' % D[:7])), 'the cut reference folder: the day\'s month of news without the item published after the mark of minute 359 (10:00:00 New York); other files as they are')
            shutil.rmtree(rx); shutil.rmtree(cx)
            # 2026-08-17: every news value missing, in both news groups
            gz('news-2026-08-15_2026-08-31.jsonl.gz', []); gz('earnings-2026-08-15_2026-11-19.jsonl.gz', []); gz('earnings-2025-01-01_2025-12-31.jsonl.gz', []); gz('earnings-2026-01-01_2026-06-30.jsonl.gz', [])
            o3, r3 = assemble(t, pc, '2026-08-17', tkf, ref, bz, flf, S0, td, w, wm2ref16, wm2tk); ck(all(o3[c].isna().all() for c in NW + l1bz.BZCOLS) and r3['reference']['news_window_touches_sealed'] and r3['bz']['news_window_touches_sealed'] and r3['missing']['nw_n'] == 5 and o3['sic2'].notna().any(), 'a row of 2026-08-17: every nw_* and bz column missing and counted; the company columns that need no split factor stay')
            ck(o3['mcap'].isna().all() and o3['so_log'].isna().all() and r3['reference']['age']['cells_missing_because_a_split_factor_would_cross_the_sealed_window']['mcap'] == 4 and r3['reference']['age']['shares_record']['rows_with_a_record'] == 4 and rec['reference']['age']['cells_missing_because_a_split_factor_would_cross_the_sealed_window']['mcap'] == 0 and rec['fl']['fl_age_days'] == {'rows_with_a_fact': 2, 'median': 12.0, 'max': 12.0}, '2026-08-17 with shares records from 2024: mcap and so_log missing, counted in the record with the age of the records; the 2024 day has none')
            # the keys against the label table
            lab = os.path.join(td, 'lab.parquet'); duckdb.sql("COPY (SELECT * FROM (VALUES %s) v(sym, m)) TO '%s' (FORMAT parquet)" % (', '.join("('%s', %d::SMALLINT)" % (s, m) for s, m, _ in rows), lab)); keys_equal(o, lab)
            refuses(lambda: keys_equal(o.iloc[:4], lab), 'rows_not_the_label_tables', 'a row less'); refuses(lambda: keys_equal(o.iloc[[1, 0, 2, 3, 4]], lab), 'rows_not_the_label_tables', 'another order')
            out = os.path.join(td, 'out'); r0 = dict(rec); write(o, r0, out, D); j = json.load(open(os.path.join(out, D + '.json'))); b = duckdb.sql("SELECT * FROM read_parquet('%s')" % os.path.join(out, D + '.parquet')).df()
            ck(j['outcome'] == 'published' and j['table'] == ident(os.path.join(out, D + '.parquet')) and list(b.columns) == list(o.columns) and b.sym.tolist() == o.sym.tolist() and same(b['k2_open'].values, o['k2_open'].values).all() and str(b['bz_n60'].dtype) == 'float32', 'the table and its record; the stored table reads back equal')
            refuses(lambda: write(o, dict(rec), out, D), 'output_exists', 'a day is not written twice')
        finally: L.SPLITS_MD5 = keep
    finally: shutil.rmtree(td, ignore_errors=True)
    print('feat selftest passed: %d checks' % n[0])


if __name__ == '__main__':
    a = sys.argv[1:]
    if a == ['test']: test()
    elif a[:1] == ['refprep'] and len(a) == 3: refprep(*a[1:])
    elif a[:1] == ['day'] and len(a) == 14: day_(*a[1:])
    elif a[:1] == ['prefix'] and len(a) >= 15: prefix(*a[1:])
    else: sys.exit('usage: python l1feat.py refprep <reference source> <out> | day <13 arguments, see the head of the file> | prefix <the same 13> <HH:MM> ... | test')

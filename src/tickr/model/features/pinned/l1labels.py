# Layer 1, first set: the five labels at executable prices (h30e, d30e, t10e, b30e with pu30e / pl30e, rk30e).
# Definitions: l1-first-set-labels-2026-11-20.md (ACCEPTED, md5 ff3294712b490b12f4d54b0f1ccc0c7e, not edited) WITH its errata l1-first-set-labels-errata-2026-11-21.md (items A, B, C, F, J, K, N; listed at the end of this header); prices as in r1-pre-run-record.md sections 1, 2, 4, 6a. Research only; nothing here is read by the bot.
# usage: python l1labels.py <minute_aggs dir> <types.csv> <day> <tk dir> <px dir> <manifest dir> <early-close days file> <out.parquet>   |   python l1labels.py test   |   python l1labels.py coverage <minute_aggs dir or file of names> <early-close days file> <first day> <last day>   |   python l1labels.py calcheck <explained-list or -> <minute_aggs dir or inventory list> <first day> <last day> ...   |   python l1labels.py prefix <minute_aggs dir> <types.csv> <day> <HH:MM> ...   |   python l1labels.py keydiff <minute_aggs dir> <types.csv> <day> <old.parquet> <new.parquet> <out.csv> <old rule: v2 or v3>
#   Rows: see erratum B below (the lines of this paragraph and of the 'readings' list are kept as first written; where an erratum changes one, the erratum governs).
#   Rows are the rows of the wm2 table of that day (wm2.days2, before any sample). tk dir / px dir / manifest dir hold DAY.parquet / DAY.json; a day is read only when its manifest names the frozen build and the two sizes match.
#   Refused, never guessed: a sealed day, a day after HARD_LAST, an early-close day (the table has no early-close sessions; listed for the spec thread), a missing or foreign manifest.
# Minute index k counts from 04:00 New York (09:30 = 330), as in wide.py; tk / px store the minute of the day (k + 240).
# Readings of the text that this code fixes (told to the spec thread before the first build):
#   - a decided sale (upper, time, t10e, h30e) follows the SALE rule literally: 5 s and 15 s of the minute after the decision, then 5 s of each later minute to the session's last minute;
#     a stop (lower limit) follows section 4: 0, 2, 5, 15 s of the next minute, then 5 s of each later minute, plus 15 s of the flatten minute (section 2 takes over there);
#   - inventory mark: "a bid" is the test r1pilot uses for its unliquidated valuation (r1pilot.py sale(): `if q['bid'] > 0: p['lastbid'] = (q['bid'], k, S)`), any state; marks read from a crossed quote (state 5) are counted in the printout.
#     It is the last bid above zero seen at an attempted mark; if none, the last bid above zero of the quotes known at the minute closes from the entry minute to the decision; its age = time of the mark where it was read minus its SIP time;
#   - d30e in an entry minute without a px row = the standing fillable bid (reader rule); a px minute whose stored low (pblo5 / pblof) is empty gives no bid for that minute;
#   - stop_during_exit = a trade low at or under L in any minute after the decision up to and including the fill minute (to the session end when nothing filled);
#   - b30e_opt counts a trade high at or over U in any window minute, the entry minute included whatever its second (it is a bound), the lower limit first inside a minute;
#   - upper_codes_unknown = the quote known at a window close has a displayed bid at or over U (state 1, 2 or 4, size above zero) but no codes;
#   - half-spread band of a row = (ask - bid) / (ask + bid) of the quote known at T (bid and ask above zero, bid <= ask), else band "none";
#   - an order-unknown low of the entry minute is LOWER in the label (unk_no False). The two other readings (those rows excluded; those rows taken as not a stop, unk_no True) are second passes for the printout, never stored as the label;
#   - bid_under_L_at_entry and spread_over_u are set only when the entry mark has a bid above zero; rows that enter with no bid are counted apart (ebid);
#   - a code token that cannot be parsed refuses the day.
# Added after the first build (2026-11-17, builder d52d4c6b) was read, by the spec thread's note of 15:45Z; no label value changed (every label column of the rebuild equal to the first table d4d73475):
#   a. stored columns tq_ask, tq_bid = ask and bid of the quote known at T, and a printed table of entered rows whose entry price A is over that ask by more than 1, 2, 5, 10% with their part of the h30e sum
#      (the live entry is a limit inside an entry window; with these columns a capped-entry variant can be derived without a rebuild);
#   b. fixture: bid_under_L_at_entry and spread_over_u are one condition, the two bits are equal; printed once;
#   c. printed count of rows where b30e_opt is under b30e (an upper exit sold above U), and the stress coverage line;
#   d. counts beside shares on the rare lines.
# ERRATA of 2026-11-21 (Codex review codex-025 / codex-026 of builder 281141a7, accepted by the spec thread 18:18Z). Changed against 281141a7:
#   A. No sealed file is opened, history included. Before the first open the builder lists the bar files it needs (the target and the HIST trading-day files before it; the previous trading day is the last of them) and refuses the day with
#      status history_in_sealed_window when the span from the first of them to the target touches the sealed window or passes HARD_LAST. No short-history variant: targets 2026-08-17..2026-09-14 are refused (embargo) until S1 is opened.
#      The table code (wide.py, wm.py, wm2.py, wm2ref.py, wm2tk.py) is loaded from the builder's folder with pinned md5; a mismatch refuses. The 45-calendar-day warm-up of wm2.days2 is NOT used: only those HIST + 1 files are opened.
#   B. Row set = wm2's R (the stock was on a list at or before q in this session and has a bar at q) with m <= flatten minute - 2 and NO next-bar condition. It is made by ONE declared replacement in the pinned wm2.py source
#      (ROWPATCH below, written to wm2_l1.py beside the builder). old_* columns are empty where the old label has none; old values on old rows are unchanged.
#   C. Inventory mark = the LATEST observation with a bid above zero among all px marks (0, 2, 5, 15 s) and the quotes known at minute closes, after the entry fill mark and not after the session's last liquidation attempt,
#      stored with minute, mark, SIP time, age and state. (Replaces the narrower reading of the list above.)
#   D. b_stress_src: 0 no stress low in the trigger minute, 1 present and its order after the trigger known, 2 present, order unknown. Coverage counts 1 only. ("Stress order from trade buckets" is parked by name until the pilot's raw-tick stop audit is read.)
#   E. stop_during_exit: a trade low at or under L in a minute after the decision and before the fill minute; in the fill minute no flag only when the low equals L exactly and its first second (xlos) is after the fill second,
#      a low under L or an unknown second = flag. With no fill the window ends at the last liquidation attempt, same proof rule in its minute.
#   F. bid_under_L_at_entry and spread_over_u are set by ONE comparison (entry bid above zero and <= L).
#   G. A minute of a table stock where tk has a quote update or a trade and px has no row refuses the day before anything is written. Carry-forward only for minutes with neither.
#   H. no_entry rows store the mark (the 5 s mark of minute m+1), the state and both code strings of the refusing quote (e_qi, e_qc, e_src).
#   I. b30e tables print observed fills beside marked-included with coverage; stress against primary on the same rows.
#   J. From the flatten minute every open position follows the session's liquidation attempts: 5 s and 15 s there, then 5 s of each later minute. So a decided sale also tries 15 s of the flatten minute (as stops and r1pilot do).
#   K. b30e_opt is the "optimistic variant", never "a bound"; max(b30e, b30e_opt) is the hindsight maximum of two constructed outcomes. b_opt_src: 0 none, 2 a later window minute, 1 the entry minute alone and its high first printed
#      at or after second 5 (xhis; proven after the entry mark by the same convention as xlos), 3 the entry minute alone, not proven.
#   L. Entry-cap table: denominators = entered rows WITH an ask at T; a snapshot comparison, not a limit-order replay.
#   M. The manifest's day and the md5 of the tk and px files are checked and printed. A row before the day's first in-grid quote has no tq_ask / tq_bid / band.
#   N. Scorer unchanged (wm2eval.py): select1() below is its predicate, with the tie fixtures of codex-026.
#   O. `coverage` lists refused dates by reason.
#   P. The carry starts BEFORE 04:00: the stock's last tk row with a quote update before 04:00 of the same day, with its terminal state and codes, stands in the grid until the first in-grid update, under the same tests as any standing quote;
#      its SIP time gives the age. (r1pilot reads minutes from 04:00 only: "pilot carry starts at 04:00" is a named limit of the pilot, stated with its result; the frozen pilot is not touched.)
# ERRATA, THIRD EDITION (21:00Z 2026-11-21, section "After the second read"; Codex's read codex-027 / codex-028 of builder 5561d4e3). Changed against 5561d4e3:
#   E1a. The files a day needs come from a trading-day calendar (trading-days.txt beside the builder, pinned by md5; weekdays minus the full-day closures of exchange-closures.txt), never from the files present.
#        Order, all before any bar file is opened: sealed target; not_a_trading_day; the fixed embargo from the calendar alone (the first HIST sessions after the sealed window = history_in_sealed_window whatever the folder holds);
#        the HIST calendar sessions before the target are the history: reaching before ARCH_FIRST = history_before_archive_start (so the first HIST sessions of the archive are not targets), a sealed one = history_in_sealed_window,
#        one without a bar file = bar_file_missing with its date (the target too). `coverage` walks the calendar. `calcheck` prints, names only, both differences between the calendar and the bar-file names.
#   E2b. Second declared replacement in the pinned table code (LISTPATCH, three places, each must apply exactly once): a list at minute m ranks only names with a bar today at or before m (gain / loss: a known change; Most Active:
#        cumulative volume above zero), order by the value, ties by ticker ascending (the tickers of the day stand in ascending order, a stable sort keeps it; the builder refuses a day whose tickers are not ASCII and ascending).
#        A third declared replacement (CAPPATCH) changes no value: it hands the list membership matrices of the day to the prefix test. `prefix` and the fixtures check: the table from the day's bars cut after minute c has, for every
#        minute up to c, the same list memberships and row keys as the table of the whole day.
#   E6a. _stop is NOT changed: a stop triggered in the minute before the flatten keeps section 4 (0, 2, 5, 15 s of the flatten minute, then 5 s of each later minute). Three fixtures; printed: sales pending on entering the flatten
#        minute and those filled at its second 15; stops triggered in the minute before the flatten by fill mark.
#   E9a. A day is refused before anything is written when the chosen quote update before 04:00 has no px row (seed_gaps). The comparison with a run whose carry starts at 04:00 covers every stored column (chg_p bits below).
#   Run rules: the builder refuses when WM_TOPN, WM_LAG, WM_REF, WM_TICK or WM_TSPAN is set (each asked by its name), prints the effective settings of the table code, opens exactly the bar files checked before the build, and names
#        its inputs separately in the build record.
# ERRATA, FOURTH EDITION (21:51Z 2026-11-21, section "After the third read"; Codex's read codex-029 / codex-030 of builder e6f36ed3). Changed against e6f36ed3:
#   E2c. The Most Active list is made in EVERY minute from the names with cumulative volume above zero; only the gain and loss lists wait for a known change (the inherited guard skipped the whole minute).
#   E1b. In a folder a bar file is a regular file named exactly YYYY-MM-DD.csv.gz; any other name is listed apart and never stands for a date. Bare dates only in an explicit inventory list. `calcheck` reconciles the dates against
#        DECLARED bounds and ends with an error on a difference that its explained-list does not name. The early-close list is pinned by md5 like the calendar.
#   The table code hands the prefix test each component list at both depths (_K: gain, loss, Most Active, post-market gain, post-market loss); `prefix` compares every component, the two unions and the row keys.
#   `keydiff` names a list as a reason only when the membership of that component changed at the watch start; its old rule is the table code of builder 5561d4e3 (v2) or of e6f36ed3 (v3), rebuilt and checked by md5.
#   The fixtures no longer need the platform's unstable sort to add and to remove keys: both directions are made with controlled masks; every actual difference against the old sort must be explained, whatever its direction.
#   E1c (version 5). Every input of a build is present and pinned, or the run ends in a typed refusal before any data file is opened. build, prefix and keydiff take the bar source only as a FOLDER (bar_source_not_a_folder; an
#        inventory list serves coverage and calcheck alone); the types file and the splits file are required and pinned by md5 (types_file_missing, splits_file_missing, types_file_md5, splits_file_md5; no "-", no "absent, continuing");
#        the tick inputs: manifest_missing, manifest_unreadable (not JSON, or without date, script_md5, files.tk / files.px with bytes and md5), tick_file_missing, price_file_missing. The fixtures call the inner functions.
#   E1c, second part (version 6). Unreadable inputs are typed refusals naming the file: types_file_unreadable (no ticker column, not a csv), early_close_file_missing, bar_file_unreadable (a needed bar file that is empty or not a
#        gzip csv with the columns BARCOLS; all the day's bar files are looked at, first line only, before the table code opens any). day_malformed: the day is YYYY-MM-DD and a real date (20261117 was refused as sealed before).
#        Output: output_folder_missing and output_exists are refused before the build starts; the table is written under a temporary name in the same folder and renamed when complete.
#   E1c, third part (version 6). bar_file_wrong_day: the first data row of every checked bar file, and every row of the builder's own second read of the target, must carry the file's New York date (the table code takes only
#        the minute from window_start). The build record gives bytes and md5 of each bar file read. calendar_file_missing, table_code_missing. cut_malformed (a prefix cut is HH:MM inside 04:00..19:59), bound_malformed
#        (bounds of calcheck and coverage: two days YYYY-MM-DD, first not after last).
#   Version 9 (errata 'E1e, additions of 00:32Z', errata file md5 ab485a8d32c3e3acc6aa9d8de1cc340d; counted on 2026-11-17 first: every pst value of the px file is null, 0, 6 or 11..16; table stocks without a tk row: 0):
#     a state is judged by its base digit (pst modulo 10), as derive reads it: quote_state_missing and terminal_state_missing test the base digit; quote_state_unknown: a state below zero or outside
#     0, 1..6, 11..16 on any state column of any row. tick_symbol_missing: a table stock with no row at all in the tick file (the converter keeps only the tickers of its types file).
#     The "table ready" record holds day, builder md5, ABSOLUTE output path, bytes, md5 of the temporary file and the claim token; `published <day> <table> <build log>` wants all of them equal and the
#     record's builder md5 equal to the md5 of the builder that runs the check.
#   Version 8 (errata E1e, errata file md5 96d6f9d8627d402c65ed922ae7ae1252; the rows rules 1a and 1b refuse were counted on 2026-11-17 over all rows before coding: 0 each):
#     1a quote_state_missing: a mark with no state (null or 0) and a bid or ask above zero. 1b for every key of both files whose tk row has a quote update (qn > 0): terminal_state_missing (pstE null or 0),
#        terminal_before_minute (pqtE before the start of its minute). An empty mark, the terminal of a minute without an update and a mark carrying an earlier minute stay valid.
#     2a bar_stamp_off_minute: window_start of every row is an exact minute start (bar_load). 2c the target is read ONCE: the minute closes of the labels are the close matrix the table code captured from its
#        read (closes()); files_same runs after that last bar read.
#     3a a claim whose token cannot be written is output_not_writable and the attempt removes the claim it has just created (or says that it stays). 3b the final name is checked again right after the claim.
#     4a ONE PROTOCOL PER OUTPUT FOLDER: every writer of the folder is this builder; a file put there by anything else is outside the guarantee. 4b before the rename one record line "table ready" with bytes
#        and md5 of the temporary file and the claim token, then standard output is flushed (report_not_written); the rename is the commit. 4c mode `published <table> <build log>`: the table's bytes and md5
#        equal a "table ready" record of this output in the log, else published_unverified; a claim left beside it is reported, never removed.
#   Version 7 (errata 'E1d, additions', errata file md5 b7237e4887b9c7633bcdcdee9ef925a1; real types read on 2026-11-17: m INTEGER in tk and px, pqt0/2/5/15/E BIGINT): a quote with a state and no time is refused (quote_time_missing); m of both tick files and every pqt column must be stored as integers; names, days and cuts are matched whole (fullmatch, digits 0-9);
#     bar_load refuses a bar column that is not numeric or a window_start that is not an integer; a claim that cannot be created is output_not_writable; bytes and md5 of a table are taken before the rename.
#   E1d (version 6): what is inside an input is checked on its first authorized read, and a table is published once.
#     1. bar_load replaces the pinned wide.load inside the table code (a declared replacement, same frame): in the one read that feeds the table, for every history file and the target, every row carries the file's
#        New York date (bar_file_wrong_day) and (ticker, minute) is unique (bar_key_duplicate), before the minute index is used.
#     2. tick_keys, over ALL rows of the tk and the px file (before 04:00 too): readable with the required columns (tick_/price_file_unreadable), keys not null and the minute an integer of the day before any cast
#        (tick_/price_key_invalid), (sym, m) unique (tick_/price_key_duplicate). A minute without a row stays valid.
#     3. quote times of the px file: pqt<S> not after minute start + S s (quote_after_mark), pqtE of minute m before the start of m+1 (quote_at_or_after_boundary), every quote time on the day's New York date
#        (quote_wrong_day; tested before quote_after_mark, so a stamp of the next day reports the wrong day). No quote = state 0 or null with time null or 0: kept as it is. A state that says a
#        quote exists (not null, not 0) with no time cannot be checked against its mark: quote_time_missing (version 7).
#     4. publication: claim() takes <out>.claim with exclusive create before anything is computed (output_claimed); publish() writes to a temporary name of its own, reads it back (output_verify_failed), runs the
#        report, checks the claim and the final name again (output_exists) and renames last; the claim is released in finally; a claim left by a killed attempt is never removed by the builder.
# REFUSAL CODES, the word after "REFUSED: " (every refusal has one): table_variable_set, day_malformed, early_close_file_missing, early_close_file_md5, bar_source_not_a_folder, calendar_file_missing, calendar_file_md5,
#   calendar_file_unreadable, sealed_day, not_a_trading_day, history_in_sealed_window, history_before_archive_start, bar_file_missing, types_file_missing, splits_file_missing, types_file_md5, splits_file_md5, splits_file_unreadable, splits_not_captured, early_close,
#   manifest_missing, manifest_unreadable, tick_file_missing, price_file_missing, manifest_wrong_day, manifest_mismatch, types_file_unreadable, types_ticker_empty, bar_file_unreadable, bar_file_wrong_day, output_folder_missing, output_exists,
#   bar_key_duplicate, tick_file_unreadable, price_file_unreadable, tick_key_invalid, price_key_invalid, tick_key_duplicate, price_key_duplicate, quote_time_missing, quote_state_unknown, tick_symbol_missing, quote_state_missing, terminal_state_missing, terminal_before_minute, bar_stamp_off_minute, closes_ticker_missing, report_not_written, quote_wrong_day, quote_after_mark, quote_at_or_after_boundary,
#   output_claimed, output_not_writable, output_verify_failed,
#   table_code_missing, table_code_md5, table_code_replacement, table_code_settings, code_files_changed, tick_files_changed, bar_files_opened_differ, tickers_not_ascending, no_table_rows, price_row_missing, early_quote_price_row_missing, code_token_unreadable;
#   E1f, 2 (version 10): `published <day> <table> <build log> <early-close days file>` first decides whether the day may be a target at all (target_ok: day_malformed, sealed_day, not_a_trading_day,
#        history_in_sealed_window, history_before_archive_start, early_close; reads only the calendar and the early-close list), then finds its record in the log, and only then touches the table. No label code changed.
#   E1g (version 10): a ticker is text in every reader of the build (read_types: nothing converted to a missing value; types_ticker_empty refuses an empty ticker); the table code's own types read is replaced
#        by the builder's (TYPEPATCH). Adds the stock NA, which the default reader had dropped from every table.
#   E1g, point 4 (version 11): keydiff's reference side = types minus the tickers the default reader loses (computed, lost_ticker), own history, captures aligned by ticker; old rule `own` = this builder's table code.
#        New reasons: a stock-session whose list place at its old first listed minute was taken by a lost ticker (ONE swap per differing component, set equality); the added keys of a lost ticker. No label code changed.
#   E1h (version 12, from Codex's letters 039 and 040): the readers, no label-computing function changed (the 2026-11-17 table stays 97be1dfe byte for byte).
#        1 one buffer per file (raw): the types file, every bar file, the calendar and the early-close list are read once per process; md5 and parsing use that buffer. published reads its log once and takes
#          bytes and md5 of the candidate from one read. 2 a NUL byte or a double quote in the types file or in the unpacked text of a bar file is refused before the csv reader (types_file_bytes, bar_file_bytes):
#          the reader rewrote NA\0X and "N"A to NA and 1\00 to 1. 3 bar_value_invalid: every numeric field of a bar file finite, open, close, high, low above 0, volume 0 or more; ticker and minute named
#          (inf, 1e999, 0 and -5 passed). 4 published_log_is_candidate: a log that is the candidate itself is refused.
#   E1i (version 13, from Codex's letter 044): no input is used from bytes other than the bytes whose digest is checked and reported. No label-computing function changed (2026-11-17 stays 97be1dfe).
#        1 splits.jsonl: captured ONCE (real_inputs); the pin is checked on those bytes, the adjustment is parsed from those bytes, the build record reports their digest. A file changed after the capture changes nothing.
#        2 the target's manifest: one capture; parsed from it, its digest in the record is of it.
#        3 tk and px of the target (the engine reads them by path, so one buffer is not possible): bytes and md5 are taken again after the last read; a difference refuses (tick_files_changed); the record says so.
#        4 table code (wide, wm, wm2, wm2ref, wm2tk, the written wm2_l1.py, the old-rule modules of keydiff): pins checked before the import as before; bytes and md5 of every file are taken again after the
#          modules are loaded and again at the end of the run, before a table is published; a difference refuses (code_files_changed). The record names both readings.
#        5 published: a "table ready" record ends at LF, CRLF or a bare CR (universal line ends on the captured bytes); the output-path field holds neither CR nor LF.
#        6 published: file identity of log and candidate (same file under any two names, hard links too) is tested BEFORE any content of the log is read; target_ok (sealed day first) stays before it.
#        NAMED LIMITS, not closed: (a) the builder's own md5 in the records is of its file on disk, not of the code in memory; (b) numba keeps compiled code of the table functions beside the code, keyed by
#          the file's time and size, and Python may load a module from its compiled copy: neither is tied to the bytes; (c) points 3 and 4 do not see a file that was changed and put back between the two readings.
#   E1j (version 14, from Codex's letter 048): no label-computing function changed (2026-11-17 stays 97be1dfe).
#        1 published: the log holds exactly ONE "table ready" record; more than one is refused (log_record_repeated, the log checker's word), also when one of them is the table's, and before any
#          content of the table is read.
#        2 splits: real_inputs returns the verified capture as an object (factors parsed from the checked bytes, path, digest). history, table and the build record take THAT object; nothing looks the
#          file up again by name, so a name that is re-pointed after the check changes nothing. A table asked for without the object is refused (splits_not_captured). The reader for a folder without
#          a check (splits_fixture) is for the fixtures only. A line that is not a split record is a typed refusal (splits_file_unreadable; unreachable with the pinned bytes).
#        3 the build record's digest of the early-close list is that of the capture early_days verified, not a new hash of the path. Looked for in every record line: no other digest of an already
#          captured file is taken from a path (the builder's own md5 stays a named limit).
#   other modes: published_unverified, log_record_repeated, cut_malformed, bound_malformed, old_rule_md5, old_rule_unknown.
# px rows without a tk row (seen at 16:10, 18:30, 19:00, 20:00) carry only official-close and limit columns with the standing marks; they are not a defect.
import sys, os, re, stat, gzip, json, math, glob, time, socket, hashlib, bisect, collections, datetime, zoneinfo, numpy as np, numba as nb

NM = 960; OFF = 240; FROZEN = 'c1c1ead1751ba765f494af79235bddd7'; HARD_LAST = '2026-11-19'; SEAL = ('2026-07-01', '2026-08-14')
FL = np.array([328, 710, 955], np.int64); SL = np.array([329, 719, 959], np.int64)   # flatten minute of a session (wide.py SESS) and its last minute (last liquidation attempt at second 5 of it)
BIDBAD = {2, 4, 8}; ASKBAD = {3, 4, 7}; MARKS = (0, 2, 5, 15); HOR = 30; TRAIL = 0.10; UP = 2.0; DN = 1.0; RKMIN = 50; HIST = 20
PIN = {'wide.py': '4527914daae6119fab5a4d01163bf480', 'wm.py': '366fb5ac2bc3aea498f56b30b7257b5a', 'wm2.py': 'a345cd2139fbabdcebd8971cd676ccf5', 'wm2ref.py': '8c2ce78eb813ddf060141b4e4d3b6200', 'wm2tk.py': 'e480668bb0b49819d4226eb85f26c749'}
ROWPATCH = ('g = ~np.isnan(lab[:, 0]);', 'g0_ = ~np.isnan(lab[:, 0]); g = rq <= rf - 2; lab[~g0_, 2] = rq[~g0_];')   # erratum B: keep every R row up to flatten - 2; rows without an old label get nq = q so the old code indexes a valid bar (their old_* values are blanked)
CALF = 'trading-days.txt'; CAL_MD5 = '3c22591622d1f06ce1611f9fcf5c702d'; ARCH_FIRST = '2021-11-16'; EARLY_MD5 = 'a0b77a4e86e27c4c178a3cf0522bf5e3'; TYPES_MD5 = 'fd1b9f017178a8edb6d2daf6234016fc'; SPLITS_MD5 = '3862ea7ccd18954e61f6b5ea8cab9f82'   # (the early-close list is pinned too) the pinned trading-day calendar; the documented first day of the open bar archive (the folders 2019-11-01 .. 2021-11-15 are closed)
ENVREF = ('WM_TOPN', 'WM_LAG', 'WM_REF', 'WM_TICK', 'WM_TSPAN'); TOPN_PIN = 500; N2_PIN = 200   # variables the table code reads: the builder refuses when one is set; the two list depths of the table code's defaults
_GL = "o = np.argsort(c[ids]); inl[ids[o[-TOPN:]], m] = True; inl[ids[o[:TOPN]], m] = True; in2[ids[o[-N2:]], m] = True; in2[ids[o[:N2]], m] = True"
_GLN = "o = np.argsort(c[ids], kind='stable'); og_ = np.argsort(-c[ids], kind='stable'); inl[ids[og_[:TOPN]], m] = True; inl[ids[o[:TOPN]], m] = True; in2[ids[og_[:N2]], m] = True; in2[ids[o[:N2]], m] = True"
_HA = "c = chg[:, m]; ids = np.where(~np.isnan(c))[0]\n        if len(ids) == 0: continue\n        "; _HC = "c = chg2[:, m - 720]; ids = np.where(~np.isnan(c))[0]\n        if len(ids) == 0: continue\n        "
_MAO = "v = CV[:, m]; iv = np.argsort(v)[-TOPN:]; inl[iv[v[iv] > 0], m] = True; iv = iv[-N2:]; in2[iv[v[iv] > 0], m] = True"; _MAN = "v = CV[:, m]; iv = np.where(v > 0)[0]; iv = iv[np.argsort(-v[iv], kind='stable')]; inl[iv[:TOPN], m] = True; in2[iv[:N2], m] = True"
_AL = "inl = np.zeros((n, NM), np.bool_); in2 = np.zeros((n, NM), np.bool_); N2 = 200"; _ALK = _AL + "; _K = np.zeros((10, n, NM), np.bool_)"; _KMN = "; _K[4, iv[:TOPN], m] = True; _K[5, iv[:N2], m] = True"
_MAOK = "v = CV[:, m]; iv = np.argsort(v)[-TOPN:]; inl[iv[v[iv] > 0], m] = True; _K[4, iv[v[iv] > 0], m] = True; iv = iv[-N2:]; in2[iv[v[iv] > 0], m] = True; _K[5, iv[v[iv] > 0], m] = True"
def _KGN(k): return "; _K[%d, ids[og_[:TOPN]], m] = True; _K[%d, ids[og_[:N2]], m] = True; _K[%d, ids[o[:TOPN]], m] = True; _K[%d, ids[o[:N2]], m] = True" % (k, k + 1, k + 2, k + 3)
def _KGO(k): return "; _K[%d, ids[o[-TOPN:]], m] = True; _K[%d, ids[o[-N2:]], m] = True; _K[%d, ids[o[:TOPN]], m] = True; _K[%d, ids[o[:N2]], m] = True" % (k, k + 1, k + 2, k + 3)
COMP = ('gain', 'gain, second depth', 'loss', 'loss, second depth', 'Most Active', 'Most Active, second depth', 'post-market gain', 'post-market gain, second depth', 'post-market loss', 'post-market loss, second depth')   # the rows of _K
# The declared replacements of the list code. E2b: first N of (value descending, ticker ascending) / first N of (value ascending, ticker ascending); ids ascend with the ticker and the sort is stable. E2c: Most Active stands BEFORE the guard.
LISTPATCH = (('component matrices for the prefix test (no value changes)', _AL, _ALK),
             ('main loop: Most Active in every minute from names with volume (E2c), then the gain and loss lists (E2b)', _HA + _GL + "\n        " + _MAO, _MAN + _KMN + "\n        " + _HA + _GLN + _KGN(0)),
             ('post-market gain and loss lists (E2b)', _HC + _GL, _HC + _GLN + _KGN(6)))
V2_WM2L1 = 'd1f5da851817972df3bfb077e073d059'; V3_WM2L1 = 'e5a4c80f92defaf691c7eafe49f6dffd'   # md5 of the table code as builders 5561d4e3 and e6f36ed3 wrote it, for `keydiff`
_CAP3 = '_CAP.update(tick=tick, inl=inl, in2=in2, chg=chg, cv=CV, chg2=chg2, has=has, c=C); ok = inl & has & (np.nan_to_num(C) >= MINP);'
TYPEPATCH = ("types = set(pd.read_csv(types_path, dtype=str).ticker) if types_path != '-' else None", "types = _L1_READ_TYPES(types_path) if types_path != '-' else None")   # erratum E1g: the table code has no types reader of its own (days2 is not run by the build; its line is replaced all the same)
CAPPATCH = ('ok = inl & has & (np.nan_to_num(C) >= MINP);', '_CAP.update(tick=tick, inl=inl, in2=in2, comp=_K, chg=chg, cv=CV, chg2=chg2, has=has, c=C); ok = inl & has & (np.nan_to_num(C) >= MINP);')   # no value changes: the membership matrices of the day for the prefix test
BARCOLS = ('ticker', 'volume', 'open', 'close', 'high', 'low', 'window_start')   # the columns of a daily bar file that the table code reads
PXCOLS = ['xlos', 'xhis', 'pblof', 'pblofs', 'pblo5', 'pblo5s', 'pstE', 'pqiE', 'pqcE', 'pqtE'] + ['%s%d' % (a, S) for S in (0, 2, 5, 15) for a in ('pst', 'qb', 'qa', 'qbz', 'qaz', 'pqi', 'pqc', 'pqt')]
F_UNK, F_WATCH, F_ENTMIN, F_BIDL, F_SPR, F_DURING, F_UCODES, F_CUT, F_STRUNK = 1, 2, 4, 8, 16, 32, 64, 128, 256
FLAGS = (('order_unknown', F_UNK), ('watched_stop', F_WATCH), ('stopped_in_entry_minute', F_ENTMIN), ('bid_under_L_at_entry', F_BIDL), ('spread_over_u', F_SPR), ('stop_during_exit', F_DURING), ('upper_codes_unknown', F_UCODES), ('cut', F_CUT), ('stress_order_unknown', F_STRUNK))
OUT = ('st est ereason A ebid h_val h_ret_ok h_mark_ok h_min h_sec cut d_val d_n t_val t_ret_ok t_mark_ok t_min t_sec t_dec t_trail b_unit u b_cls b_val b_ret_ok b_mark_ok b_min b_sec b_dec b_stress b_flags b_opt cls_ok b_opt_src b_stress_src').split()
O = {k: j for j, k in enumerate(OUT)}
EREASON = {0: 'filled', 1: 'side validity unresolved', 2: 'state', 3: 'no ask or no size', 4: 'ask invalid by indicator', 5: 'condition 20'}
UNIT = {0: 'ok', 1: 'no vu', 2: 'u not finite', 3: 'u <= 0', 4: 'u >= 1'}


def codes(s):
    if s is None or s != s or s == '': return frozenset()
    r = set()
    for x in str(s).split(','):
        x = x.strip()
        if x == '': continue
        if not x.lstrip('-').isdigit(): raise SystemExit('REFUSED: code_token_unreadable: code token %r in %r cannot be parsed' % (x, s))
        r.add(int(x))
    return frozenset(r)


@nb.njit(cache=True)
def _sale(SB, i, dk, fl, sl):
    # SALE rule for a sale decided at the close of minute dk: (dk+1, 5 s), (dk+1, 15 s), then 5 s of each later minute up to sl, and 15 s of the flatten minute too (erratum J). Returns price, minute, mark index.
    for k in range(dk + 1, sl + 1):
        for s in (2, 3):
            if s == 3 and k > dk + 1 and k != fl: continue
            x = SB[s, i, k]
            if x == x: return x, k, s
    return np.nan, -1, -1


@nb.njit(cache=True)
def _stop(SB, i, j, fl, sl):
    # stop fill proxy (section 4 PRIMARY) after trigger minute j: 0, 2, 5, 15 s of minute j+1, then 5 s of each later minute (and 15 s of the flatten minute) up to sl
    for k in range(j + 1, sl + 1):
        for s in range(4):
            if k > j + 1 and not (s == 2 or (s == 3 and k == fl)): continue
            x = SB[s, i, k]
            if x == x: return x, k, s
    return np.nan, -1, -1


@nb.njit(cache=True)
def _mark(RB, TRB, i, e, sl):
    # inventory mark (erratum C): the latest bid above zero among the px marks and the quotes known at minute closes, after the entry fill mark (e, 5 s) and not after the last liquidation attempt (sl, 5 s). Returns bid, minute, mark index (4 = minute close).
    for k in range(sl, e - 1, -1):
        if k < sl:
            r = TRB[i, k]
            if r == r: return r, k, 4
            r = RB[3, i, k]
            if r == r: return r, k, 3
        if k > e:
            for s in (2, 1, 0):
                r = RB[s, i, k]
                if r == r: return r, k, s
    return np.nan, -1, -1


@nb.njit(cache=True)
def _close(price, fm, fs, RB, TRB, i, e, sl, A):
    # value, ret_ok, mark_ok, minute, mark index of an exit
    if price == price: return price / A - 1, 1.0, 0.0, fm, fs
    r, k, s = _mark(RB, TRB, i, e, sl)
    if r == r: return r / A - 1, 0.0, 1.0, k, s
    return np.nan, 0.0, 0.0, -1, -1


@nb.njit(cache=True)
def core(ri, rq, rs, vu, FLm, SLm, EA, EST, ERS, EB, SB, RB, TB, TU, TRB, DE, DL, TLO, THI, XLOS, XHIS, PBLOF, PBLOFS, PBLO5, PBLO5S, CC, nout, unk_no):
    n = len(ri); out = np.full((n, nout), np.nan); SEC = (0.0, 2.0, 5.0, 15.0)
    for r in range(n):
        i = ri[r]; q = rq[r]; s_ = rs[r]; fl = FLm[s_]; sl = SLm[s_]; e = q + 1
        if e > fl - 1:
            out[r, 0] = 3; continue   # no window left (the table has no such row)
        A = EA[i, e]; out[r, 1] = EST[i, e]; out[r, 2] = ERS[i, e]
        if not (A == A):
            out[r, 0] = 1; continue
        out[r, 0] = 0; out[r, 3] = A; eb = EB[i, e]; out[r, 4] = eb; dk = min(q + HOR, fl - 1); cut = 1.0 if q + HOR > fl - 1 else 0.0; out[r, 10] = cut
        # h30e
        p, fm, fs = _sale(SB, i, dk, fl, sl); v, a_, b_, m_, x_ = _close(p, fm, fs, RB, TRB, i, e, sl, A)
        out[r, 5] = v; out[r, 6] = a_; out[r, 7] = b_; out[r, 8] = m_; out[r, 9] = x_
        # d30e
        mn = np.inf; cnt = 0
        for k in range(e, dk + 1):
            x = DE[i, k] if k == e else DL[i, k]
            if x == x:
                cnt += 1
                if x < mn: mn = x
        out[r, 12] = cnt
        if cnt > 0: out[r, 11] = mn / A - 1
        # t10e
        peak = A; dec = fl - 1; trail = 0.0
        for k in range(e, fl):
            c = CC[i, k]
            if c == c:
                if c > peak: peak = c
                if c <= (1 - TRAIL) * peak:
                    dec = k; trail = 1.0; break
        p, fm, fs = _sale(SB, i, dec, fl, sl); v, a_, b_, m_, x_ = _close(p, fm, fs, RB, TRB, i, e, sl, A)
        out[r, 13] = v; out[r, 14] = a_; out[r, 15] = b_; out[r, 16] = m_; out[r, 17] = x_; out[r, 18] = dec; out[r, 19] = trail
        # b30e
        u = vu[r] * np.sqrt(HOR); out[r, 32] = 0.0
        if not (vu[r] == vu[r]): out[r, 20] = 1; continue
        if not np.isfinite(u): out[r, 20] = 2; continue
        if u <= 0: out[r, 20] = 3; continue
        if u >= 1: out[r, 20] = 4; continue
        out[r, 20] = 0; out[r, 21] = u; L = A * (1 - DN * u); U = A * (1 + UP * u); fg = 0; cls = -1; dec = -1; opt = False; trig = -1
        if s_ != 1: fg |= F_WATCH
        if cut > 0: fg |= F_CUT
        if eb > 0 and eb <= L: fg |= F_BIDL | F_SPR   # one comparison, two names (erratum F)
        opte = False; optl = False
        for k in range(e, dk + 1):
            lo = TLO[i, k]
            if lo == lo and lo <= L:
                ou = k == e and not (XLOS[i, k] >= 5)
                if ou: fg |= F_UNK
                if not (ou and unk_no):   # unk_no: the alternative reading, an order-unknown low of the entry minute is NOT a stop (printed beside, never the label)
                    cls = 2; trig = k
                    if k == e: fg |= F_ENTMIN
                    break
            if THI[i, k] >= U:
                opt = True
                if k == e: opte = True
                else: optl = True
            tb = TB[i, k]
            if tb == tb and tb >= U:
                cls = 1; dec = k; break
            tu = TU[i, k]
            if tu == tu and tu >= U: fg |= F_UCODES
            if k == dk: cls = 0; dec = k
        stress = np.nan; ssrc = np.nan
        if cls == 2:
            p, fm, fs = _stop(SB, i, trig, fl, sl); v, a_, b_, m_, x_ = _close(p, fm, fs, RB, TRB, i, e, sl, A); out[r, 28] = trig
            if p == p:
                xl = XLOS[i, trig]
                if trig == e: lw = PBLO5[i, trig]; ls = PBLO5S[i, trig]; need = 5.0; unk = not (xl >= 5)
                else: lw = PBLOF[i, trig]; ls = PBLOFS[i, trig]; need = -1.0; unk = False
                okl = lw == lw and lw > 0
                known = okl and ls == ls and xl == xl and (not unk) and ls > xl and ls >= need
                sp = p
                if known and lw < p: sp = lw
                if okl and not known: fg |= F_STRUNK
                stress = (sp / A - 1) / u; ssrc = 1.0 if known else (2.0 if okl else 0.0)
        else:
            p, fm, fs = _sale(SB, i, dec, fl, sl); v, a_, b_, m_, x_ = _close(p, fm, fs, RB, TRB, i, e, sl, A); out[r, 28] = dec
            last = fm if p == p else sl; lsec = SEC[fs] if p == p else 5.0
            for k in range(dec + 1, last + 1):
                lo = TLO[i, k]
                if lo == lo and lo <= L:
                    if k < last or lo < L or not (XLOS[i, k] > lsec):   # erratum E: in the fill minute only a low exactly at L first printed after the fill second is proven to follow the fill
                        fg |= F_DURING; break
            if p == p: stress = (p / A - 1) / u
        osrc = 0.0
        if optl: osrc = 2.0
        elif opte: osrc = 1.0 if XHIS[i, e] >= 5 else 3.0
        out[r, 22] = cls; out[r, 23] = v / u; out[r, 24] = a_; out[r, 25] = b_; out[r, 26] = m_; out[r, 27] = x_; out[r, 29] = stress; out[r, 30] = fg; out[r, 31] = UP if opt else v / u; out[r, 32] = 1.0; out[r, 33] = osrc; out[r, 34] = ssrc
    return out


def derive(M):
    # M: raw per-cell arrays -> the arrays core() reads. Raw input (n stocks x NM minutes):
    #   haspx; per mark S: qb qa qbz qaz pst (NaN = empty) bb ab c20 (bool: indicator invalidates the bid / the ask, condition 20) pqt (int64) as dicts keyed by S;
    #   qn, tk last quote qbid qask qbsl qasl; terminal mark pstE (NaN = none) bbE abE c20E pqtE; tlo thi xlos pblof pblofs pblo5 pblo5s; close.
    n = M['haspx'].shape[0]; hp = M['haspx']; z = lambda a: np.where(np.isnan(a), 0.0, a)
    has = M['qn'] > 0; idx = np.where(has, np.arange(NM)[None, :], -1); LQ = np.maximum.accumulate(idx, axis=1); LP = np.concatenate([np.full((n, 1), -1), LQ[:, :-1]], axis=1)
    cb, ca, cbz, caz = z(M['qbid']), z(M['qask']), z(M['qbsl']), z(M['qasl']); known = ~np.isnan(M['pstE'])
    dst = np.where((cb <= 0) & (ca <= 0), 6, np.where(ca <= 0, 2, np.where(cb <= 0, 3, np.where(ca == cb, 4, np.where(cb > ca, 5, 1)))))
    cst = np.where(known, np.nan_to_num(M['pstE']).astype(np.int64) % 10, dst); cbb = M['bbE'] & known; cab = M['abE'] & known; c20 = M['c20E'] & known; cts = np.where(known, M['pqtE'], 0)
    I0 = M.get('init')   # erratum P: per stock the last quote before 04:00 (has, bid, ask, bsz, asz, pstE or NaN, bb, ab, c20, pqt); None = no stock has one
    if I0 is None: I0 = dict(has=np.zeros(n, bool), bid=np.zeros(n), ask=np.zeros(n), bsz=np.zeros(n), asz=np.zeros(n), pstE=np.full(n, np.nan), bb=np.zeros(n, bool), ab=np.zeros(n, bool), c20=np.zeros(n, bool), pqt=np.zeros(n, np.int64))
    ih = I0['has'][:, None]; ib, ia, ibz, iaz = (np.where(I0['has'], z(I0[k]), 0.0)[:, None] for k in ('bid', 'ask', 'bsz', 'asz')); ikn = (~np.isnan(I0['pstE']))[:, None]
    idst = np.where((ib <= 0) & (ia <= 0), 6, np.where(ia <= 0, 2, np.where(ib <= 0, 3, np.where(ia == ib, 4, np.where(ib > ia, 5, 1)))))
    ist = np.where(ih, np.where(ikn, np.nan_to_num(I0['pstE']).astype(np.int64)[:, None] % 10, idst), 0); ibb = I0['bb'][:, None] & ikn & ih; iab = I0['ab'][:, None] & ikn & ih; ic20 = I0['c20'][:, None] & ikn & ih; its_ = np.where(ih & ikn, I0['pqt'][:, None], 0)
    def carry(ix):   # the carried quote for the minute index array ix (-1 = no in-grid update yet: the quote from before 04:00 when the stock has one, else no quote yet today)
        j = np.maximum(ix, 0); g = lambda a: np.take_along_axis(a, j, axis=1); none = ix < 0
        return (np.where(none, ib, g(cb)), np.where(none, ia, g(ca)), np.where(none, ibz, g(cbz)), np.where(none, iaz, g(caz)), np.where(none, ist, g(cst)), np.where(none, ibb, g(cbb)), np.where(none, iab, g(cab)), np.where(none, ic20, g(c20)), np.where(none, ikn | ~ih, g(known)), np.where(none, its_, g(cts)))
    def bid_ok(b, bz, st, bb, c2, kn): return kn & np.isin(st, (1, 2, 4)) & (b > 0) & (bz > 0) & ~bb & ~c2
    Q = carry(LP); SB = np.full((4, n, NM), np.nan); RB = np.full((4, n, NM), np.nan); TS = np.zeros((4, n, NM), np.int64); SS = np.zeros((4, n, NM), np.int8); D = {}
    for si, S in enumerate(MARKS):
        st = np.where(hp, np.nan_to_num(M['pst'][S]).astype(np.int64) % 10, Q[4]); b = np.where(hp, z(M['qb'][S]), Q[0]); bz = np.where(hp, z(M['qbz'][S]), Q[2]); bb = np.where(hp, M['bb'][S], Q[5]); c2 = np.where(hp, M['c20'][S], Q[7]); kn = hp | Q[8]
        ok = bid_ok(b, bz, st, bb, c2, kn); SB[si] = np.where(ok, b, np.nan); RB[si] = np.where(b > 0, b, np.nan); TS[si] = np.where(hp, M['pqt'][S], Q[9]); SS[si] = st
        if S == 5:
            a = np.where(hp, z(M['qa'][S]), Q[1]); az = np.where(hp, z(M['qaz'][S]), Q[3]); ab = np.where(hp, M['ab'][S], Q[6])
            rs = np.where(~kn, 1, np.where(~np.isin(st, (1, 3, 4)), 2, np.where((a <= 0) | (az <= 0), 3, np.where(ab, 4, np.where(c2, 5, 0)))))
            D['EA'] = np.where(rs == 0, a, np.nan); D['EST'] = st.astype(np.float64); D['ERS'] = rs.astype(np.float64); D['EB'] = b; qsb = np.where(ok, b, np.nan)
    T = carry(LQ); okT = bid_ok(T[0], T[2], T[4], T[5], T[7], T[8]); D['TB'] = np.where(okT, T[0], np.nan)
    D['TU'] = np.where(~T[8] & np.isin(T[4], (1, 2, 4)) & (T[0] > 0) & (T[2] > 0), T[0], np.nan); D['TRB'] = np.where(T[0] > 0, T[0], np.nan); noq = (LQ < 0) & ~ih; D['TQA'] = np.where(noq, np.nan, T[1]); D['TQB'] = np.where(noq, np.nan, T[0]); D['PRE'] = (LQ < 0) & ih; D['LP'] = LP; D['LQ'] = LQ; D['TTS'] = T[9]; D['TST'] = T[4].astype(np.int8)
    with np.errstate(divide='ignore', invalid='ignore'): D['HS'] = np.where((T[0] > 0) & (T[1] > 0) & (T[0] <= T[1]), (T[1] - T[0]) / (T[1] + T[0]), np.nan)
    pos = lambda a: np.where(a > 0, a, np.nan)
    qs = np.where(bid_ok(Q[0], Q[2], Q[4], Q[5], Q[7], Q[8]), Q[0], np.nan)   # standing fillable bid of a quiet minute
    D['DE'] = np.where(hp, pos(M['pblo5']), qs); D['DL'] = np.where(hp, pos(M['pblof']), qs)
    D['SB'] = SB; D['RB'] = RB; D['TS'] = TS; D['SS'] = SS; D['TLO'] = pos(M['tlo']); D['THI'] = pos(M['thi']); D['CC'] = M['close']
    for k in ('xlos', 'xhis', 'pblof', 'pblofs', 'pblo5', 'pblo5s'): D[k.upper()] = M[k]
    return D


def run(D, ri, rq, rs, vu, unk_no=False):
    return core(np.asarray(ri, np.int64), np.asarray(rq, np.int64), np.asarray(rs, np.int64), np.asarray(vu, np.float64), FL, SL, D['EA'], D['EST'], D['ERS'], D['EB'], D['SB'], D['RB'], D['TB'], D['TU'], D['TRB'], D['DE'], D['DL'], D['TLO'], D['THI'], D['XLOS'], D['XHIS'],
                D['PBLOF'], D['PBLOFS'], D['PBLO5'], D['PBLO5S'], D['CC'], len(OUT), unk_no)


def rank30(minute, val):
    # rk30e: average rank / n among the rows of the same minute that have a value, highest value = highest rank; n; empty when n < RKMIN
    import pandas as pd
    s = pd.Series(val); g = s.groupby(np.asarray(minute)); n = g.transform('count'); rk = g.rank(pct=True)
    return rk.where(n >= RKMIN).to_numpy(), n.to_numpy()


def select1(pred):
    # the scorer's best 1% of ONE minute, unchanged (wm2eval.py line 24): descending rank with ties averaged, r = rank / n, selected when r <= 0.01 or r * n <= 1.
    # So t rows tied for first are selected whole when (t + 1) / 2 <= max(1, 0.01 n), else none (codex-026).
    import pandas as pd
    s = pd.Series(pred); n = len(s); r = s.rank(ascending=False) / n
    return ((r <= 0.01) | (r * n <= 1)).to_numpy()


def px_gaps(M):
    # erratum G: minutes where tk has a quote update or a trade and px has no row
    return int((((M['qn'] > 0) | (M['tn'] > 0)) & ~M['haspx']).sum())


def seed_gaps(I0):
    # erratum E9a: stocks whose chosen quote update before 04:00 has no px row (its terminal state and codes would be missing, not unknown)
    return int((I0['has'] & ~I0['px']).sum())


def sealed(d): return SEAL[0] <= d <= SEAL[1] or d > HARD_LAST


def md5(path):
    h = hashlib.md5()
    with open(path, 'rb') as f:
        for b in iter(lambda: f.read(1 << 22), b''): h.update(b)
    return h.hexdigest()


_ONCE = {}; _KEEP = [True]


def raw(path):
    # erratum E1h, 1 (version 12): the bytes of a pinned text file or a bar file are read ONCE per process; the md5 and the parsing both use that buffer, so no parse ever sees bytes that were not the ones hashed.
    # The fixtures switch the keeping off (_KEEP), because they rewrite files under one name; each read is then still one buffer for its md5 and its parse. Dedicated fixtures show the kept buffer.
    k = os.path.realpath(path)
    if k in _ONCE: return _ONCE[k]
    with open(path, 'rb') as f: b = f.read()
    if _KEEP[0]: _ONCE[k] = b
    return b


def md5b(b): return hashlib.md5(b).hexdigest()


def forget(path): _ONCE.pop(os.path.realpath(path), None)   # for a file this process itself writes anew under one name (the cut bar file of the prefix test), never for an input


def plain(b, path, what):
    # erratum E1h, 2: a NUL byte or a double quote is refused before the csv reader, which would rewrite the field (NA\0X and "N"A both became NA, 1\00 became 1)
    for c, nm in ((b'\x00', 'a NUL byte'), (b'"', 'a double quote')):
        i = b.find(c)
        if i >= 0: raise SystemExit('REFUSED: %s_file_bytes: %s holds %s at byte %d (line %d); a field is taken as written, never rewritten by the csv reader' % (what, path, nm, i, b.count(b'\n', 0, i) + 1))


def calendar(p=None, pin=CAL_MD5):
    # erratum E1a: the pinned trading-day calendar beside the builder (the two arguments are for the fixtures only)
    p = p or os.path.join(os.path.dirname(os.path.abspath(__file__)), CALF)
    if not os.path.isfile(p): raise SystemExit('REFUSED: calendar_file_missing: no trading-day calendar %s beside the builder' % CALF)
    b = raw(p)
    if md5b(b) != pin: raise SystemExit('REFUSED: calendar_file_md5: the trading-day calendar %s differs from the pinned md5' % CALF)
    cal = [l.strip() for l in b.decode('utf-8', 'replace').split('\n') if l.strip() and not l.startswith('#')]
    if cal != sorted(set(cal)): raise SystemExit('REFUSED: calendar_file_unreadable: the trading-day calendar is not ascending and unique')
    return cal


def embargo(cal):
    # the first HIST sessions after the sealed window, from the calendar alone
    i = bisect.bisect_right(cal, SEAL[1]); return cal[i:i + HIST]


_NAME = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}\.csv\.gz'); _DATE = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}'); _CUT = re.compile(r'[0-9]{2}:[0-9]{2}')   # always used with fullmatch: `$` would also match before a final newline, and \d other digits than 0-9


def bar_names(src):
    # erratum E1b, names only (nothing is opened in a folder). A folder: the dates of the regular files named exactly YYYY-MM-DD.csv.gz; every other name is returned apart and never stands for a date.
    # An explicit inventory list (a text file, one name per line): exact file names or bare dates; other lines apart.
    if os.path.isfile(src):
        nm = [l.strip() for l in open(src) if l.strip() and not l.startswith('#')]; ok = [bool(_NAME.fullmatch(x) or _DATE.fullmatch(x)) for x in nm]
        return {x[:10] for x, k in zip(nm, ok) if k}, sorted(x for x, k in zip(nm, ok) if not k)
    good = set(); other = []
    for x in sorted(os.listdir(src)):
        if _NAME.fullmatch(x) and stat.S_ISREG(os.lstat(os.path.join(src, x)).st_mode): good.add(x[:10])
        else: other.append(x)
    return good, other


def day_ok(day):
    # erratum E1c: a day is written YYYY-MM-DD and is a real date; anything else is refused as malformed, never under another cause
    try: ok = bool(_DATE.fullmatch(day)) and datetime.date.fromisoformat(day).isoformat() == day
    except (ValueError, TypeError): ok = False
    if not ok: raise SystemExit('REFUSED: day_malformed: %r is not a day written YYYY-MM-DD' % (day,))


def read_types(types):
    # erratum E1c: the tickers of the types file, or a typed refusal naming it.
    # erratum E1g (version 10): a ticker is TEXT. The default reader turns NA, NULL, None, nan and others into a missing value and so dropped the stock NA from every table; here nothing is converted, and an empty
    # ticker is refused, never dropped. This is the ONE types reader of the build: the table code takes its set from here (TYPEPATCH).
    # erratum E1h (version 12): the buffer that is parsed is the buffer whose md5 the build compares with the pin (raw); a NUL byte or a double quote is refused before the csv reader (plain).
    import pandas as pd, io
    try: b = raw(types)
    except FileNotFoundError: raise SystemExit('REFUSED: types_file_missing: no types file %s' % types)
    except OSError as x: raise SystemExit('REFUSED: types_file_unreadable: %s cannot be read (%s)' % (types, type(x).__name__))
    plain(b, types, 'types')
    try: d = pd.read_csv(io.BytesIO(b), dtype=str, keep_default_na=False, na_filter=False)
    except Exception as x: raise SystemExit('REFUSED: types_file_unreadable: %s cannot be read as a csv (%s)' % (types, type(x).__name__))
    if 'ticker' not in d.columns: raise SystemExit('REFUSED: types_file_unreadable: %s has no ticker column' % types)
    e = [k for k, t in enumerate(d.ticker) if not isinstance(t, str) or t.strip() == '']
    if e: raise SystemExit('REFUSED: types_ticker_empty: %s has %d lines with an empty ticker; first: data line %d' % (types, len(e), e[0] + 1))
    return set(d.ticker)


def ny_date(ns):
    return datetime.datetime.fromtimestamp(int(ns) / 1e9, zoneinfo.ZoneInfo('America/New_York')).date().isoformat()


def bars_readable(src, need):
    # erratum E1c: every bar file of the checked list is a gzip csv whose first line names the columns the table code reads and whose first data row carries the file's New York date; only those two lines are read.
    # Raised before the table code opens any of them. Returns bytes and md5 of each file for the build record.
    rec = []
    for d in need:
        f = os.path.join(src, d + '.csv.gz')
        try:
            import io
            b = raw(f)   # erratum E1h, 1: bytes and md5 of the record are those of the buffer that bar_load unpacks and parses
            with gzip.GzipFile(fileobj=io.BytesIO(b)) as g: head = g.readline().decode('utf-8', 'replace').strip().split(','); row = g.readline().decode('utf-8', 'replace').strip().split(',')
        except Exception as x: raise SystemExit('REFUSED: bar_file_unreadable: %s is not a readable gzip csv (%s)' % (f, type(x).__name__))
        if not set(BARCOLS) <= set(head): raise SystemExit('REFUSED: bar_file_unreadable: %s is empty or lacks the columns %s' % (f, ', '.join(c for c in BARCOLS if c not in head)))
        try: got = ny_date(row[head.index('window_start')])
        except Exception: raise SystemExit('REFUSED: bar_file_unreadable: %s has no data row with a readable window_start' % f)
        if got != d: raise SystemExit('REFUSED: bar_file_wrong_day: %s holds bars of %s' % (f, got))
        rec.append((d, len(b), md5b(b)))
    return rec


def bar_load(path):
    # erratum E1d, 1: the read of the pinned wide.load (same frame returned; a fixture compares the two) with the checks made on that one read, before the minute index is used by anyone: every row of the file,
    # inside 04:00..20:00 or not, carries the New York date of the file's name, and (ticker, minute) is unique. The table code and the builder's read of the target both come through here.
    import pandas as pd
    day = os.path.basename(path)[:10]
    if not _NAME.fullmatch(os.path.basename(path)): raise SystemExit('REFUSED: bar_file_unreadable: %s is not named YYYY-MM-DD.csv.gz' % path)
    # erratum E1h (version 12): 1 the file's bytes come from the one buffer (raw) that bars_readable hashed; 2 a NUL byte or a double quote in the unpacked text is refused before the csv reader;
    # 3 every numeric field is finite, open, close, high and low are above 0, volume is not below 0: refused by ticker and minute (inf, 1e999, 0 and -5 used to pass into the table).
    import io
    try: txt = gzip.decompress(raw(path))
    except Exception as x: raise SystemExit('REFUSED: bar_file_unreadable: %s cannot be read as a gzip csv of bars (%s)' % (path, type(x).__name__))
    plain(txt, path, 'bar')
    try: d = pd.read_csv(io.BytesIO(txt), dtype={'ticker': str}, na_filter=False)
    except Exception as x: raise SystemExit('REFUSED: bar_file_unreadable: %s cannot be read as a gzip csv of bars (%s)' % (path, type(x).__name__))
    for c in BARCOLS:   # version 7: a text in a price column or a fractional stamp is refused by the column's name, not carried into the table
        if c not in d.columns: raise SystemExit('REFUSED: bar_file_unreadable: %s lacks the column %s' % (path, c))
        if c == 'ticker': continue
        if not (pd.api.types.is_integer_dtype(d[c]) if c == 'window_start' else pd.api.types.is_numeric_dtype(d[c]) and not pd.api.types.is_bool_dtype(d[c])): raise SystemExit('REFUSED: bar_file_unreadable: %s: the column %s is read as %s, not as %s' % (path, c, d[c].dtype, 'an integer' if c == 'window_start' else 'a number'))
    ts = pd.to_datetime(d.window_start, unit='ns', utc=True).dt.tz_convert('America/New_York')
    off = (d.window_start.values % NSMIN) != 0   # erratum E1e, 2a: an exact minute start, before the minute index is derived
    if off.any(): i = int(np.flatnonzero(off)[0]); raise SystemExit('REFUSED: bar_stamp_off_minute: %s has %d rows whose window_start is not an exact minute start; first: %s at %d' % (path, int(off.sum()), d.ticker.values[i], int(d.window_start.values[i])))
    bad = ts.dt.normalize() != pd.Timestamp(day, tz='America/New_York')
    if bad.any(): i = int(np.flatnonzero(bad.values)[0]); raise SystemExit('REFUSED: bar_file_wrong_day: %s holds %d rows of other days; first: %s at %s' % (path, int(bad.sum()), d.ticker.values[i], ts.iloc[i].isoformat()))
    for c in d.columns:
        if c == 'ticker' or not pd.api.types.is_numeric_dtype(d[c]): continue
        v = d[c].values.astype(np.float64); bad = ~np.isfinite(v)
        if c in ('open', 'close', 'high', 'low'): bad |= ~(v > 0)
        elif c == 'volume': bad |= ~(v >= 0)
        if bad.any(): i = int(np.flatnonzero(bad)[0]); raise SystemExit('REFUSED: bar_value_invalid: %s has %d rows whose %s is not a finite number%s; first: %s at %s holds %r' % (path, int(bad.sum()), c, ' above 0' if c in ('open', 'close', 'high', 'low') else ' of 0 or more' if c == 'volume' else '', d.ticker.values[i], ts.iloc[i].isoformat(), v[i]))
    d['m'] = (ts.dt.hour * 60 + ts.dt.minute - 240).astype(np.int32)
    dup = d.duplicated(['ticker', 'm'])
    if dup.any(): i = int(np.flatnonzero(dup.values)[0]); raise SystemExit('REFUSED: bar_key_duplicate: %s has %d rows whose (ticker, minute) is already there; first: %s at %s' % (path, int(dup.sum()), d.ticker.values[i], ts.iloc[i].isoformat()))
    return d[(d.m >= 0) & (d.m < NM)]


def ny_ns(day, add=0):
    # the start of the New York date `day` (plus `add` days) in ns since the epoch
    x = datetime.date.fromisoformat(day) + datetime.timedelta(days=add); return int(datetime.datetime(x.year, x.month, x.day, tzinfo=zoneinfo.ZoneInfo('America/New_York')).timestamp()) * 10 ** 9


TKCOLS = ['sym', 'm', 'qn', 'tn', 'qbid', 'qask', 'qbsl', 'qasl', 'tlo', 'thi']   # the columns load_day reads from the tk file
DAYMIN = 1440   # minutes of a day: the range of the key m of the tk and px files (minutes from 00:00 New York)
NSMIN = 60 * 10 ** 9; NSSEC = 10 ** 9   # unit factors: ns in a minute, in a second
NKEYS = 3   # how many offending keys a refusal prints
PSTSET = (0, 1, 2, 3, 4, 5, 6, 11, 12, 13, 14, 15, 16)   # the states the frozen converter writes (null = no value); derive reads the base digit pst % 10 (1, 2, 4 a bid that can be sold to; 5 crossed; 6 cleared)
INTTYPES = ('TINYINT', 'SMALLINT', 'INTEGER', 'BIGINT', 'UTINYINT', 'USMALLINT', 'UINTEGER', 'UBIGINT')   # parquet integer columns as duckdb names them


def tick_keys(day, tkf, pxf):
    # erratum E1d, 2 and 3: the tk and the px file over all their rows, before load_day casts or indexes anything. Returns the row counts for the build record.
    import duckdb
    con = duckdb.connect(); rows = {}
    for nm, f, cols in (('tick', tkf, TKCOLS), ('price', pxf, ['sym', 'm'] + PXCOLS)):
        src = "read_parquet('%s')" % f
        def q(sql, what):
            try: return con.execute(sql).fetchall()
            except Exception as x: raise SystemExit('REFUSED: %s_file_unreadable: %s cannot be read as parquet (%s, %s)' % (nm, f, what, type(x).__name__))
        ty = {r[0]: r[1] for r in q('DESCRIBE SELECT * FROM %s' % src, 'schema')}; have = list(ty); miss = [c for c in cols if c not in have]
        if miss: raise SystemExit('REFUSED: %s_file_unreadable: %s lacks the columns %s' % (nm, f, ', '.join(miss)))
        for c in ['m'] + ([x for x in cols if x.startswith('pqt')] if nm == 'price' else []):   # version 7: the minute and every quote time are stored as integers (INTEGER and BIGINT in the frozen converter's files); a float would round a stamp onto its mark
            if ty[c] not in INTTYPES: raise SystemExit('REFUSED: %s_file_unreadable: %s: the column %s has the type %s, not an integer type' % (nm, f, c, ty[c]))
        mm = 'TRY_CAST(m AS DOUBLE)'; inv = "sym IS NULL OR m IS NULL OR %s IS NULL OR %s <> floor(%s) OR %s < 0 OR %s >= %d" % (mm, mm, mm, mm, mm, DAYMIN)
        n, bad = q('SELECT count(*), coalesce(sum((%s)::INT), 0) FROM %s' % (inv, src), 'keys')[0]; rows[nm] = int(n)
        if bad: raise SystemExit('REFUSED: %s_key_invalid: %s has %d rows whose sym is null or whose minute is null, not an integer or outside the day; first (sym, m): %s' % (nm, f, bad, q('SELECT sym, m FROM %s WHERE %s LIMIT %d' % (src, inv, NKEYS), 'keys')))
        dup = q('SELECT sym, m, count(*) FROM %s GROUP BY sym, m HAVING count(*) > 1 ORDER BY sym, m' % src, 'keys')
        if dup: raise SystemExit('REFUSED: %s_key_duplicate: %s has %d keys (sym, m) more than once; first (sym, m, rows): %s' % (nm, f, len(dup), dup[:NKEYS]))
    src = "read_parquet('%s')" % pxf; d0 = ny_ns(day); d1 = ny_ns(day, 1); ms = '(%d::BIGINT + m::BIGINT * %d::BIGINT)' % (d0, NSMIN); tcols = ['pqt%d' % S for S in MARKS] + ['pqtE']
    def some(cond, col): return con.execute('SELECT sym, m, %s FROM %s WHERE %s ORDER BY sym, m LIMIT %d' % (col, src, cond, NKEYS)).fetchall()
    def cnt(conds): return con.execute('SELECT %s FROM %s' % (', '.join('coalesce(sum((%s)::INT), 0)' % c for c in conds), src)).fetchone()
    has = lambda c: '%s IS NOT NULL AND %s <> 0' % (c, c); scols = ['pst%d' % S for S in MARKS] + ['pstE']
    uk = ['%s IS NOT NULL AND %s NOT IN (%s)' % (sc, sc, ', '.join(str(x) for x in PSTSET)) for sc in scols]   # version 9: a state the converter does not write
    for sc, cond, k in zip(scols, uk, cnt(uk)):
        if k: raise SystemExit('REFUSED: quote_state_unknown: %s has %d rows whose %s is not one of %s; first (sym, m, state): %s' % (pxf, k, sc, PSTSET, some(cond, sc)))
    sm = ['(pst%d IS NULL OR pst%d %% 10 = 0) AND (qb%d > 0 OR qa%d > 0)' % (S, S, S, S) for S in MARKS]   # erratum E1e, 1a; by the base digit (version 9)
    for S, cond, k in zip(MARKS, sm, cnt(sm)):
        if k: raise SystemExit('REFUSED: quote_state_missing: %s has %d rows whose mark at second %d has a bid or ask above zero and no state; first (sym, m, state): %s' % (pxf, k, S, some(cond, 'pst%d' % S)))
    tm = ['%s IS NOT NULL AND %s <> 0 AND (%s IS NULL OR %s = 0)' % (sc, sc, c, c) for sc, c in zip(scols, tcols)]
    for sc, c, cond, k in zip(scols, tcols, tm, cnt(tm)):
        if k: raise SystemExit('REFUSED: quote_time_missing: %s has %d rows whose %s says a quote exists and whose %s is null or 0; first (sym, m, state): %s' % (pxf, k, sc, c, some(cond, sc)))
    wd = ['%s AND (%s < %d::BIGINT OR %s >= %d::BIGINT)' % (has(c), c, d0, c, d1) for c in tcols]
    for c, cond, k in zip(tcols, wd, cnt(wd)):
        if k: raise SystemExit('REFUSED: quote_wrong_day: %s has %d rows whose %s is not on the New York date %s; first (sym, m, time): %s' % (pxf, k, c, day, some(cond, c)))
    am = ['%s AND %s > %s + %d::BIGINT' % (has('pqt%d' % S), 'pqt%d' % S, ms, S * NSSEC) for S in MARKS]
    for S, cond, k in zip(MARKS, am, cnt(am)):
        if k: raise SystemExit('REFUSED: quote_after_mark: %s has %d rows whose pqt%d is after the mark at second %d of their minute; first (sym, m, time): %s' % (pxf, k, S, S, some(cond, 'pqt%d' % S)))
    cond = '%s AND pqtE >= %s + %d::BIGINT' % (has('pqtE'), ms, NSMIN); k = cnt([cond])[0]
    if k: raise SystemExit('REFUSED: quote_at_or_after_boundary: %s has %d rows whose pqtE is at or after the start of the next minute; first (sym, m, time): %s' % (pxf, k, some(cond, 'pqtE')))
    jn = "FROM read_parquet('%s') t JOIN %s p ON p.sym = t.sym AND p.m = t.m WHERE t.qn > 0 AND " % (tkf, src)   # erratum E1e, 1b: the keys of both files whose tk row has a quote update
    for code, cond, col, what in (('terminal_state_missing', '(p.pstE IS NULL OR p.pstE % 10 = 0)', 'p.pstE', 'has no terminal state'), ('terminal_before_minute', 'p.pqtE IS NOT NULL AND p.pqtE <> 0 AND p.pqtE < (%d::BIGINT + p.m::BIGINT * %d::BIGINT)' % (d0, NSMIN), 'p.pqtE', 'has a terminal quote time before the start of its minute')):
        k = con.execute('SELECT count(*) ' + jn + cond).fetchone()[0]
        if k: raise SystemExit('REFUSED: %s: %s has %d rows whose minute has a quote update in the tick file and %s; first (sym, m, value): %s' % (code, pxf, k, what, con.execute('SELECT p.sym, p.m, %s %s%s ORDER BY p.sym, p.m LIMIT %d' % (col, jn, cond, NKEYS)).fetchall()))
    return rows


def tick_symbols(tkf, syms):
    # version 9: every table stock has at least one row in the tick file (a table stock has a bar, so it has trades; the converter keeps only the tickers of its types file plus SPY and QQQ)
    import duckdb
    have = {r[0] for r in duckdb.connect().execute("SELECT DISTINCT sym FROM read_parquet('%s')" % tkf).fetchall()}; miss = [x for x in syms if x not in have]
    if miss: raise SystemExit('REFUSED: tick_symbol_missing: %d table stocks have no row at all in %s; first: %s' % (len(miss), tkf, miss[:NKEYS]))


def closes(cap, syms):
    # erratum E1e, 2c: the minute closes of the table stocks from the close matrix the table code captured in its one read of the target (raw closes, NaN where a minute has no bar), aligned by ticker
    tk_ = np.asarray(cap['tick']); j = np.searchsorted(tk_, syms); j[j >= len(tk_)] = 0
    if len(syms) and not (tk_[j] == np.asarray(syms)).all(): raise SystemExit('REFUSED: closes_ticker_missing: a table stock is not among the tickers of the captured close matrix')
    return np.array(cap['c'][j], dtype=np.float64)


def claim(out):
    # erratum E1d, 4: the exclusive claim of one attempt on its output, taken before anything is computed. Exclusive create; no hard link. A claim that is already there refuses, whoever left it.
    c = out + '.claim'; tok = 'pid %d host %s start_ns %d' % (os.getpid(), socket.gethostname(), time.time_ns())
    try: fd = os.open(c, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        try: who = open(c).read().strip()
        except OSError: who = 'unreadable'
        raise SystemExit('REFUSED: output_claimed: %s is there (%s); another attempt builds this table, or a killed one left its claim: the builder never removes it' % (c, who))
    except OSError as x: raise SystemExit('REFUSED: output_not_writable: the claim %s cannot be created (%s)' % (c, type(x).__name__))
    try:
        with os.fdopen(fd, 'w') as f: f.write(tok + '\n')
    except OSError as x:   # erratum E1e, 3a: the claim was created by this attempt a moment ago, so the name is still its own to remove
        try: os.unlink(c); left = 'the empty claim was removed'
        except OSError: left = 'the claim STAYS and needs explicit recovery'
        raise SystemExit('REFUSED: output_not_writable: the token of the claim %s cannot be written (%s); %s' % (c, type(x).__name__, left))
    return c, tok


def holds(cl):
    try: return open(cl[0]).read().strip() == cl[1]
    except OSError: return False


def release(cl):
    if holds(cl): os.unlink(cl[0])


def attempt(out, fn):
    # erratum E1d, 4: one attempt holds the claim from before anything is computed to its end; the claim is released by success and by any failure
    cl = claim(out)
    try: out_ok(out); return fn(cl)   # erratum E1e, 3b: an attempt that waited across another's commit ends at once
    finally: release(cl)


def verify(tmp, day, cols, n):
    # erratum E1d, 4: the temporary file read back before publication
    import duckdb
    try:
        con = duckdb.connect(); src = "read_parquet('%s')" % tmp; have = [r[0] for r in con.execute('DESCRIBE SELECT * FROM %s' % src).fetchall()]
        if have != list(cols): raise SystemExit('REFUSED: output_verify_failed: %s does not have the declared column list' % tmp)
        rows, dates, keys = con.execute('SELECT count(*), count(DISTINCT date), count(DISTINCT (sym, m)) FROM %s' % src).fetchone(); other = con.execute("SELECT count(*) FROM %s WHERE date IS NULL OR date <> '%s' OR sym IS NULL OR m IS NULL" % (src, day)).fetchone()[0]
    except SystemExit: raise
    except Exception as x: raise SystemExit('REFUSED: output_verify_failed: %s cannot be read back (%s)' % (tmp, type(x).__name__))
    if rows != n: raise SystemExit('REFUSED: output_verify_failed: %s has %d rows, the table in memory %d' % (tmp, rows, n))
    if other or (rows and dates != 1): raise SystemExit('REFUSED: output_verify_failed: %s has rows of another date than %s or without a key' % (tmp, day))
    if keys != rows: raise SystemExit('REFUSED: output_verify_failed: %s has %d rows and %d keys (sym, m)' % (tmp, rows, keys))


def publish(L, out, day, cl, rep):
    # erratum E1d, 4: temporary name of this attempt -> read back -> the report (everything of it that can fail) -> the claim is still this attempt's -> the final name is still absent -> rename, the last step.
    # A temporary file of a failed attempt stays where it is and stops nobody.
    import duckdb
    tmp = os.path.join(os.path.dirname(os.path.abspath(out)), '.writing-%s-%d-%d-%s' % (socket.gethostname(), os.getpid(), time.time_ns(), os.path.basename(out)))
    con = duckdb.connect(); con.register('L_', L); con.execute("COPY L_ TO '%s' (FORMAT PARQUET, COMPRESSION ZSTD)" % tmp); con.close()
    verify(tmp, day, list(L.columns), len(L)); nb_ = os.path.getsize(tmp); h_ = md5(tmp); rep()   # version 7: bytes and md5 of the table are taken from the temporary file, before the rename
    try: print(READY % (day, md5(os.path.abspath(__file__)), os.path.abspath(out), nb_, h_, cl[1])); sys.stdout.flush()   # erratum E1e, 4b: the record of the commit is out before the commit
    except Exception as x: raise SystemExit('REFUSED: report_not_written: the record of %s could not be written to standard output (%s); nothing is published' % (out, type(x).__name__))
    if not holds(cl): raise SystemExit('REFUSED: output_claimed: the claim %s is no longer this attempt\'s; nothing is published' % cl[0])
    out_ok(out); os.rename(tmp, out); return tmp, nb_, h_


READY = 'build record: table ready: day %s; builder md5 %s; output %s; temporary file bytes %d; md5 %s; claim %s'
_EOL = re.compile(r'\r\n|\r|\n')   # erratum E1i, 5: a record ends at LF, CRLF or a bare CR
_READY = re.compile(r'build record: table ready: day ([0-9]{4}-[0-9]{2}-[0-9]{2}); builder md5 ([0-9a-f]{32}); output ([^\r\n]+); temporary file bytes ([0-9]+); md5 ([0-9a-f]{32}); claim (pid [0-9]+ host \S+ start_ns [0-9]+)')


def target_ok(day, earlyf):
    # erratum E1f, 2a (version 10): the refusals of a build that concern the target alone and need no data file, in the build's order with sealed_day first: day form, sealed_day (also a day after the last open day),
    # not_a_trading_day, history_in_sealed_window, history_before_archive_start, early_close. Reads the pinned calendar and the pinned early-close list, nothing else.
    day_ok(day)
    if sealed(day): raise SystemExit('REFUSED: sealed_day: %s is a sealed day' % day)
    cal = calendar(); needed(cal, set(cal), day)   # every calendar day counts as present here: bar_file_missing belongs to the build, not to this check
    if day in early_days(earlyf): raise SystemExit('REFUSED: early_close: %s is an early-close day; the table has no early-close sessions' % day)


def published(day, tab, log, earlyf):
    # erratum E1e, 4c (version 9): a committed table is recognised from a "table ready" record of its build log: same day, same absolute output path, same bytes, same md5, written by the builder that runs this check.
    # The confirmation line after the rename is not needed. Used by the driver of the full build and by the done test.
    # erratum E1f, 2 (version 10): the order. 1 the target may be built at all (target_ok), before the candidate or the log is touched; 2 the log is read and a record with this day, this builder and this absolute output path
    # is looked for: none = published_unverified with the candidate still unopened (no kind, no size, no digest); 3 only then the candidate's kind, bytes and md5 are taken and compared with the record.
    # erratum E1h (version 12): 4 a log that is the candidate itself is refused. Same path, or a log whose real path is the candidate's path: before the log is read, from the log's side alone (E1f 2a stands:
    # the candidate is not touched before a record names it). The same file under two names (a hard link, or the candidate a link to the log) can only be seen by looking at the candidate, so it is refused at
    # step 3, before the candidate is opened. 1 the log is read once, and bytes and md5 of the candidate are taken from one read of it.
    # erratum E1i (version 13): 6 the identity test (samefile: a stat of both names, no content) is made before the log is read; it replaces E1f 2a's "candidate not touched" by "no content of either file read".
    # 5 the records of the log end at LF, CRLF or a bare CR.
    target_ok(day, earlyf); me = md5(os.path.abspath(__file__)); ab = os.path.abspath(tab)
    if os.path.abspath(log) == ab or os.path.realpath(log) == os.path.join(os.path.realpath(os.path.dirname(ab)), os.path.basename(ab)): raise SystemExit(   # the candidate's FOLDER is resolved (on macOS /var is a link to /private/var), the candidate itself is not touched
        'REFUSED: published_log_is_candidate: the build log %s and the table %s are one file; nothing was read' % (log, tab))
    try: one_ = os.path.samefile(log, tab)   # erratum E1i, 6: the same file under two names (a hard link, a link in either direction), asked of the file system BEFORE any content is read; a missing log or candidate is not "the same file" and gets its own refusal below
    except OSError: one_ = False
    if one_: raise SystemExit('REFUSED: published_log_is_candidate: the build log %s and the table %s are one file under two names; nothing was read' % (log, tab))
    try:
        with open(log, 'rb') as f: lb = f.read()
        recs = [m.groups() for m in (_READY.fullmatch(l) for l in _EOL.split(lb.decode('utf-8', 'replace'))) if m]
    except OSError as x: raise SystemExit('REFUSED: published_unverified: the build log %s cannot be read (%s); the table was not opened' % (log, type(x).__name__))
    if len(recs) > 1: raise SystemExit('REFUSED: log_record_repeated: the build log %s holds %d "table ready" records; exactly one is expected; the table was not opened' % (log, len(recs)))   # erratum E1j, 1
    mine = [r for r in recs if r[:3] == (day, me, ab)]
    if not mine: raise SystemExit('REFUSED: published_unverified: none of the %d "table ready" records of %s has day %s, builder %s, output %s; the table was not opened' % (len(recs), log, day, me, ab))
    if not os.path.isfile(tab) or os.path.islink(tab): raise SystemExit('REFUSED: published_unverified: no regular file %s' % tab)
    if os.path.samefile(log, tab): raise SystemExit('REFUSED: published_log_is_candidate: the build log %s and the table %s are one file under two names; the table was not opened' % (log, tab))
    with open(tab, 'rb') as f: tb = f.read()
    b = len(tb); h = md5b(tb); del tb
    if (str(b), h) not in [r[3:5] for r in mine]: raise SystemExit('REFUSED: published_unverified: none of the %d "table ready" records of %s for day %s, builder %s, output %s has bytes %d, md5 %s' % (len(mine), log, day, me, ab, b, h))
    print('published: %s day %s bytes %d md5 %s equals a "table ready" record of %s written by this builder (%s)' % (ab, day, b, h, log, me))
    if os.path.lexists(tab + '.claim'): print('published: a claim is left beside the table (%s); reported, not removed' % (tab + '.claim'))
    return b, h


def cut_minute(hm):
    # erratum E1c: a prefix cut is HH:MM inside 04:00 .. 19:59; returns the minute index from 04:00
    if not isinstance(hm, str) or not _CUT.fullmatch(hm) or not (4 <= int(hm[:2]) <= 19) or int(hm[3:]) > 59: raise SystemExit('REFUSED: cut_malformed: %r is not a time HH:MM inside 04:00 .. 19:59' % (hm,))
    return int(hm[:2]) * 60 + int(hm[3:]) - OFF


def bounds_ok(first, last):
    # erratum E1c: the bounds of calcheck and coverage are two days YYYY-MM-DD, the first not after the last
    for x in (first, last):
        try: day_ok(x)
        except SystemExit: raise SystemExit('REFUSED: bound_malformed: %r is not a day written YYYY-MM-DD' % (x,))
    if first > last: raise SystemExit('REFUSED: bound_malformed: the first day %s is after the last day %s' % (first, last))


def out_ok(out):
    # erratum E1c: the output is refused before the build starts when its folder is missing or the file is already there
    if not os.path.isdir(os.path.dirname(os.path.abspath(out))): raise SystemExit('REFUSED: output_folder_missing: no folder %s' % os.path.dirname(os.path.abspath(out)))
    if os.path.lexists(out): raise SystemExit('REFUSED: output_exists: %s is already there; a build never writes over a table' % out)


def needed(cal, have, day, first=ARCH_FIRST):
    # erratum E1a: the bar files a target day needs = the HIST calendar sessions before it and the target. Every refusal is raised here, before any bar file is opened; `have` = the dates that have a bar file.
    day_ok(day)
    if sealed(day): raise SystemExit('REFUSED: sealed_day: %s is a sealed day' % day)
    i = bisect.bisect_left(cal, day)
    if i == len(cal) or cal[i] != day: raise SystemExit('REFUSED: not_a_trading_day: %s is not in the trading-day calendar' % day)
    if day in embargo(cal): raise SystemExit('REFUSED: history_in_sealed_window: %s is one of the first %d sessions after the sealed window (embargo, from the calendar alone)' % (day, HIST))
    hist = cal[max(0, i - HIST):i]
    if len(hist) < HIST or hist[0] < first: raise SystemExit('REFUSED: history_before_archive_start: %s has fewer than %d sessions since the first day of the archive %s' % (day, HIST, first))
    for d in hist:
        if sealed(d): raise SystemExit('REFUSED: history_in_sealed_window: %s needs the sealed day %s' % (day, d))
    for d in hist + [day]:
        if d not in have: raise SystemExit('REFUSED: bar_file_missing: %s needs the bar file of %s' % (day, d))
    return hist + [day]


def real_inputs(src, types, day, pins=(TYPES_MD5, SPLITS_MD5)):
    # erratum E1c, for build, prefix and keydiff, before any data file is opened: the bar source is a folder; the day's bar files by the calendar (needed); the types file and the splits file are there and are the pinned ones.
    day_ok(day)
    if not os.path.isdir(src): raise SystemExit('REFUSED: bar_source_not_a_folder: %s is not a folder (an inventory list serves coverage and calcheck only)' % src)
    need = needed(calendar(), bar_names(src)[0], day); sp = os.path.join(os.path.dirname(src.rstrip('/')), 'reference', 'splits.jsonl')
    if types == '-' or not os.path.isfile(types): raise SystemExit('REFUSED: types_file_missing: a build needs the pinned types file')
    if not os.path.isfile(sp): raise SystemExit('REFUSED: splits_file_missing: %s is absent; a build is not made without the split adjustment' % sp)
    if md5b(raw(types)) != pins[0]: raise SystemExit('REFUSED: types_file_md5: the types file differs from the pinned md5')
    sb = slurp_(sp)   # erratum E1i, 1: the ONE capture of the splits file; the pin is checked on it, the factors are parsed from it, the record reports its digest
    if md5b(sb) != pins[1]: raise SystemExit('REFUSED: splits_file_md5: the splits file differs from the pinned md5')
    return need, Splits(splits_parse(sb), sp, md5b(sb))   # erratum E1j, 2: the verified capture travels as this object; nothing reads the file by name again


def tick_inputs(day, tkd, pxd, mand):
    # erratum E1c: the manifest, the tick file and the price file of the target, each present and consistent, or a typed refusal. Nothing but the manifest is opened here (the two data files are only sized and hashed).
    tkf = os.path.join(tkd, day + '.parquet'); pxf = os.path.join(pxd, day + '.parquet'); mff = os.path.join(mand, day + '.json')
    if not os.path.isfile(mff): raise SystemExit('REFUSED: manifest_missing: no manifest %s' % mff)
    try:
        mb = slurp_(mff); mf = json.loads(mb.decode('utf-8')); fs = mf['files'] if isinstance(mf['files'], dict) else json.loads(mf['files'].replace("'", '"')); need_ = (mf['date'], mf['script_md5'], fs['tk']['bytes'], fs['px']['bytes'], fs['tk']['md5'], fs['px']['md5'])
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as x: raise SystemExit('REFUSED: manifest_unreadable: the manifest of %s is not JSON or lacks date, script_md5, files.tk or files.px with bytes and md5 (%s)' % (day, type(x).__name__))
    if not os.path.isfile(tkf): raise SystemExit('REFUSED: tick_file_missing: no tick file %s' % tkf)
    if not os.path.isfile(pxf): raise SystemExit('REFUSED: price_file_missing: no price file %s' % pxf)
    if mf['date'] != day: raise SystemExit('REFUSED: manifest_wrong_day: the manifest of %s names the day %r' % (day, mf['date']))
    if mf['script_md5'] != FROZEN or fs['tk']['bytes'] != os.path.getsize(tkf) or fs['px']['bytes'] != os.path.getsize(pxf): raise SystemExit('REFUSED: manifest_mismatch: manifest of %s does not name the frozen build or the file sizes differ' % day)
    got = {'tk': md5(tkf), 'px': md5(pxf)}
    for k in ('tk', 'px'):
        if fs[k]['md5'] != got[k]: raise SystemExit('REFUSED: manifest_mismatch: md5 of the %s file of %s differs from its manifest' % (k, day))
    got['manifest'] = md5b(mb)   # erratum E1i, 2: the digest of the bytes the manifest was parsed from
    return tkf, pxf, mff, mf, got


class Splits:
    # erratum E1j, 2 (version 14): the splits of a run: the factors parsed from the bytes whose md5 was compared with the pin, the path they were read from and their digest
    def __init__(self, factors, path, digest): self.factors = factors; self.path = path; self.digest = digest


def slurp_(path):
    with open(path, 'rb') as f: return f.read()


def splits_parse(b):
    # execution date -> [(ticker, factor)] from the bytes of a splits file; lines as a text read gives them (LF, CRLF, CR)
    SP = {}; ls = _EOL.split(b.decode('utf-8', 'replace'))
    if ls and ls[-1] == '': ls.pop()   # the piece after the last line end is not a line
    for i, l in enumerate(ls):
        try: j = json.loads(l); SP.setdefault(j['execution_date'], []).append((j['ticker'], j['split_from'] / j['split_to']))
        except (ValueError, KeyError, TypeError, ZeroDivisionError) as x: raise SystemExit('REFUSED: splits_file_unreadable: line %d of the splits file is not a split record (%s)' % (i + 1, type(x).__name__))
    return SP


def splits_fixture(src):
    # FOR THE FIXTURES ONLY: the splits beside a bar folder without the pin check (none = no adjustment). No mode of the builder calls this; a build, a prefix test and keydiff take the object real_inputs returns.
    sp = os.path.join(os.path.dirname(src.rstrip('/')), 'reference', 'splits.jsonl')
    if not os.path.exists(sp): return Splits({}, None, None)
    b = slurp_(sp); return Splits(splits_parse(b), sp, md5b(b))


def splits_taken(sp):
    if not isinstance(sp, Splits): raise SystemExit('REFUSED: splits_not_captured: a table is built only with the splits object real_inputs verified')
    return sp.factors


def _split(SP, dt, prev, mprev, hist):   # as wm2.days2: previous closes and the stored history are put on the split-adjusted scale on the execution date
    for tk_, fac in SP.get(dt, []):
        if tk_ in prev: prev[tk_] *= fac
        if tk_ in mprev: mprev[tk_] *= fac
        for h in hist:
            j = np.searchsorted(h['tick'], tk_)
            if j < len(h['tick']) and h['tick'][j] == tk_:
                for k in ('hi', 'lo', 'cl', 'pc'): h[k][j] *= fac


def history(src, tys, need, day2, sp):
    # the state the target's table needs (previous closes, market closes, HIST day summaries) from exactly the files need[:-1], with the target day's splits applied; returns it and the list of opened files
    SP = splits_taken(sp); prev = {}; mprev = {}; hist = collections.deque(maxlen=HIST); opened = []
    for dt in need[:-1]:
        _split(SP, dt, prev, mprev, hist); f = os.path.join(src, dt + '.csv.gz'); opened.append(f); _, lastc, summ, mclose = day2(f, prev, tys, hist, mprev, want=False)
        prev.update(lastc); mprev.update(mclose); hist.append(summ)
    _split(SP, need[-1], prev, mprev, hist); return (prev, mprev, hist), opened


def table(src, types, need, day2, sp):
    # the wm2 table of the target need[-1] from exactly the files of `need` (the list checked by needed(); no 45-day warm-up); returns the table and the list of opened files. day2 = wm2_l1.day2, or a spy in the fixtures.
    import pandas as pd
    tys = read_types(types) if types != '-' else None
    (prev, mprev, hist), opened = history(src, tys, need, day2, sp); f = os.path.join(src, need[-1] + '.csv.gz'); opened.append(f)
    t = day2(f, prev, tys, hist, mprev, want=True)[0]
    opened_ok(opened, need)
    return t, opened


def opened_ok(opened, need):
    if [os.path.basename(x)[:10] for x in opened] != list(need): raise SystemExit('REFUSED: bar_files_opened_differ: the bar files opened are not the list checked before the build')


def files_same(brec, opened):
    # erratum E1c: bytes and md5 of every bar file after the table is built equal those taken at the check before it
    if [tuple(x) for x in brec] != [(os.path.basename(f)[:10], os.path.getsize(f), md5(f)) for f in opened]: raise SystemExit('REFUSED: bar_files_opened_differ: a bar file changed between the check and the build')


def ticks_same(got, tkf, pxf, mf):
    # erratum E1i, 3: bytes and md5 of the tick and the price file after the last read of them equal those compared with the manifest before the first
    fs = mf['files'] if isinstance(mf['files'], dict) else json.loads(mf['files'].replace("'", '"'))
    for k, f in (('tk', tkf), ('px', pxf)):
        if os.path.getsize(f) != fs[k]['bytes'] or md5(f) != got[k]: raise SystemExit('REFUSED: tick_files_changed: the %s file %s changed between the check against the manifest and the end of its use' % (k, f))


def code_files(here, names):
    # (name, bytes, md5) of code files beside the builder, each from one read
    out = []
    for k in sorted(names):
        try: b = slurp_(os.path.join(here, k))
        except OSError: out.append((k, -1, 'unreadable')); continue
        out.append((k, len(b), md5b(b)))
    return out


_CODE = {}   # erratum E1i, 4: file name -> (bytes, md5) the run must find again: the pinned table code as checked before the import, a written module as the text that was written


def code_same(when, here=None, rec=None):
    # erratum E1i, 4: every code file of this run read again; anything but the bytes and md5 recorded for it refuses (here and rec: the fixtures' own folder and record)
    here = here or os.path.dirname(os.path.abspath(__file__)); rec = _CODE if rec is None else rec; now_ = code_files(here, rec); bad = [k for k, n, h in now_ if rec[k] != (n, h)]
    if bad: raise SystemExit('REFUSED: code_files_changed: %s: %s differ from the code that was checked before the import' % (when, ', '.join(bad)))
    return now_


def tickers_ok(tk_):
    if not all(x.isascii() for x in tk_) or any(tk_[i] >= tk_[i + 1] for i in range(len(tk_) - 1)): raise SystemExit('REFUSED: tickers_not_ascending: the tickers of the day are not ASCII in ascending order; the tie rule of erratum E2b would not hold')


def rows_ok(t, day):
    if t is None or len(t) == 0: raise SystemExit('REFUSED: no_table_rows: no table rows for %s' % day)


def gaps_refuse(day, gaps, sg):
    if gaps: raise SystemExit('REFUSED: price_row_missing: %s has %d minutes of table stocks where tk has a quote update or a trade and px has no row (erratum G)' % (day, gaps))
    if sg: raise SystemExit('REFUSED: early_quote_price_row_missing: %s has %d table stocks whose chosen quote update before 04:00 has no px row (erratum E9a)' % (day, sg))


def pins_ok(here, pin):
    for k in sorted(pin):
        if not os.path.isfile(os.path.join(here, k)): raise SystemExit('REFUSED: table_code_missing: no %s beside the builder' % k)
    rec = code_files(here, pin); got = {k: h for k, n, h in rec}
    if got != pin: raise SystemExit('REFUSED: table_code_md5: table code differs from the pinned md5: %s' % {k: v for k, v in got.items() if v != pin[k]})
    return {k: (n, h) for k, n, h in rec}


def patched(src, patches):
    new = src
    for nm, old, rp in patches:
        if src.count(old) != 1 or new.count(old) != 1: raise SystemExit('REFUSED: table_code_replacement: the declared replacement "%s" does not apply exactly once' % nm)
        new = new.replace(old, rp)
    if src.count('N2 = %d' % N2_PIN) != 1: raise SystemExit('REFUSED: table_code_settings: the second list depth of the table code is not %d' % N2_PIN)
    return new


def settings_ok(w):
    if w.NDAYS != HIST or tuple(w.SEAL) != SEAL or w.HARD_LAST != HARD_LAST: raise SystemExit('REFUSED: table_code_settings: history length or sealed window differ between builder and table code')
    if w.TOPN != TOPN_PIN or w.LAG != 0 or w.REF is not None or w.TK is not None or w.TSPAN is not None: raise SystemExit('REFUSED: table_code_settings: the table code does not run with its defaults')


def env_refuse():
    # run rule: each variable the table code reads is asked by its name; a set one refuses the run
    for k in ENVREF:
        if os.environ.get(k) is not None: raise SystemExit('REFUSED: table_variable_set: the variable %s is set in this process; the label builder runs only with the defaults of the table code' % k)


def load_wm2():
    # the pinned table code from the builder's folder, with the declared replacements: the row rule (erratum B), the list rule in three places (erratum E2b) and the capture for the prefix test
    env_refuse(); here = os.path.dirname(os.path.abspath(__file__)); _CODE.update(pins_ok(here, PIN))
    new = patched(open(os.path.join(here, 'wm2.py')).read(), (('row rule',) + ROWPATCH,) + LISTPATCH + (('capture',) + CAPPATCH, ('types reader',) + TYPEPATCH))
    p = os.path.join(here, 'wm2_l1.py')
    if not os.path.exists(p) or open(p).read() != new:
        with open(p, 'w') as f: f.write(new)
    if here not in sys.path: sys.path.insert(0, here)
    import wm2_l1
    if not hasattr(wm2_l1, '_CAP'): wm2_l1._CAP = {}
    wm2_l1.load = bar_load   # erratum E1d, 1: the declared replacement of the bar read
    wm2_l1._L1_READ_TYPES = read_types   # erratum E1g: the one types reader
    settings_ok(wm2_l1)
    nb_ = new.encode('utf-8'); _CODE['wm2_l1.py'] = (len(nb_), md5b(nb_)); code_same('after the table code was loaded')   # erratum E1i, 4: the file that was imported is the text that was written, and the pinned files are still the pinned bytes
    return wm2_l1, md5b(nb_)


def settings(w):
    from wide import SESS
    return 'list depth TOPN %d, second depth N2 %d, minimum price MINP %.2f, LAG %d, reference columns off, tick columns off, TSPAN off, history days %d (at least %d, at least %d returns), start windows and flatten minutes %s' % (w.TOPN, N2_PIN, w.MINP, w.LAG, w.NDAYS, w.MINH, w.MINR, SESS.tolist())


def rule_lists(cap, m):
    # errata E2b / E2c, the stated rule computed apart from the table code, for the fixtures: the names of the ten component lists (COMP) of minute m, from the captured values
    tick = cap['tick']
    def top(val, known, desc, N):
        ids = sorted((i for i in range(len(tick)) if known[i]), key=lambda i: ((-val[i] if desc else val[i]), tick[i])); return {tick[i] for i in ids[:N]}
    c = cap['chg'][:, m]; kn = ~np.isnan(c); v = cap['cv'][:, m]; out = {0: top(c, kn, True, TOPN_PIN), 1: top(c, kn, True, N2_PIN), 2: top(c, kn, False, TOPN_PIN), 3: top(c, kn, False, N2_PIN), 4: top(v, v > 0, True, TOPN_PIN), 5: top(v, v > 0, True, N2_PIN)}
    for k in (6, 7, 8, 9): out[k] = set()
    if m >= 720:
        c = cap['chg2'][:, m - 720]; kn = ~np.isnan(c); out.update({6: top(c, kn, True, TOPN_PIN), 7: top(c, kn, True, N2_PIN), 8: top(c, kn, False, TOPN_PIN), 9: top(c, kn, False, N2_PIN)})
    return out


def keys_of(cap, MINP):
    # the row rule (errata B, E2a) restated from a capture: the watch of a session starts at the first minute of its start window with list membership and a bar closing at MINP or more; rows at every bar from there to the flatten minute - 2
    from wide import SESS
    has = cap['has']; ok = cap['inl'] & has & (np.nan_to_num(cap['c']) >= MINP); tick = cap['tick']; out = set()
    for a, b, fl in SESS:
        w = ok[:, a:b + 1]; first = a + w.argmax(axis=1)
        for i in np.flatnonzero(w.any(axis=1)):
            for m in np.flatnonzero(has[i, first[i]:fl - 1]) + first[i]: out.add((tick[i], int(m)))
    return out


def prefix_cmp(full, cut, c):
    # full, cut = (capture, set of row keys) of the whole day and of the day cut after minute c. Returns the counts of differences for the minutes up to c: every component list at both depths, the two unions, the row keys.
    (cf, kf), (cc, kc) = full, cut; pos = {t: i for i, t in enumerate(cf['tick'])}; miss = [t for t in cc['tick'] if t not in pos]; j = np.array([pos[t] for t in cc['tick'] if t in pos], np.int64); late = np.ones(len(cf['tick']), bool); late[j] = False
    r = dict(names_only_in_cut=len(miss), names_first_printed_after_cut=int(late.sum())); cd = {}; cl = 0
    for k in ('inl', 'in2'): r[k + '_differs'] = int((cf[k][j][:, :c + 1] != cc[k][:, :c + 1]).sum()) if not miss else 10 ** 9; r[k + '_late_name_listed'] = int(cf[k][late][:, :c + 1].sum())
    for k, nm in enumerate(COMP):
        x = int((cf['comp'][k][j][:, :c + 1] != cc['comp'][k][:, :c + 1]).sum()) if not miss else 10 ** 9; cl += int(cf['comp'][k][late][:, :c + 1].sum())
        if x: cd[nm] = x
    r['component_lists_compared'] = len(COMP); r['component_memberships_differ'] = sum(cd.values()); r['components_that_differ'] = cd or 'none'; r['component_late_name_listed'] = cl
    a = {x for x in kf if x[1] <= c}; b = {x for x in kc if x[1] <= c}; r['row_keys_to_cut'] = len(a); r['row_keys_differ'] = len(a ^ b)
    r['bad'] = r['names_only_in_cut'] + r['inl_differs'] + r['in2_differs'] + r['inl_late_name_listed'] + r['in2_late_name_listed'] + r['component_memberships_differ'] + cl + r['row_keys_differ']; return r


def one_table(w, path, st, tys):
    w._CAP.clear(); t = w.day2(path, st[0], tys, st[2], st[1], want=True)[0]; cap = dict(w._CAP); tk_ = cap['tick']; tickers_ok(tk_)
    return cap, (set(zip(t.sym.values, t.m.values.astype(int))) if t is not None else set()), t


def cut_file(path, c, out):
    # the day's bar file with the bars after minute c removed (c = minute index from 04:00)
    import pandas as pd
    d = pd.read_csv(path, dtype={'ticker': str}, na_filter=False); ts = pd.to_datetime(d.window_start, unit='ns', utc=True).dt.tz_convert('America/New_York'); m = ts.dt.hour * 60 + ts.dt.minute - OFF
    d[m <= c].to_csv(out, index=False, compression='gzip')


def prefix(src, types, day, *cuts):
    # errata E2b / E2c on a real day: the table from the bars cut after each minute against the table of the whole day
    import pandas as pd, tempfile
    env_refuse(); cm = [cut_minute(hm) for hm in cuts]; need, sp = real_inputs(src, types, day); tys = read_types(types); bars_readable(src, need); w, wmd = load_wm2()
    st, opened = history(src, tys, need, w.day2, sp); f = os.path.join(src, day + '.csv.gz'); full = one_table(w, f, st, tys)[:2]; bad = 0
    print('prefix test %s: builder md5 %s, wm2_l1.py md5 %s; %s; whole day: names %d, row keys %d' % (day, md5(os.path.abspath(__file__)), wmd, settings(w), len(full[0]['tick']), len(full[1])))
    with tempfile.TemporaryDirectory() as td:
        for hm in cuts:
            c = cut_minute(hm); p = os.path.join(td, day + '.csv.gz'); cut_file(f, c, p); forget(p); r = prefix_cmp(full, one_table(w, p, st, tys)[:2], c); bad += r['bad']
            print('  cut after %s (minute %d): %s' % (hm, c, r))
    code_same('at the end of the run'); print('prefix test: table code read again at the end of the run: unchanged (erratum E1i, 4)')
    print('prefix test: differences %d' % bad)
    if bad: raise SystemExit('PREFIX TEST FAILED')


def load_old_rule(which, pins=(V2_WM2L1, V3_WM2L1)):
    # the table code as an earlier builder ran it, with the capture and the component matrices added (no value changes), for `keydiff`: 'v2' = builder 5561d4e3 (row rule only, lists as inherited), 'v3' = builder e6f36ed3
    # (ties by ticker, the guard still skipping Most Active). The text is rebuilt from the pinned wm2.py and must have the md5 of the file that builder wrote before anything is added.
    import importlib
    here = os.path.dirname(os.path.abspath(__file__))
    if not os.path.isfile(os.path.join(here, 'wm2.py')): raise SystemExit('REFUSED: table_code_missing: no wm2.py beside the builder')
    t = open(os.path.join(here, 'wm2.py')).read().replace(*ROWPATCH)
    def step(old, new):
        nonlocal t
        if t.count(old) != 1: raise SystemExit('REFUSED: table_code_replacement: a replacement of the old-rule table code does not apply exactly once')
        t = t.replace(old, new)
    if which == 'v2':
        if hashlib.md5(t.encode()).hexdigest() != pins[0]: raise SystemExit('REFUSED: old_rule_md5: the old-rule table code is not the one builder 5561d4e3 wrote')
        step(_AL, _ALK); step(_HA + _GL, _HA + _GL + _KGO(0)); step(_MAO, _MAOK); step(_HC + _GL, _HC + _GL + _KGO(6)); step(*CAPPATCH)
    elif which == 'v3':
        step(_HA + _GL, _HA + _GLN); step(_MAO, _MAN); step(_HC + _GL, _HC + _GLN); step(CAPPATCH[0], _CAP3)
        if hashlib.md5(t.encode()).hexdigest() != pins[1]: raise SystemExit('REFUSED: old_rule_md5: the old-rule table code is not the one builder e6f36ed3 wrote')
        step(_AL, _ALK); step(_HA + _GLN, _HA + _GLN + _KGN(0)); step(_MAN, _MAN + _KMN); step(_HC + _GLN, _HC + _GLN + _KGN(6)); step(_CAP3, CAPPATCH[1])
    else: raise SystemExit('REFUSED: old_rule_unknown: the old rule is v2, v3 or own')
    nm = 'wm2_l1_old_' + which; p = os.path.join(here, nm + '.py')
    if not os.path.exists(p) or open(p).read() != t:
        with open(p, 'w') as f: f.write(t)
    if here not in sys.path: sys.path.insert(0, here)
    w = importlib.import_module(nm); w._CAP = {}; w.load = bar_load
    tb_ = t.encode('utf-8'); _CODE.update(pins_ok(here, {'wm2.py': PIN['wm2.py']})); _CODE[nm + '.py'] = (len(tb_), md5b(tb_)); code_same('after the old-rule table code was loaded'); return w


def lost_ticker(t):
    # erratum E1g (version 11): True when pandas' default csv reader turns the ticker text into a missing value, which is how the reference builders (up to 13ba39fa) read the types file. Computed, not listed;
    # the same function as the comparator's (a fixture compares the two).
    import io, pandas as pd
    return isinstance(t, str) and t != '' and ',' not in t and '\n' not in t and '"' not in t and bool(pd.read_csv(io.StringIO('ticker\n%s\n' % t), dtype=str).ticker.isna().iloc[0])


def align(co, tick):
    # version 11: a capture re-indexed to the ticker order `tick` of another capture of the same day (a ticker the capture lacks: on no list, no bar, no value). A ticker of `co` that `tick` lacks is refused.
    pos = {t: i for i, t in enumerate(co['tick'])}; miss = [t for t in co['tick'] if t not in set(tick)]
    if miss: raise SystemExit('KEYDIFF FAILED: the reference capture holds %d tickers the new capture lacks; first: %s' % (len(miss), miss[0]))
    ix = np.array([pos.get(t, -1) for t in tick]); g = ix >= 0; out = dict(tick=list(tick))
    for k, v in co.items():
        if k == 'tick': continue
        ax = 1 if k == 'comp' else 0; sh = list(v.shape); sh[ax] = len(tick); a = np.zeros(sh, v.dtype) if v.dtype == np.bool_ else np.full(sh, np.nan, v.dtype)
        if ax == 0: a[g] = v[ix[g]]
        else: a[:, g] = v[:, ix[g]]
        out[k] = a
    return out


def global_swaps(cn, co, lost):
    # erratum E1g, item 9 (version 11): over every list component that feeds the row rule (the first-depth lists: gain, loss, Most Active, post-market gain, post-market loss) and every minute of the day, the
    # (component, minute) pairs whose membership differs between the aligned reference capture and the new one. A pair is a swap when the names only in the new list are all lost tickers and the names only in the
    # reference list are exactly as many. Returns pairs compared, pairs that differ, names displaced in swaps, pairs of any other kind, and the first such pairs.
    lm = np.array([t in lost for t in cn['tick']]); comp = dif = disp = bad = 0; first = []
    for k in (0, 2, 4, 6, 8):
        on = cn['comp'][k] & ~co['comp'][k]; orf = co['comp'][k] & ~cn['comp'][k]; a = on.sum(axis=0); b = orf.sum(axis=0); nl = (on & ~lm[:, None]).sum(axis=0); d = (a + b) > 0; x = d & ((nl > 0) | (a != b))
        comp += on.shape[1]; dif += int(d.sum()); disp += int(b[d & ~x].sum()); bad += int(x.sum()); first += [(COMP[k], int(m)) for m in np.flatnonzero(x)[:3]]
    return comp, dif, disp, bad, first[:3]


def key_reasons(cn, co, add, rem, MINP, TOPN, lost=frozenset()):
    # cn, co = captures of the new-rule and the old-rule table of the same day; add, rem = row keys only in the new / only in the old build. Returns one row per key with its reason, and the keys by stock-session.
    # A list is named only when the membership of THAT component differs between the two rules at the minute where the watch start changed, and that difference is itself explained: a tie standing across the cut of the list,
    # or (E2c) the Most Active list in a minute where no name has a known change. Anything else is UNEXPLAINED.
    from wide import SESS
    tick = cn['tick']; pos = {t: i for i, t in enumerate(tick)}; has = cn['has']; okn = cn['inl'] & has & (np.nan_to_num(cn['c']) >= MINP); oko = co['inl'] & has & (np.nan_to_num(cn['c']) >= MINP)
    def tie(val, known, i, N):   # a tie stands across the cut of a list and the stock is in it: strictly better names < N < strictly better + tied
        if not known[i]: return False
        better = int((known & (val > val[i])).sum()); tied = int((known & (val == val[i])).sum()); return better < N < better + tied
    def swap(k, i, m):
        # erratum E1g (version 11): the new list of component k at minute m is the reference list with ONE swap: a lost ticker in, the stock i out (set equality). Returns the lost ticker, or ''.
        ln = set(np.flatnonzero(cn['comp'][k][:, m]).tolist()); lo_ = set(np.flatnonzero(co['comp'][k][:, m]).tolist()); inn = ln - lo_; out_ = lo_ - ln
        return tick[next(iter(inn))] if (out_ == {i} and len(inn) == 1 and tick[next(iter(inn))] in lost) else ''
    def why(i, m):
        r = []; c = cn['chg'][:, m]; k_ = ~np.isnan(c); v = cn['cv'][:, m]; c2 = cn['chg2'][:, m - 720] if m >= 720 else np.full(len(tick), np.nan); k2 = ~np.isnan(c2)
        for k, val, known in ((0, c, k_), (2, -c, k_), (4, v, v > 0), (6, c2, k2), (8, -c2, k2)):
            if cn['comp'][k, i, m] == co['comp'][k, i, m]: continue
            if k == 4 and not k_.any(): r.append('E2c: the Most Active list in a minute where no name has a known change')
            elif tie(val, known, i, TOPN): r.append('tie at the cut of the %s list, broken by ticker' % COMP[k])
            elif swap(k, i, m): r.append('E1g: place on the %s list taken by %s, a ticker the reference reader lost' % (COMP[k], swap(k, i, m)))
            else: return ''
        return ' + '.join(r)
    rows = []; groups = collections.defaultdict(set)
    for sym, m in add | rem: groups[(sym, int(np.searchsorted(SESS[:, 2], m, side='right')))].add((sym, m))
    for (sym, se), ks in sorted(groups.items()):
        i = pos[sym]; a, b, fl = SESS[se]; qo = np.flatnonzero(oko[i, a:b + 1]); qn_ = np.flatnonzero(okn[i, a:b + 1]); q_old = a + qo[0] if len(qo) else None; q_new = a + qn_[0] if len(qn_) else None
        if q_old is None and q_new is None: lo = None; okk = False
        else:
            lo = min(x for x in (q_old, q_new) if x is not None); hi = max(x if x is not None else fl for x in (q_old, q_new)); exp = {(sym, m) for m in range(lo, min(hi, fl - 1)) if has[i, m] and m <= fl - 2}
            kind = 'added' if (q_old is None or (q_new is not None and q_new < q_old)) else 'removed'; rs = why(i, lo) if (cn['inl'][i, lo] != co['inl'][i, lo]) else ''
            if sym in lost: rs = 'E1g: the stock was not in the reference universe (its ticker is read as a missing value by the reference reader)' if (q_old is None and kind == 'added' and not co['has'][i].any()) else ''
            okk = exp == ks and ks <= (add if kind == 'added' else rem) and rs != ''
        for k in sorted(ks): rows.append(dict(sym=sym, m=k[1], session=('pre', 'regular', 'post')[se], change=('added' if k in add else 'removed'), watch_start_old=q_old, watch_start_new=q_new, reason=('%s, at minute %d' % (rs, lo)) if okk else 'UNEXPLAINED'))
    return rows, groups


def keydiff(src, types, day, oldf, newf, outcsv, which):
    # errata E2b / E2c, the row keys added and removed by the list rule on one day, each with its reason (key_reasons). `which` = the rule of the old build: v2, v3, or own = this builder's own table code.
    # erratum E1g (version 11): the reference side reads the types as the reference builders did: read_types minus the tickers the default reader loses (computed), with its own history; it must give exactly the old keys.
    # The two captures are aligned by ticker before they are compared.
    import pandas as pd, duckdb
    env_refuse(); need, sp = real_inputs(src, types, day); tys = read_types(types); bars_readable(src, need); w, _ = load_wm2(); wo = w if which == 'own' else load_old_rule(which)
    lost = sorted(t for t in tys if lost_ticker(t)); tyr = tys - set(lost); f = os.path.join(src, day + '.csv.gz')
    st, _ = history(src, tys, need, w.day2, sp); cn, kn, _ = one_table(w, f, st, tys); sto, _ = history(src, tyr, need, wo.day2, sp); co, ko, _ = one_table(wo, f, sto, tyr); co = align(co, cn['tick']); con = duckdb.connect()
    rd = lambda x: set(map(tuple, con.execute("SELECT sym, CAST(m AS INTEGER) FROM read_parquet('%s')" % x).fetchall())); fo, fn = rd(oldf), rd(newf)
    print('keydiff %s: builder md5 %s; old rule %s; reference types = the types file minus the tickers the default reader loses (%d: %s); old build keys %d, new build keys %d; the old-rule table rebuilt here has the old build keys: %s; the new-rule table has the new build keys: %s; the row rule restated from the capture gives the new keys: %s' % (day, md5(os.path.abspath(__file__)), which, len(lost), ', '.join(lost) or 'none', len(fo), len(fn), ko == fo, kn == fn, keys_of(cn, w.MINP) == kn))
    if ko != fo or kn != fn: raise SystemExit('KEYDIFF FAILED: a table rebuilt here does not have the keys of its build')
    if which == 'own':   # the same table code on both sides: every membership difference of the day must be a swap with a lost ticker (not for v2 / v3, whose list code differs)
        g = global_swaps(cn, co, frozenset(lost)); print('list memberships, reference against new, same table code: (component, minute) pairs compared %d, pairs that differ %d, names displaced by a lost ticker %d, pairs of any other kind %d%s' % (g[0], g[1], g[2], g[3], (' (first: %s)' % g[4]) if g[3] else ''))
        if g[3]: raise SystemExit('KEYDIFF FAILED: %d (component, minute) pairs differ by something other than a lost ticker in and as many names out' % g[3])
    add = fn - fo; rem = fo - fn; rows, groups = key_reasons(cn, co, add, rem, w.MINP, w.TOPN, frozenset(lost))
    R = pd.DataFrame(rows, columns=['sym', 'm', 'session', 'change', 'watch_start_old', 'watch_start_new', 'reason']); R.to_csv(outcsv, index=False); un = int((R.reason == 'UNEXPLAINED').sum()) if len(R) else 0
    print('list memberships (stock-minutes on a list) that differ between the old and the new rule: %d of %d; in the second-depth lists: %d; by component: %s' % (int((cn['inl'] != co['inl']).sum()), int(cn['inl'].sum()), int((cn['in2'] != co['in2']).sum()), {COMP[k]: int((cn['comp'][k] != co['comp'][k]).sum()) for k in range(len(COMP)) if (cn['comp'][k] != co['comp'][k]).any()} or 'none'))
    nk = ~(~np.isnan(cn['chg'])).any(axis=0); vol = (cn['cv'] > 0).any(axis=0); mm = np.flatnonzero(nk & vol)
    print('erratum E2c: minutes of the day where no name has a known change and at least one name has volume: %d%s; Most Active memberships in those minutes: new rule %d, old rule %d; row keys whose reason is E2c: %d' % (len(mm), (' (%s)' % ' '.join(str(int(x)) for x in mm[:40])) if len(mm) else '', int(cn['comp'][4][:, mm].sum()), int(co['comp'][4][:, mm].sum()), int(R.reason.str.contains('E2c').sum()) if len(R) else 0))
    print('row keys added %d (stock-sessions %d), removed %d (stock-sessions %d)' % (len(add), len({g for g, ks in groups.items() if ks <= add}), len(rem), len({g for g, ks in groups.items() if ks <= rem})))
    if len(R):
        R['why'] = R.reason.str.replace(r', at minute \d+', '', regex=True); print(R.groupby(['change', 'session', 'why']).size().to_string())
    code_same('at the end of the run'); print('keydiff: table code read again at the end of the run: unchanged (erratum E1i, 4)')
    print('keys with their reasons saved: %s; UNEXPLAINED keys: %d' % (outcsv, un))
    if un: raise SystemExit('KEYDIFF FAILED: unexplained keys')


def calcheck(explf, *args):
    # errata E1a / E1b, names only: each source (a folder or an inventory list) with its DECLARED first and last day. Printed and counted: calendar days inside the bounds without a bar file, bar files that are not calendar days,
    # bar files outside the bounds, names that are not bar-file names. A difference is explained only when the explained-list names it (one line: the date or the exact name, then the reason); any other one ends with an error.
    cal = calendar(); cs = set(cal); ex = {}; un = 0
    if explf != '-':
        for l in open(explf):
            if l.strip() and not l.startswith('#'): k, _, v = l.strip().partition(' '); ex[k] = v
    if len(args) % 3 or not args: raise SystemExit('usage: calcheck <explained-list or -> <folder or inventory list> <first day> <last day> ...')
    print('calendar %s md5 %s: %d trading days %s .. %s' % (CALF, CAL_MD5, len(cal), cal[0], cal[-1]))
    for src, first, last in zip(args[0::3], args[1::3], args[2::3]):
        bounds_ok(first, last)
        have, other = bar_names(src); D = [('calendar day inside the bounds without a bar file', sorted(d for d in cal if first <= d <= last and d not in have)), ('bar file that is not a calendar day', sorted(d for d in have - cs if first <= d <= last)),
                                         ('bar file outside the declared bounds', sorted(d for d in have if not (first <= d <= last))), ('name that is not a bar-file name', other)]
        print('%s, declared bounds %s .. %s: bar files %d, calendar days inside the bounds %d' % (src, first, last, len(have), sum(first <= d <= last for d in cal)))
        for nm, v in D:
            print('  %s: %d' % (nm, len(v)))
            for x in v:
                print('    %s: %s' % (x, ex.get(x, 'UNEXPLAINED'))); un += x not in ex
    print('calendar check: unexplained differences %d' % un)
    if un: raise SystemExit('CALENDAR CHECK FAILED')


# ---------------------------------------------------------------- fixtures
def blank(n=1):
    f = lambda v: np.full((n, NM), float(v)); b = lambda: np.zeros((n, NM), bool)
    M = dict(haspx=np.ones((n, NM), bool), qn=np.ones((n, NM)), qbid=f(10.0), qask=f(10.02), qbsl=f(100), qasl=f(100), pstE=f(1), bbE=b(), abE=b(), c20E=b(), pqtE=np.ones((n, NM), np.int64),
             tlo=f(10.0), thi=f(10.02), xlos=f(30), xhis=f(30), tn=np.ones((n, NM)), pblof=f(10.0), pblofs=f(40), pblo5=f(10.0), pblo5s=f(40), close=f(10.01))
    for k in ('qb', 'qa', 'qbz', 'qaz', 'pst'): M[k] = {S: f({'qb': 10.0, 'qa': 10.02, 'qbz': 100, 'qaz': 100, 'pst': 1}[k]) for S in MARKS}
    for k in ('bb', 'ab', 'c20'): M[k] = {S: b() for S in MARKS}
    M['pqt'] = {S: np.ones((n, NM), np.int64) for S in MARKS}
    return M


def quiet(M, k, i=0):   # minute k has no px row and no quote update (it may still not have a trade)
    M['haspx'][i, k] = False; M['qn'][i, k] = 0; M['tn'][i, k] = 0
    for a in ('tlo', 'thi', 'close'): M[a][i, k] = np.nan


def one(M, q=400, sess=1, vu=0.01 / math.sqrt(30)):
    o = run(derive(M), [0], [q], [sess], [vu])[0]; return {k: o[j] for k, j in O.items()}


def test():
    _KEEP[0] = False; _ONCE.clear()   # erratum E1h: the fixtures rewrite files under one name; the kept buffer has its own fixtures below
    import inspect; src_ = open(os.path.abspath(__file__)).read()
    eq = lambda a, b: abs(a - b) < 1e-12; n = 0
    def ck(c, what):
        nonlocal n; n += 1
        if not c: raise SystemExit('FIXTURE FAILED: ' + what)
    q = 400; e = 401; u = 0.01   # row minute 10:40, entry at 10:41:05, unit 1%: L = 9.9198, U = 10.2204
    # 1 plain row: bought at the ask of the 5 s mark of minute q+1, sold at the bid of the 5 s mark of minute q+31
    r = one(blank()); ck(r['st'] == 0 and eq(r['A'], 10.02) and eq(r['h_val'], 10.0 / 10.02 - 1) and r['h_ret_ok'] == 1 and r['h_min'] == q + 31 and r['h_sec'] == 2 and r['cut'] == 0, 'plain h30e')
    ck(eq(r['d_val'], 10.0 / 10.02 - 1) and r['d_n'] == 30, 'plain d30e'); ck(r['t_trail'] == 0 and r['t_dec'] == 709 and r['t_min'] == 710 and eq(r['t_val'], 10.0 / 10.02 - 1), 'plain t10e = flatten')
    ck(r['b_cls'] == 0 and eq(r['b_val'], (10.0 / 10.02 - 1) / u) and r['b_flags'] == 0 and r['cls_ok'] == 1 and eq(r['b_opt'], r['b_val']) and eq(r['b_stress'], r['b_val']), 'plain b30e = time')
    # 2 entry tests: crossed fails; ask only fills; ask indicator 3 fails; condition 20 fails; indicator 2 (bid side) does not stop a buy; a later mark never matters
    M = blank(); M['pst'][5][0, e] = 5; r = one(M); ck(r['st'] == 1 and r['est'] == 5 and r['ereason'] == 2 and np.isnan(r['h_val']) and np.isnan(r['b_val']), 'crossed entry')
    M = blank(); M['pst'][5][0, e] = 13; M['qb'][5][0, e] = 0; r = one(M); ck(r['st'] == 0 and r['est'] == 3 and r['ebid'] == 0 and r['b_flags'] == 0, 'ask-only entry fills; no bid = no bid flags')
    M = blank(); M['qb'][5][0, e] = 9.9; r = one(M); ck(int(r['b_flags']) == F_BIDL | F_SPR, 'entry bid under L and spread over u (one condition under two names: (A - bid) / A >= u is bid <= L; the two bits are always equal)');
    for b_ in (9.0, 9.9197, 9.9199, 9.95, 10.02): M['qb'][5][0, e] = b_; f_ = int(one(M)['b_flags']); ck(((f_ & F_BIDL) > 0) == ((f_ & F_SPR) > 0), 'the two entry-bid bits are equal')
    M['qb'][5][0, e] = 9.93; ck(one(M)['b_flags'] == 0, 'entry bid inside')
    M = blank(); M['ab'][5][0, e] = True; ck(one(M)['ereason'] == 4, 'ask indicator'); M = blank(); M['c20'][5][0, e] = True; ck(one(M)['ereason'] == 5, 'condition 20 entry')
    M = blank(); M['bb'][5][0, e] = True; ck(one(M)['st'] == 0, 'bid indicator at a buy'); M = blank(); M['qaz'][5][0, e] = 0; ck(one(M)['ereason'] == 3, 'no ask size')
    M = blank(); M['pst'][15][0, e] = 5; M['pst'][0][0, e] = 5; ck(one(M)['st'] == 0, 'other marks of the entry minute')
    # 3 SALE schedule: 5 s invalid -> 15 s; both -> 5 s of the next minute; the 0 s and 2 s marks are not sale marks
    M = blank(); M['bb'][5][0, q + 31] = True; M['qb'][15][0, q + 31] = 9.99; r = one(M); ck(r['h_sec'] == 3 and r['h_min'] == q + 31 and eq(r['h_val'], 9.99 / 10.02 - 1), 'sale at 15 s')
    M['c20'][15][0, q + 31] = True; M['qb'][5][0, q + 32] = 9.98; M['qb'][0][0, q + 32] = 11.0; r = one(M); ck(r['h_min'] == q + 32 and r['h_sec'] == 2 and eq(r['h_val'], 9.98 / 10.02 - 1), 'sale at the next minute')
    # 4 no fill to the end of the session: marked at the last bid above zero (crossed quotes cannot fill); no bid at all = no value
    M = blank()
    for S in MARKS: M['pst'][S][0, q + 31:] = 5
    M['pstE'][0, q + 30:] = 5; M['qb'][5][0, 719] = 9.5; r = one(M); ck(r['h_ret_ok'] == 0 and r['h_mark_ok'] == 1 and eq(r['h_val'], 9.5 / 10.02 - 1) and r['h_min'] == 719, 'inventory mark')
    for S in MARKS: M['qb'][S][0, e:] = 0
    M['qbid'][0, e:] = 0; r = one(M); ck(r['h_mark_ok'] == 0 and r['h_ret_ok'] == 0 and np.isnan(r['h_val']), 'no bid at all'); ck(r['st'] == 0, 'entry still filled')
    # 5 cut at the flatten: row 15:30 (k 690): decision at F = close of minute 709, attempts 15:50:05 ...; pre-market ends at 09:29:05
    r = one(blank(), q=690); ck(r['cut'] == 1 and r['h_min'] == 710 and int(r['b_flags']) & F_CUT and r['b_dec'] == 709, 'cut')
    M = blank(); M['bb'][5][0, 328] = True; M['bb'][15][0, 328] = True; r = one(M, q=320, sess=0); ck(r['h_min'] == 329 and int(r['b_flags']) & F_WATCH, 'pre-market schedule')
    M['bb'][5][0, 329] = True; M['qb'][5][0, 330] = 12.0; r = one(M, q=320, sess=0); ck(r['h_ret_ok'] == 0 and r['h_mark_ok'] == 1, 'nothing is sold after the session')
    # 6 reader rule: a quiet sale minute reads the tk LAST quote of the latest earlier minute with an update (not its 15 s mark), with the codes of its terminal mark; no terminal mark = not fillable
    M = blank(); quiet(M, q + 31); M['qbid'][0, q + 30] = 9.9; M['qb'][15][0, q + 30] = 10.5; r = one(M); ck(r['h_min'] == q + 31 and eq(r['h_val'], 9.9 / 10.02 - 1), 'quiet minute reads the last quote')
    M['bbE'][0, q + 30] = True; r = one(M); ck(r['h_min'] == q + 32, 'terminal code invalidates the carried bid'); M['bbE'][0, q + 30] = False; M['pstE'][0, q + 30] = np.nan; r = one(M); ck(r['h_min'] == q + 32, 'codes unknown = no fill')
    M = blank(); quiet(M, e); M['pstE'][0, q] = np.nan; r = one(M); ck(r['st'] == 1 and r['ereason'] == 1, 'entry on a quote without codes')
    M = blank(); M['haspx'][0, :e + 1] = False; M['qn'][0, :e + 1] = 0; r = one(M); ck(r['st'] == 1 and r['est'] == 0, 'no quote yet today')
    # 7 d30e: entry minute from second 5 (pblo5), later minutes pblof, quiet minute the standing bid, a bar's trade low is not used; nothing after the window
    M = blank(); M['pblof'][0, e] = 9.0; M['pblo5'][0, e] = 9.95; M['pblof'][0, e + 3] = 9.9; M['tlo'][0, e + 5] = 9.93; M['pblof'][0, q + 31] = 5.0; r = one(M); ck(eq(r['d_val'], 9.9 / 10.02 - 1), 'd30e lows')
    M = blank(); quiet(M, e + 2); M['qbid'][0, e + 1] = 9.7; r = one(M); ck(eq(r['d_val'], 9.7 / 10.02 - 1), 'd30e quiet minute'); M['pblof'][0, e + 4] = np.nan; ck(one(M)['d_n'] == 29, 'd30e empty low')
    # 8 t10e: decision at the first close at or under 0.90 x peak (peak takes the entry price and the closes from the entry minute); a minute without a bar decides nothing; it is not limited to 30 minutes
    M = blank(); M['close'][0, e + 50] = 9.0179; r = one(M); ck(r['t_trail'] == 1 and r['t_dec'] == e + 50 and r['t_min'] == e + 51 and r['t_sec'] == 2, 't10e from the entry price')
    M = blank(); M['close'][0, e + 50] = 9.0181; ck(one(M)['t_trail'] == 0, 't10e just above'); M = blank(); M['close'][0, e + 2] = 12.0; M['close'][0, e + 3:e + 9] = 11.5; M['close'][0, e + 9] = 10.8; r = one(M); ck(r['t_dec'] == e + 9, 't10e from a later peak')
    M = blank(); M['close'][0, q] = 20.0; ck(one(M)['t_trail'] == 0, 'a close before the entry minute is not a peak')
    # 9 b30e lower limit: trade low at or under L; fill at the 0 s mark of the next minute, search 2, 5, 15 s; lower before upper inside a minute; value raw
    M = blank(); k = e + 4; M['tlo'][0, k] = 9.9197; M['qb'][0][0, k + 1] = 9.80; M['qbid'][0, k] = 11.0; r = one(M); ck(r['b_cls'] == 2 and r['b_dec'] == k and r['b_min'] == k + 1 and r['b_sec'] == 0 and eq(r['b_val'], (9.80 / 10.02 - 1) / u), 'lower limit')
    ck(r['b_flags'] == 0 and eq(r['b_opt'], r['b_val']), 'lower: no flag'); M['tlo'][0, k] = 9.9199; M['thi'][0, k] = 9.95; r = one(M); ck(r['b_cls'] == 1 and r['b_dec'] == k, 'just above L, bid over U at the close = upper')
    M = blank(); M['tlo'][0, k] = 9.9; M['bb'][0][0, k + 1] = True; M['bb'][2][0, k + 1] = True; M['qb'][5][0, k + 1] = 9.7; r = one(M); ck(r['b_sec'] == 2 and eq(r['b_val'], (9.7 / 10.02 - 1) / u), 'stop search')
    M = blank(); M['tlo'][0, e] = 9.9; r = one(M); ck(r['b_cls'] == 2 and int(r['b_flags']) == F_ENTMIN, 'stop in the entry minute, order known'); M['xlos'][0, e] = 4; ck(int(one(M)['b_flags']) == F_ENTMIN | F_UNK | F_STRUNK, 'order_unknown (the stress low is then not dated after the trigger either)'); a_ = run(derive(M), [0], [q], [1], [0.01 / math.sqrt(30)], True)[0]; ck(a_[O['b_cls']] == 0 and int(a_[O['b_flags']]) == F_UNK, 'order_unknown the other way')
    # 10 stress: the trigger minute's lowest fillable bid when its second is after the lowest print; else flagged
    M = blank(); M['tlo'][0, k] = 9.9; M['pblof'][0, k] = 9.6; r = one(M); ck(eq(r['b_stress'], (9.6 / 10.02 - 1) / u) and eq(r['b_val'], (10.0 / 10.02 - 1) / u), 'stress'); M['pblofs'][0, k] = 30; r = one(M)
    ck(eq(r['b_stress'], r['b_val']) and int(r['b_flags']) & F_STRUNK and r['b_stress_src'] == 2, 'stress order unknown'); M['pblofs'][0, k] = 40; ck(one(M)['b_stress_src'] == 1, 'stress source known')
    M['pblof'][0, k] = np.nan; r = one(M); ck(r['b_stress_src'] == 0 and not (int(r['b_flags']) & F_STRUNK) and eq(r['b_stress'], r['b_val']), 'no stress low is not a known order (erratum D)'); ck(np.isnan(one(blank())['b_stress_src']), 'stress source only on filled stops')
    # 11 upper limit: the quote known at a minute close (tk last quote), bid at or over U, valid codes; an update stamped exactly at T (only in the 0 s mark of the next minute) is not in it; the sale can be under U
    M = blank(); M['qbid'][0, k] = 10.2205; M['qb'][5][0, k + 1] = 10.1; r = one(M); ck(r['b_cls'] == 1 and r['b_dec'] == k and r['b_min'] == k + 1 and eq(r['b_val'], (10.1 / 10.02 - 1) / u), 'upper limit')
    M = blank(); M['qb'][0][0, k + 1] = 11.0; ck(one(M)['b_cls'] == 0, 'quote stamped at T is not known at T'); M = blank(); M['qbid'][0, k] = 10.2203; ck(one(M)['b_cls'] == 0, 'just under U')
    M = blank(); M['qbid'][0, k] = 10.5; M['qask'][0, k] = 10.6; M['bbE'][0, k] = True; ck(one(M)['b_cls'] == 0, 'invalid bid cannot trigger'); M['bbE'][0, k] = False; M['pstE'][0, k] = np.nan; r = one(M); ck(r['b_cls'] == 0 and int(r['b_flags']) & F_UCODES, 'upper_codes_unknown')
    M = blank(); M['qbid'][0, q + 30] = 10.5; r = one(M); ck(r['b_cls'] == 1 and r['b_dec'] == q + 30, 'bid over U at the time limit = upper'); M = blank(); M['qbid'][0, q + 31] = 10.5; ck(one(M)['b_cls'] == 0, 'after the window')
    # 12 one exit: a low under L while the decided sale waits sets the flag and sells nothing more; optimistic bound = a trade high at U counts as +2 units
    M = blank(); M['qbid'][0, k] = 10.3; M['bb'][5][0, k + 1] = True; M['bb'][15][0, k + 1] = True; M['tlo'][0, k + 1] = 9.0; r = one(M); ck(r['b_cls'] == 1 and r['b_min'] == k + 2 and int(r['b_flags']) & F_DURING, 'stop_during_exit')
    M = blank(); M['thi'][0, k] = 10.2205; r = one(M); ck(r['b_cls'] == 0 and eq(r['b_opt'], 2.0) and r['b_opt_src'] == 2, 'optimistic bound'); M2 = blank(); M2['thi'][0, e] = 10.3; ck(one(M2)['b_opt_src'] == 1 and one(blank())['b_opt_src'] == 0, 'optimistic variant on the entry minute alone, high first printed after the entry mark'); M2['xhis'][0, e] = 3; ck(one(M2)['b_opt_src'] == 3, 'entry-minute high not proven after the entry mark'); M2['thi'][0, e + 2] = 10.3; ck(one(M2)['b_opt_src'] == 2, 'a later minute'); M['tlo'][0, k] = 9.9; r = one(M); ck(r['b_cls'] == 2 and eq(r['b_opt'], r['b_val']), 'bound: lower first')
    # 13 unit: no vu, u >= 1; the other labels stay
    r = one(blank(), vu=np.nan); ck(r['b_unit'] == 1 and r['cls_ok'] == 0 and np.isnan(r['b_val']) and r['h_ret_ok'] == 1, 'no vu'); ck(one(blank(), vu=1.1 / math.sqrt(30))['b_unit'] == 4 and one(blank(), vu=0.0)['b_unit'] == 3, 'unit refusals')
    # 14 rank: average rank / n, ties averaged, empty under RKMIN rows
    v = np.arange(60, dtype=float); v[1] = 0.0; v[59] = np.nan; rk, cnt = rank30(np.r_[np.zeros(60), np.ones(10)], np.r_[v, np.arange(10.0)]); ck(cnt[0] == 59 and eq(rk[58], 1.0) and eq(rk[0], 1.5 / 59) and eq(rk[1], 1.5 / 59) and np.isnan(rk[59]) and np.isnan(rk[60]), 'rk30e')
    # 15 errata of 2026-11-21
    uu = (0.01 / math.sqrt(30)) * np.sqrt(30); L_ = 10.02 * (1 - DN * uu)   # the limit exactly as core() computes it
    # C: a bid seen only at +15 s of the entry minute is the inventory mark
    M = blank()
    for S in MARKS: M['qb'][S][0, e:] = 0
    M['qbid'][0, e:] = 0; M['qb'][15][0, e] = 9.8; r = one(M); ck(r['h_ret_ok'] == 0 and r['h_mark_ok'] == 1 and r['h_min'] == e and r['h_sec'] == 3 and eq(r['h_val'], 9.8 / 10.02 - 1), 'C: bid only at +15 s of the entry minute')
    M['qb'][5][0, e] = 9.9; M['qb'][15][0, e] = 0; ck(one(M)['h_mark_ok'] == 0, 'C: the entry mark itself is not after the entry'); M['qb'][15][0, 719] = 9.7; ck(one(M)['h_mark_ok'] == 0, 'C: nothing after the last liquidation attempt'); M['qb'][2][0, 719] = 9.6; r = one(M); ck(r['h_mark_ok'] == 1 and r['h_min'] == 719 and r['h_sec'] == 1, 'C: latest observation')
    # E: in the fill minute only a low exactly at L first printed after the fill second is proven to follow the fill
    def ex(lo, xl, late=False):
        M = blank(); M['qbid'][0, k] = 10.3; M['tlo'][0, k + 1] = lo; M['xlos'][0, k + 1] = xl
        if late: M['bb'][5][0, k + 1] = True
        return one(M)
    r = ex(L_, 40); ck(r['b_cls'] == 1 and r['b_min'] == k + 1 and r['b_sec'] == 2 and not (int(r['b_flags']) & F_DURING), 'E: low at L proven after the fill'); ck(int(ex(L_, 3)['b_flags']) & F_DURING, 'E: low at L before the fill second')
    ck(int(ex(L_, 5)['b_flags']) & F_DURING, 'E: same second is not after'); ck(int(ex(L_ - 0.01, 40)['b_flags']) & F_DURING, 'E: low under L stays ambiguous'); ck(int(ex(L_, np.nan)['b_flags']) & F_DURING, 'E: second unknown')
    r = ex(L_, 10, True); ck(r['b_sec'] == 3 and int(r['b_flags']) & F_DURING, 'E: fill at 15 s, low at second 10'); ck(not (int(ex(L_ + 0.01, 3)['b_flags']) & F_DURING), 'E: low above L')
    # F: one comparison sets both bits, at exact equality too
    M = blank(); M['qb'][5][0, e] = L_; ck(int(one(M)['b_flags']) == F_BIDL | F_SPR, 'F: entry bid exactly at L sets both bits')
    M = blank(); M['qa'][5][0, e] = 10.0; M['qb'][5][0, e] = 9.90; f_ = int(one(M)['b_flags']); ck(((f_ & F_BIDL) > 0) == ((f_ & F_SPR) > 0), 'F: A 10, bid 9.90, u 0.01: the two bits are equal')
    # G: tk quote update or trade without a px row
    M = blank(); ck(px_gaps(M) == 0, 'G: none'); quiet(M, 500); ck(px_gaps(M) == 0, 'G: a quiet minute is not a gap'); M['tn'][0, 500] = 1; ck(px_gaps(M) == 1, 'G: trade without px row'); M['tn'][0, 500] = 0; M['qn'][0, 500] = 2; ck(px_gaps(M) == 1, 'G: quote update without px row')
    # J: from the flatten minute every open position follows the liquidation attempts; the only valid bid at 15:50:15 fills a sale decided minutes earlier
    M = blank()
    for S in MARKS: M['bb'][S][0, 701:] = True
    M['bb'][15][0, 710] = False; M['qb'][15][0, 710] = 9.6; r = one(M, q=670); ck(r['cut'] == 0 and r['h_ret_ok'] == 1 and r['h_min'] == 710 and r['h_sec'] == 3 and eq(r['h_val'], 9.6 / 10.02 - 1), 'J: sale decided at 15:41 fills at 15:50:15')
    M['bb'][15][0, 705] = False; ck(one(M, q=670)['h_min'] == 710, 'J: 15 s of another later minute is not an attempt')
    # M, P: no quote yet = no tq / band; a quote from before 04:00 stands until the first in-grid update
    M = blank(); M['haspx'][0, :e + 1] = False; M['qn'][0, :e + 1] = 0; M['tn'][0, :e + 1] = 0; D = derive(M); ck(np.isnan(D['TQA'][0, q]) and np.isnan(D['TQB'][0, q]) and np.isnan(D['HS'][0, q]), 'M: a row before the first quote has no tq_ask, tq_bid, band')
    M['init'] = dict(has=np.array([True]), bid=np.array([9.99]), ask=np.array([10.01]), bsz=np.array([100.0]), asz=np.array([100.0]), pstE=np.array([1.0]), bb=np.array([False]), ab=np.array([False]), c20=np.array([False]), pqt=np.array([7], np.int64))
    D = derive(M); ck(eq(D['TQA'][0, q], 10.01) and eq(D['TQB'][0, q], 9.99) and eq(D['HS'][0, q], 0.02 / 20.0) and D['TTS'][0, q] == 7, 'P: quote at 03:50 gives tq and band at the first row'); r = one(M); ck(r['st'] == 0 and eq(r['A'], 10.01), 'P: entry in a quiet minute is judged on that quote')
    M['init']['ab'] = np.array([True]); ck(one(M)['ereason'] == 4, 'P: its codes apply'); M['init']['ab'] = np.array([False]); M['init']['pstE'] = np.array([np.nan]); ck(one(M)['ereason'] == 1, 'P: no terminal mark = side validity unresolved')
    M['init']['pstE'] = np.array([1.0]); M['haspx'][0, 100] = True; M['qn'][0, 100] = 1; ck(eq(derive(M)['TQA'][0, q], 10.02), 'P: the first in-grid update supersedes it')
    # N: the scorer's predicate (codex-026)
    def sel(n_, t_): p = np.arange(n_, dtype=float); p[-t_:] = n_; return int(select1(p).sum())
    ck(sel(150, 2) == 2 and sel(100, 2) == 0 and sel(199, 3) == 0 and sel(100, 1) == 1 and sel(300, 1) == 3 and sel(50, 1) == 1, 'N: best 1% with ties')
    sr_ = select_report([1] * 150 + [2] * 100 + [3] * 199, list(np.r_[np.arange(148.0), 148, 148]) + list(np.r_[np.arange(98.0), 98, 98]) + list(np.r_[np.arange(196.0), 196, 196, 196])); ck(sr_['selected'] == 2 and sr_['minutes_with_nothing_selected'] == 2 and sr_['minutes_with_a_tie_across_the_cut'] == 3 and sr_['of_them_tied_rows_dropped'] == 2 and sr_['per_minute'] == {0: 2, 2: 1}, 'N: the printout counts')
    # A / E1a: the files a day needs come from the calendar; every refusal before any open (spy on the opener)
    import tempfile, io, contextlib, pandas as pd, duckdb
    cal = calendar(); cs = set(cal); ck(embargo(cal)[0] == '2026-08-17' and embargo(cal)[-1] == '2026-09-14' and len(embargo(cal)) == HIST, 'E1a: the embargo is the 20 sessions 2026-08-17 .. 2026-09-14')
    ck('2026-06-19' not in cs and '2026-07-03' not in cs and '2026-09-07' not in cs and '2025-01-09' not in cs and '2026-06-18' in cs and cal[0] == '2019-11-01' and cal[-1] == '2026-12-31', 'E1a: calendar closures')
    with tempfile.TemporaryDirectory() as td:
        def folder(nm, days):
            f = os.path.join(td, nm); os.mkdir(f)
            for d in days: open(os.path.join(f, d + '.csv.gz'), 'w').close()
            return f
        names = [d for d in cal if '2026-05-01' <= d <= '2026-09-30']; src = folder('all', names); seen = []
        def spy(path, prev, tys, hist, mprev, want=True): seen.append((os.path.basename(path)[:10], want)); return ('T' if want else None), {}, {'tick': np.array([], dtype=object)}, {}
        def ref(day, s_=None):
            seen.clear(); s_ = s_ or src
            try: table(s_, '-', needed(cal, bar_names(s_)[0], day), spy, splits_fixture(s_)); return ''
            except SystemExit as x: return str(x)
        ck('history_in_sealed_window' in ref('2026-08-17') and seen == [], 'A: 2026-08-17 opens nothing'); ck('history_in_sealed_window' in ref('2026-09-14') and seen == [], 'A: 2026-09-14 opens nothing'); ck('sealed day' in ref('2026-07-15') and seen == [], 'A: sealed target')
        ck(ref('2026-09-15') == '' and [d for d, w in seen] == [d for d in cal if '2026-08-17' <= d <= '2026-09-15'] and len(seen) == 21 and [w for d, w in seen] == [False] * 20 + [True], 'E1a: 2026-09-15 is allowed with the history 2026-08-17 .. 2026-09-14 exactly')
        ck(ref('2026-06-30') == '' and len(seen) == 21 and seen[0][0] == '2026-06-01', 'A: 20 history days before the window')
        ck('not_a_trading_day' in ref('2026-06-19') and 'not_a_trading_day' in ref('2026-06-20') and seen == [], 'E1a: a closure and a Saturday are not trading days')
        ck('sealed day' in ref('2026-11-20') and seen == [], 'A: a day after HARD_LAST')
        # a copy that starts on 2026-08-17: the embargo does not depend on the folder
        s2 = folder('from0817', [d for d in names if d >= '2026-08-17'])
        ck('history_in_sealed_window' in ref('2026-08-17', s2) and seen == [] and 'history_in_sealed_window' in ref('2026-09-14', s2) and seen == [], 'E1a: a copy that starts on 2026-08-17 refuses 2026-08-17 and 2026-09-14, nothing opened'); ck(ref('2026-09-15', s2) == '' and seen[0][0] == '2026-08-17' and len(seen) == 21, 'E1a: first open target on that copy')
        # a missing ordinary session is not replaced by an older day
        s3 = folder('hole', [d for d in names if d != '2026-06-10']); x = ref('2026-06-30', s3); ck('bar_file_missing' in x and x.endswith('2026-06-10') and seen == [], 'E1a: a missing ordinary session refuses the days that need it, nothing opened'); ck(ref('2026-06-09', s3) == '' and len(seen) == 21, 'E1a: a day before the hole')
        # an absent target is refused and listed
        s4 = folder('notarget', [d for d in names if d != '2026-06-25' and not sealed(d)]); x = ref('2026-06-25', s4); ck('bar_file_missing' in x and x.endswith('2026-06-25') and seen == [], 'E1a: an absent target')
        ef = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'early-close-days.txt'); bad_ = os.path.join(td, 'early.txt'); open(bad_, 'w').write('# x\n2026-06-18 early\n')
        try: early_days(bad_); x = ''
        except SystemExit as e_: x = str(e_)
        ck('pinned' in x and '2025-11-28' in early_days(ef), 'E1b: the early-close list is pinned by md5')
        with contextlib.redirect_stdout(io.StringIO()): cv = coverage(s4, ef, '2026-06-01', '2026-09-30')
        ck('early_close' not in cv and cv['sealed_day'][0] == '2026-07-01' and cv['sealed_day'][-1] == '2026-08-14' and cv['history_in_sealed_window'] == embargo(cal) and '2026-09-15' in cv['buildable'] and '2026-06-30' not in cv['buildable'] and '2026-06-24' in cv['buildable']
           and cv['bar_file_missing'][0] == '2026-06-25(needs 2026-06-25)' and '2026-06-30(needs 2026-06-25)' in cv['bar_file_missing'] and sum(len(v) for v in cv.values()) == len([d for d in cal if '2026-06-01' <= d <= '2026-09-30']), 'O / E1a: coverage walks the calendar, by reason (sealed files absent from the folder, a target absent)')
        ck('history_in_sealed_window' in ref('2026-08-17', s4) and seen == [] and ref('2026-09-15', s4) == '' and seen[0][0] == '2026-08-17', 'A: the refusal does not depend on the sealed files being in the folder')
        s5 = folder('extra', names + ['2026-06-20']); 
        with contextlib.redirect_stdout(io.StringIO()): cv = coverage(s5, ef, '2026-06-01', '2026-06-30')
        ck(cv['bar_file_not_a_calendar_day'] == ['2026-06-20'], 'E1a: a bar file that is not a calendar day is listed')
        inv = os.path.join(td, 'inv.txt'); open(inv, 'w').write('\n'.join([d for d in cal if '2025-10-01' <= d <= '2025-12-05'] + ['2025-11-28.csv.part.x']) + '\n')
        with contextlib.redirect_stdout(io.StringIO()): cv = coverage(inv, ef, '2025-11-24', '2025-12-01')
        ck(cv['early_close'] == ['2025-11-28'] and cv['buildable'] == ['2025-11-24', '2025-11-25', '2025-11-26', '2025-12-01'] and bar_names(inv)[1] == ['2025-11-28.csv.part.x'], 'O / E1b: an early-close day in the coverage; bare dates in an explicit inventory list')
        # E1b: only a regular file named exactly YYYY-MM-DD.csv.gz stands for a date
        s6 = folder('names', [d for d in names if d != '2026-06-10'] + ['2026-06-10.backup']); os.mkdir(os.path.join(s6, '2026-06-11x.csv.gz')); open(os.path.join(s6, '2026-06-10'), 'w').close(); os.rename(os.path.join(s6, '2026-06-12.csv.gz'), os.path.join(td, 'real12')); os.mkdir(os.path.join(s6, '2026-06-12.csv.gz'))
        hv, ot = bar_names(s6); ck('2026-06-10' not in hv and '2026-06-12' not in hv and ot == ['2026-06-10', '2026-06-10.backup.csv.gz', '2026-06-11x.csv.gz', '2026-06-12.csv.gz'], 'E1b: a backup name, a bare date, a longer name and a folder of the right name are not bar files')
        x = ref('2026-06-30', s6); ck('bar_file_missing' in x and x.endswith('2026-06-10') and seen == [], 'E1b: 2026-06-10.backup.csv.gz does not pass for the missing file of 2026-06-10')
        def cc(*a):
            try:
                with contextlib.redirect_stdout(io.StringIO()): calcheck(*a)
                return ''
            except SystemExit as e_: return str(e_)
        ck(cc('-', src, names[0], names[-1]) == '' and 'FAILED' in cc('-', src, cal[cal.index(names[0]) - 1], names[-1]) and 'FAILED' in cc('-', src, names[1], names[-1]) and 'FAILED' in cc('-', s3, names[0], names[-1]) and 'FAILED' in cc('-', s5, names[0], names[-1]) and 'FAILED' in cc('-', s6, names[0], names[-1]), 'E1b: calcheck against declared bounds: a missing first day, a file outside the bounds, a hole, a file that is not a calendar day and foreign names each end with an error')
        exf = os.path.join(td, 'expl.txt'); open(exf, 'w').write('# x\n2026-06-10 lost\n'); ck(cc(exf, s3, names[0], names[-1]) == '', 'E1b: a difference named in the explained-list passes')
        # E1c: every input of a build present and pinned, or a typed refusal; nothing is opened
        def rf(f_, *a, **k):
            try: f_(*a, **k); return ''
            except SystemExit as e_: return str(e_).split(': ')[1] if str(e_).startswith('REFUSED: ') and str(e_).count(': ') >= 2 else str(e_)
        root = os.path.join(td, 'e1c'); os.mkdir(root); bars = os.path.join(root, 'minute_aggs'); os.mkdir(bars)
        for d in [x for x in cal if '2026-10-20' <= x <= '2026-11-17']: open(os.path.join(bars, d + '.csv.gz'), 'w').close()
        lst = os.path.join(root, 'list.txt'); open(lst, 'w').write('\n'.join(x for x in cal if '2026-10-20' <= x <= '2026-11-17') + '\n'); ty = os.path.join(root, 'types.csv'); open(ty, 'w').write('ticker\nAAA\n'); tm = md5(ty)
        ck(rf(real_inputs, lst, ty, '2026-11-17') == 'bar_source_not_a_folder' and rf(prefix, lst, ty, '2026-11-17', '09:45') == 'bar_source_not_a_folder' and rf(keydiff, lst, ty, '2026-11-17', 'a', 'b', 'c', 'v3') == 'bar_source_not_a_folder' and rf(build, lst, ty, '2026-11-17', root, root, root, ef, 'o') == 'bar_source_not_a_folder', 'E1c: an inventory list is not a bar source for build, prefix or keydiff')
        ck(rf(real_inputs, bars, '-', '2026-11-17') == 'types_file_missing' and rf(real_inputs, bars, os.path.join(root, 'no.csv'), '2026-11-17') == 'types_file_missing' and rf(build, bars, '-', '2026-11-17', root, root, root, ef, 'o') == 'types_file_missing', 'E1c: no types file, or "-", refuses')
        ck(rf(real_inputs, bars, ty, '2026-11-17') == 'splits_file_missing' and rf(build, bars, ty, '2026-11-17', root, root, root, ef, 'o') == 'splits_file_missing' and rf(prefix, bars, ty, '2026-11-17', '09:45') == 'splits_file_missing', 'E1c: an absent splits file refuses (no build without the split adjustment)')
        os.mkdir(os.path.join(root, 'reference')); spx = os.path.join(root, 'reference', 'splits.jsonl'); open(spx, 'w').write(''); sm = md5(spx)
        ck(rf(real_inputs, bars, ty, '2026-11-17') == 'types_file_md5' and rf(real_inputs, bars, ty, '2026-11-17', pins=(tm, 'x')) == 'splits_file_md5' and rf(build, bars, ty, '2026-11-17', root, root, root, ef, 'o') == 'types_file_md5', 'E1c: a types or splits file that is not the pinned one refuses')
        ck(real_inputs(bars, ty, '2026-11-17', pins=(tm, sm))[0][0] == '2026-10-20' and real_inputs(bars, ty, '2026-11-17', pins=(tm, sm))[1].factors == {} and rf(real_inputs, bars, ty, '2026-11-16', pins=(tm, sm)) == 'bar_file_missing', 'E1c: with both files as pinned the calendar list is returned; the calendar refusals come first')
        # E1c, second part
        ck(rf(needed, cal, cs, '20261117') == 'day_malformed' and rf(needed, cal, cs, '2026-13-01') == 'day_malformed' and rf(needed, cal, cs, '2026-11-17 ') == 'day_malformed' and rf(real_inputs, bars, ty, '20261117') == 'day_malformed' and rf(build, bars, ty, '20261117', root, root, root, ef, 'o') == 'day_malformed', 'E1c: a day not written YYYY-MM-DD is malformed, not sealed')
        ck(rf(read_types, os.path.join(root, 'no.csv')) == 'types_file_missing' and read_types(ty) == {'AAA'}, 'E1c: the types file is read'); t2 = os.path.join(root, 't2.csv'); open(t2, 'w').write('symbol\nAAA\n'); ck(rf(read_types, t2) == 'types_file_unreadable', 'E1c: a types file without the ticker column'); open(t2, 'w').write(''); ck(rf(read_types, t2) == 'types_file_unreadable', 'E1c: an empty types file')
        odd_ = ['NA', 'NULL', 'None', 'nan', 'NAN', 'TRUE', 'INF', 'N/A', 'null', 'AAA']; open(t2, 'w').write('ticker,type\n' + ''.join('%s,CS\n' % x for x in odd_)); ck(read_types(t2) == set(odd_) and all(isinstance(x, str) for x in read_types(t2)), 'E1g: NA, NULL, None, nan, NAN, TRUE, INF are kept as that text')
        open(t2, 'w').write('ticker,type\nAAA,CS\n,CS\nBBB,CS\n'); ck(rf(read_types, t2) == 'types_ticker_empty', 'E1g: an empty ticker is refused, not dropped'); open(t2, 'w').write('ticker,type\nAAA,CS\n  ,CS\n'); ck(rf(read_types, t2) == 'types_ticker_empty', 'E1g: a blank ticker is refused')
        open(t2, 'wb').write(b'ticker,type\nAAA,CS\nNA\x00X,CS\n'); x_ = rf(read_types, t2); open(t2, 'wb').write(b'ticker,type\nAAA,CS\n"N"A,CS\n'); ck(x_ == 'types_file_bytes' and rf(read_types, t2) == 'types_file_bytes', 'E1h: a NUL byte or a double quote in the types file is refused before the csv reader')
        open(t2, 'wb').write(b'ticker,type\nAAA,CS\nNA,CS\n'); ck(read_types(t2) == {'AAA', 'NA'}, 'E1h: valid neighbour: the ticker NA')
        wsrc_ = open(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wm2_l1.py')).read() if os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wm2_l1.py')) else None
        w_g, _ = load_wm2(); wsrc_ = open(w_g.__file__).read(); ck('read_csv(types_path' not in wsrc_ and wsrc_.count('_L1_READ_TYPES(types_path)') == 1 and w_g._L1_READ_TYPES is read_types and 'read_csv' not in wsrc_.replace('pd.read_csv(path', ''), 'E1g: the table code the build runs has no types reader of its own (%d read_csv left)' % wsrc_.count('read_csv'))
        ck(rf(early_days, os.path.join(root, 'none.txt')) == 'early_close_file_missing' and rf(early_days, bad_) == 'early_close_file_md5' and rf(build, bars, ty, '2026-11-17', root, root, root, os.path.join(root, 'none.txt'), 'o') == 'early_close_file_missing', 'E1c: no early-close days file')
        nd = [x for x in cal if '2026-10-20' <= x <= '2026-11-17']; x = rf(bars_readable, bars, nd); ck(x == 'bar_file_unreadable', 'E1c: an empty bar file (not gzip)')
        for d in nd:
            with gzip.open(os.path.join(bars, d + '.csv.gz'), 'wt') as g: g.write(','.join(BARCOLS) + ',transactions\n' + 'AAA,1,1,1,1,1,%d,1\n' % (int(datetime.datetime.fromisoformat(d + 'T09:30:00').replace(tzinfo=zoneinfo.ZoneInfo('America/New_York')).timestamp()) * 10 ** 9))
        rec_ = bars_readable(bars, nd); ck(len(rec_) == 21 and rec_[0][0] == nd[0] and rec_[0][1] == os.path.getsize(os.path.join(bars, nd[0] + '.csv.gz')) and rec_[0][2] == md5(os.path.join(bars, nd[0] + '.csv.gz')), 'E1c: bar files with the expected header and day pass; bytes and md5 of each are returned')
        keep_ = open(os.path.join(bars, nd[5] + '.csv.gz'), 'rb').read(); open(os.path.join(bars, nd[5] + '.csv.gz'), 'wb').write(open(os.path.join(bars, nd[6] + '.csv.gz'), 'rb').read())
        try: bars_readable(bars, nd); x = ''
        except SystemExit as e_: x = str(e_)
        ck('bar_file_wrong_day' in x and nd[5] in x and x.endswith(nd[6]), 'E1c: a file named for one day that holds the bars of another'); open(os.path.join(bars, nd[5] + '.csv.gz'), 'wb').write(keep_)
        # --- erratum E1d ---
        import subprocess, wide
        e1d = os.path.join(root, 'e1d'); os.mkdir(e1d); D_ = '2026-11-17'; d0_ = ny_ns(D_); ns_ = lambda hm, dd=0: ny_ns(D_, dd) + (int(hm[:2]) * 60 + int(hm[3:])) * NSMIN
        def barfile(name, rws):
            f_ = os.path.join(e1d, name); os.makedirs(os.path.dirname(f_), exist_ok=True); pd.DataFrame(rws, columns=['ticker', 'volume', 'open', 'close', 'high', 'low', 'window_start', 'transactions']).to_csv(f_, index=False, compression='gzip'); return f_
        gb_ = [['AAA', 10, 1.0, 1.1, 1.2, 0.9, ns_('03:59'), 1], ['AAA', 10, 1.0, 1.1, 1.2, 0.9, ns_('04:00'), 1], ['AAA', 11, 1.0, 1.3, 1.3, 0.9, ns_('09:30'), 2], ['BBB', 5, 2.0, 2.1, 2.2, 1.9, ns_('09:30'), 1], ['BBB', 5, 2.0, 2.1, 2.2, 1.9, ns_('19:59'), 1], ['BBB', 5, 2.0, 2.1, 2.2, 1.9, ns_('20:00'), 1]]
        f_ = barfile('ok/%s.csv.gz' % D_, gb_); a_ = bar_load(f_); b_ = wide.load(f_); ck(len(a_) == 4 and list(a_.columns) == list(b_.columns) and a_.equals(b_) and a_.m.dtype == b_.m.dtype and list(a_.index) == list(b_.index), 'E1d: bar_load returns the frame of the pinned wide.load')
        ck(load_wm2()[0].load is bar_load and load_old_rule('v2').load is bar_load and load_old_rule('v3').load is bar_load, 'E1d: the table code, new and old rule, reads bars through bar_load')
        def has(x, code, *words): return x.startswith('REFUSED: %s: ' % code) and all(w_ in x for w_ in words)
        import inspect as _i9
        _in_src = _i9.getsource
        def rfm(f_, *a):
            try: f_(*a); return ''
            except SystemExit as e_: return str(e_)
        x = rfm(bar_load, barfile('two/%s.csv.gz' % D_, gb_ + [['CCC', 5, 2.0, 2.1, 2.2, 1.9, ns_('09:31', 1), 1]])); ck('bar_file_wrong_day' in x and 'holds 1 rows' in x and 'CCC at 2026-11-18T09:31' in x, 'E1d: a bar file with a second date, key named')
        x = rfm(bar_load, barfile('two2/%s.csv.gz' % D_, gb_ + [['CCC', 5, 2.0, 2.1, 2.2, 1.9, ns_('23:00', -1), 1]])); ck('bar_file_wrong_day' in x and 'CCC at 2026-11-16T23:00' in x, 'E1d: a row of the day before outside the grid')
        x = rfm(bar_load, barfile('dup/%s.csv.gz' % D_, gb_ + [['AAA', 99, 9.0, 9.0, 9.0, 9.0, ns_('09:30'), 1]])); ck('bar_key_duplicate' in x and '1 rows' in x and 'AAA at 2026-11-17T09:30' in x, 'E1d: one bar key twice, key named')
        x = rfm(bar_load, barfile('dup2/%s.csv.gz' % D_, gb_ + [['AAA', 99, 9.0, 9.0, 9.0, 9.0, ns_('09:30') + 30 * NSSEC, 1]])); ck(has(x, 'bar_stamp_off_minute', '1 rows', 'AAA at %d' % (ns_('09:30') + 30 * NSSEC)) and has(rfm(bar_load, barfile('off2/%s.csv.gz' % D_, gb_ + [['DDD', 1, 9.0, 9.0, 9.0, 9.0, ns_('09:36') - 1, 1]])), 'bar_stamp_off_minute', 'DDD'), 'E1e: a bar stamped inside a minute (09:35:59.999999999 would be read as 09:35)')
        # erratum E1h (version 12): bytes and values of a bar file, each wrong input with a valid neighbour
        def gzb(name, bts):
            f2_ = os.path.join(e1d, name); os.makedirs(os.path.dirname(f2_), exist_ok=True); open(f2_, 'wb').write(gzip.compress(bts)); return f2_
        base_ = gzip.decompress(open(os.path.join(e1d, 'ok', D_ + '.csv.gz'), 'rb').read()); ln_ = lambda t_, v_, p_, hm='09:31': ('%s,%s,%s,%s,%s,%s,%d,1\n' % (t_, v_, p_, p_, p_, p_, ns_(hm))).encode()
        x = rfm(bar_load, gzb('nul/%s.csv.gz' % D_, base_ + ln_('NA\x00X', 5, 2.0))); ck(has(x, 'bar_file_bytes', 'a NUL byte', 'line 8'), 'E1h: a NUL byte in a bar file is refused before the csv reader (%s)' % x[:60])
        x = rfm(bar_load, gzb('quo/%s.csv.gz' % D_, base_ + ln_('"N"A', 5, 2.0))); ck(has(x, 'bar_file_bytes', 'a double quote', 'line 8') and has(rfm(bar_load, gzb('nul2/%s.csv.gz' % D_, base_ + ln_('CCC', 5, '1\x000'))), 'bar_file_bytes', 'a NUL byte'), 'E1h: a double quote in a ticker, a NUL byte inside a number')
        a_ = bar_load(gzb('na/%s.csv.gz' % D_, base_ + ln_('NA', 5, 2.0))); ck(len(a_) == 5 and list(a_.ticker.values).count('NA') == 1 and float(a_[a_.ticker == 'NA'].close.iloc[0]) == 2.0, 'E1h: valid neighbour: the ticker NA in a bar file is a ticker')
        for k_, (v_, p_, col_) in enumerate(((5, 'inf', 'open'), (5, '-inf', 'open'), (5, '1e999', 'open'), (5, 0, 'open'), (5, -5, 'open'), (5, '0.0', 'open'), (-1, 2.0, 'volume'), ('inf', 2.0, 'volume'))):
            x = rfm(bar_load, gzb('val%d/%s.csv.gz' % (k_, D_), base_ + ln_('CCC', v_, p_))); ck(has(x, 'bar_value_invalid', '1 rows', col_, 'CCC at 2026-11-17T09:31') or (p_ == '1e999' and has(x, 'bar_file_unreadable', 'the column open is read as')), 'E1h: a bar with volume %s and prices %s is refused by ticker and minute (%s)' % (v_, p_, x[:50]))   # 1e999: pandas 3 reads it as inf (bar_value_invalid), pandas 2.3 on the Mac leaves the column as text (bar_file_unreadable, column named): refused on both, never read as a number
        x = rfm(bar_load, gzb('valc/%s.csv.gz' % D_, base_.replace(b',1.1,', b',-1.1,', 1))); ck(has(x, 'bar_value_invalid', 'close', 'AAA at 2026-11-17T03:59'), 'E1h: a close below 0 outside the grid is refused too')
        x = rfm(bar_load, gzb('valt/%s.csv.gz' % D_, base_ + b'CCC,5,2.0,2.0,2.0,2.0,%d,inf\n' % ns_('09:31'))); ck(has(x, 'bar_value_invalid', 'transactions', 'CCC'), 'E1h: a column the table does not read must be finite too')
        a_ = bar_load(gzb('valok/%s.csv.gz' % D_, base_ + ln_('CCC', 0, 0.0001) + ln_('DDD', '0.5', '1e-9'))); ck(len(a_) == 6 and float(a_[a_.ticker == 'CCC'].close.iloc[0]) == 0.0001 and float(a_[a_.ticker == 'CCC'].volume.iloc[0]) == 0.0, 'E1h: valid neighbours: price 0.0001, volume 0')
        # erratum E1h, 1: one buffer. With the keeping on, the bytes hashed by bars_readable are the bytes bar_load parses, also when the file is replaced in between; the check after the build sees the change on disk.
        kd_ = os.path.join(e1d, 'keep'); os.mkdir(kd_); kf_ = os.path.join(kd_, D_ + '.csv.gz'); open(kf_, 'wb').write(gzip.compress(base_)); _KEEP[0] = True
        try:
            rec_k = bars_readable(kd_, [D_]); first_ = open(kf_, 'rb').read(); open(kf_, 'wb').write(gzip.compress(base_ + ln_('ZZZ', 5, 2.0))); a_ = bar_load(kf_)
            ck(rec_k == [(D_, len(first_), hashlib.md5(first_).hexdigest())] and 'ZZZ' not in set(a_.ticker) and len(a_) == 4 and rf(files_same, rec_k, [kf_]) == 'bar_files_opened_differ', 'E1h: one buffer: a bar file replaced after its check is parsed from the checked bytes, and the change on disk is refused after the build')
            kt_ = os.path.join(kd_, 'types.csv'); open(kt_, 'w').write('ticker,type\nAAA,CS\n'); h1_ = md5b(raw(kt_)); open(kt_, 'w').write('ticker,type\nAAA,CS\nEVIL,CS\n')
            ck(read_types(kt_) == {'AAA'} and md5b(raw(kt_)) == h1_ == hashlib.md5(b'ticker,type\nAAA,CS\n').hexdigest(), 'E1h: one buffer: the types parsed are the bytes whose md5 was compared, also when the file is replaced in between')
            kc_ = os.path.join(kd_, 'cal.txt'); open(kc_, 'w').write('2026-06-01\n2026-06-02\n'); pc_ = md5(kc_); c1_ = calendar(kc_, pc_); open(kc_, 'w').write('2026-06-01\n2026-06-03\n'); ck(c1_ == calendar(kc_, pc_) == ['2026-06-01', '2026-06-02'], 'E1h: one buffer: the calendar')
            open(kf_, 'wb').write(gzip.compress(base_ + ln_('YYY', 5, 2.0))); forget(kf_); ck('YYY' in set(bar_load(kf_).ticker), 'E1h: one buffer: a file the process writes anew under one name (the prefix test\'s cut file) is read anew after forget')
        finally: _KEEP[0] = False; _ONCE.clear()
        gf_ = os.path.join(e1d, 'bad'); os.mkdir(gf_); open(os.path.join(gf_, D_ + '.csv.gz'), 'wb').write(b'not gzip'); ck(rf(bar_load, os.path.join(gf_, D_ + '.csv.gz')) == 'bar_file_unreadable' and rf(bar_load, barfile('ok/bars.csv.gz', gb_)) == 'bar_file_unreadable', 'E1d: a bar file that cannot be read, or without a date in its name')
        w_ = load_wm2()[0]; ck(rf(one_table, w_, os.path.join(e1d, 'dup', D_ + '.csv.gz'), ({}, {}, collections.deque(maxlen=HIST)), None) == 'bar_key_duplicate' and rf(one_table, w_, os.path.join(e1d, 'two', D_ + '.csv.gz'), ({}, {}, collections.deque(maxlen=HIST)), None) == 'bar_file_wrong_day', 'E1d: the table code itself refuses such a file on its one read')
        # tick and price files: artificial, with manifests that carry the right digests of the deliberately bad files
        con_ = duckdb.connect(); kn_ = [0]
        def tkrow(sym='AAA', m=570, **k): r = dict(sym=sym, m=m, qn=1, tn=1, qbid=1.0, qask=1.1, qbsl=1.0, qasl=1.0, tlo=1.0, thi=1.1); r.update(k); return r
        def pxrow(sym='AAA', m=570, **k):
            r = dict(sym=sym, m=m, xlos=1.0, xhis=1.1, pblof=1.0, pblofs=1.0, pblo5=1.0, pblo5s=1.0, pstE=11, pqiE='', pqcE='', pqtE=(d0_ + (m + 1) * NSMIN - 1) if isinstance(m, int) else None)
            for S in MARKS: r.update({'pst%d' % S: 11, 'qb%d' % S: 1.0, 'qa%d' % S: 1.1, 'qbz%d' % S: 1.0, 'qaz%d' % S: 1.0, 'pqi%d' % S: '', 'pqc%d' % S: '', 'pqt%d' % S: (d0_ + m * NSMIN + S * NSSEC) if isinstance(m, int) else None})
            r.update(k); return r
        def tfiles(tk, px, mt='INTEGER', drop=None, raw=None, mp=None, tt=None):
            kn_[0] += 1; b_ = os.path.join(e1d, 't%d' % kn_[0]); ds_ = [os.path.join(b_, x) for x in ('tk', 'px', 'man')]; [os.makedirs(x) for x in ds_]; fs_ = {}
            for nm_, rws, d_ in (('tk', tk, ds_[0]), ('px', px, ds_[1])):
                f_ = os.path.join(d_, D_ + '.parquet'); df_ = pd.DataFrame(rws, dtype=object)   # object first: a time in ns next to a null must not pass through a float
                for c_ in df_.columns:
                    if c_.startswith('pqt'): df_[c_] = df_[c_].astype('Int64')
                    if c_.startswith('pst'): df_[c_] = df_[c_].astype('Int8')
                df_ = df_.infer_objects()
                if drop and drop[0] == nm_: df_ = df_.drop(columns=[drop[1]])
                con_.register('X_', df_); con_.execute("COPY (SELECT * REPLACE (CAST(m AS %s) AS m%s) FROM X_) TO '%s' (FORMAT PARQUET)" % ((mt if nm_ == 'tk' else mp or 'INTEGER'), (', CAST(%s AS %s) AS %s' % (tt[0], tt[1], tt[0]) if tt and nm_ == 'px' else ''), f_)); con_.unregister('X_')
                if raw == nm_: open(f_, 'wb').write(b'PAR1 not a parquet file')
                fs_[nm_] = dict(bytes=os.path.getsize(f_), md5=md5(f_))
            json.dump(dict(date=D_, script_md5=FROZEN, files=fs_), open(os.path.join(ds_[2], D_ + '.json'), 'w')); tkf_, pxf_ = tick_inputs(D_, *ds_)[:2]   # the manifest carries the right digests: the path and digest checks pass
            return rfm(tick_keys, D_, tkf_, pxf_)
        gt_ = [tkrow(), tkrow(m=100), tkrow(m=571), tkrow('BBB', 1255, qn=0)]
        gp_ = [pxrow(), pxrow(m=100), pxrow(m=571, pqt0=d0_ + 570 * NSMIN + 3), pxrow('BBB', 1255, pstE=0, pqtE=None, pst0=0, pqt0=None, qb0=0.0, qa0=None, pst2=0, pqt2=0, qb2=None, qa2=0.0), pxrow('CCC', 600)]
        ck(tfiles(gt_, gp_) == '' and tick_keys(D_, os.path.join(e1d, 't1', 'tk', D_ + '.parquet'), os.path.join(e1d, 't1', 'px', D_ + '.parquet')) == {'tick': 4, 'price': 5},
           'E1d: good files pass: a quote exactly at its mark, a terminal quote 1 ns before the next minute, a quote carried from an earlier minute, no-quote rows with time null or 0, a row before 04:00, a minute without a row')
        ck(has(tfiles(gt_, gp_, raw='tk'), 'tick_file_unreadable') and has(tfiles(gt_, gp_, raw='px'), 'price_file_unreadable'), 'E1d: a tick or price file that is not parquet although its digest equals the manifest\'s')
        ck(has(tfiles(gt_, gp_, drop=('tk', 'qasl')), 'tick_file_unreadable', 'qasl') and has(tfiles(gt_, gp_, drop=('px', 'pqt5')), 'price_file_unreadable', 'pqt5'), 'E1d: a tick or price file without a required column')
        ck(has(tfiles(gt_ + [tkrow(m=None)], gp_), 'tick_key_invalid', '1 rows') and has(tfiles(gt_, gp_ + [pxrow(sym=None, m=572)]), 'price_key_invalid', '572') and has(tfiles(gt_ + [tkrow(m=1440)], gp_), 'tick_key_invalid', '1440') and has(tfiles(gt_, gp_ + [pxrow(m=-1)]), 'price_key_invalid', '-1'), 'E1d: a key that is null, or a minute outside the day')
        ck(has(tfiles(gt_ + [tkrow(m=572.5)], gp_, mt='DOUBLE'), 'tick_file_unreadable', 'column m', 'DOUBLE') and has(tfiles(gt_, gp_, mt='DOUBLE'), 'tick_file_unreadable') and has(tfiles([dict(r_, m=str(r_['m'])) for r_ in gt_], gp_, mt='VARCHAR'), 'tick_file_unreadable', 'VARCHAR') and has(tfiles(gt_, gp_, mp='DOUBLE'), 'price_file_unreadable', 'column m')
           and tfiles(gt_, gp_, mt='BIGINT', mp='SMALLINT') == '', 'version 7: a minute stored as a float or as text is refused by column and type; another integer type passes')
        for c_ in ['pqt%d' % S_ for S_ in MARKS] + ['pqtE']: ck(has(tfiles(gt_, gp_, tt=(c_, 'DOUBLE')), 'price_file_unreadable', 'column %s' % c_, 'DOUBLE'), 'version 7: %s stored as a float is refused' % c_)
        ck(has(tfiles(gt_, gp_ + [pxrow(m=580, pqt5=d0_ + 580 * NSMIN + 5 * NSSEC + 1)], tt=('pqt5', 'DOUBLE')), 'price_file_unreadable', 'pqt5') and has(tfiles(gt_, gp_, tt=('pqtE', 'VARCHAR')), 'price_file_unreadable', 'VARCHAR'), 'version 7: a mark + 1 ns stored as a float does not round onto its mark, it is refused')
        nl_ = os.path.join(e1d, 'nl'); os.mkdir(nl_); open(os.path.join(nl_, '2026-11-17.csv.gz\n'), 'w').close(); open(os.path.join(nl_, '2026-11-16.csv.gz'), 'w').close(); open(os.path.join(nl_, '\u0662\u0660\u0662\u0666-11-13.csv.gz'), 'w').close(); hv_, ot_ = bar_names(nl_)
        il_ = os.path.join(e1d, 'inv.txt'); open(il_, 'w').write('2026-11-16\n2026-11-17.csv.gz\n\u0662\u0660\u0662\u0666-11-13\n2026-11-18.csv.gz.bak\n'); hi_, oi_ = bar_names(il_)
        ck(hv_ == {'2026-11-16'} and len(ot_) == 2 and '2026-11-17.csv.gz\n' in ot_ and hi_ == {'2026-11-16', '2026-11-17'} and len(oi_) == 2, 'version 7: a file name with a final newline or with other digits than 0-9 never stands for a date')
        ck(rf(day_ok, '2026-11-17\n') == 'day_malformed' and rf(day_ok, '\u0662\u0660\u0662\u0666-11-17') == 'day_malformed' and rf(cut_minute, '09:45\n') == 'cut_malformed' and rf(cut_minute, '\u0660\u0669:45') == 'cut_malformed' and rf(cut_minute, None) == 'cut_malformed' and rf(bar_load, os.path.join(nl_, '2026-11-17.csv.gz\n')) == 'bar_file_unreadable', 'version 7: a day, a cut and a bar name are matched whole')
        def bcol(i_, v_): r_ = [list(x) for x in gb_]; r_[2][i_] = v_; return r_
        for i_, c_ in ((1, 'volume'), (2, 'open'), (3, 'close'), (4, 'high'), (5, 'low')): x = rfm(bar_load, barfile('c%d/%s.csv.gz' % (i_, D_), bcol(i_, 'x'))); ck(has(x, 'bar_file_unreadable', 'column %s' % c_), 'version 7: a text in the bar column %s' % c_)
        x = rfm(bar_load, barfile('c6/%s.csv.gz' % D_, bcol(6, ns_('09:30') + 0.5))); ck(has(x, 'bar_file_unreadable', 'column window_start', 'float'), 'version 7: a window_start that is not an integer')
        x = rfm(bar_load, barfile('c7/%s.csv.gz' % D_, bcol(3, ''))); ck(has(x, 'bar_file_unreadable', 'column close'), 'version 7: an empty close')
        f_ = os.path.join(e1d, 'c8'); os.mkdir(f_); f_ = os.path.join(f_, D_ + '.csv.gz'); pd.DataFrame(gb_, columns=['ticker', 'volume', 'open', 'close', 'high', 'low', 'window_start', 'transactions']).drop(columns=['low']).to_csv(f_, index=False, compression='gzip'); ck(has(rfm(bar_load, f_), 'bar_file_unreadable', 'lacks the column low'), 'version 7: a bar file without a column, at the read itself')
        ck(has(tfiles(gt_ + [tkrow(m=100, qbid=9.0)], gp_), 'tick_key_duplicate', "('AAA', 100, 2)") and has(tfiles(gt_, gp_ + [pxrow(m=100)]), 'price_key_duplicate', "('AAA', 100, 2)") and has(tfiles(gt_ + [tkrow(m=570), tkrow(m=570)], gp_), 'tick_key_duplicate', "('AAA', 570, 3)"), 'E1d: one key twice, before 04:00 and inside the grid, key named')
        for S_ in MARKS: ck(has(tfiles(gt_, gp_ + [pxrow(m=580, **{'pqt%d' % S_: d0_ + 580 * NSMIN + S_ * NSSEC + 1})]), 'quote_after_mark', 'pqt%d' % S_, "('AAA', 580, %d)" % (d0_ + 580 * NSMIN + S_ * NSSEC + 1)), 'E1d: a quote stamped 1 ns after the mark at second %d' % S_)
        ck(has(tfiles(gt_, gp_ + [pxrow(m=580, pqtE=d0_ + 581 * NSMIN)]), 'quote_at_or_after_boundary', "('AAA', 580, ") and has(tfiles(gt_, gp_ + [pxrow(m=100, sym='ZZZ', pqtE=d0_ + 101 * NSMIN + 5)]), 'quote_at_or_after_boundary', 'ZZZ'), 'E1d: a terminal quote stamped exactly at the boundary, and one after it before 04:00')
        ck(has(tfiles(gt_, gp_ + [pxrow(m=580, pqt5=None)]), 'quote_time_missing', 'pst5', "('AAA', 580, 11)") and has(tfiles(gt_, gp_ + [pxrow(m=580, pqtE=0)]), 'quote_time_missing', 'pstE') and has(tfiles(gt_, gp_ + [pxrow(m=100, pqt0=None)]), 'price_key_duplicate') and has(tfiles(gt_, gp_ + [pxrow('ZZZ', 100, pqt0=None)]), 'quote_time_missing', 'pst0')
           and tfiles(gt_, gp_ + [pxrow(m=580, pst5=None, pqt5=None, qb5=None, qa5=None), pxrow(m=581, pstE=0, pqtE=0)]) == '', 'E1d: a state that says a quote exists with no time is refused, before 04:00 too; state null or 0 with no time passes')
        ck(has(tfiles(gt_, gp_ + [pxrow(m=580, pqt0=d0_ - 1)]), 'quote_wrong_day', 'pqt0') and has(tfiles(gt_, gp_ + [pxrow(m=580, pqtE=ny_ns(D_, 1))]), 'quote_wrong_day', 'pqtE') and has(tfiles(gt_, gp_ + [pxrow(m=580, pqt15=ny_ns(D_, 1) + 5)]), 'quote_wrong_day', 'pqt15'), 'E1d: a quote time of the day before or the day after')
        for S_ in MARKS:
            ck(has(tfiles(gt_, gp_ + [pxrow(m=580, **{'pst%d' % S_: 0, 'pqt%d' % S_: 0, 'qb%d' % S_: 12.0, 'qa%d' % S_: None})]), 'quote_state_missing', 'second %d' % S_, "('AAA', 580, 0)") and has(tfiles(gt_, gp_ + [pxrow('ZZZ', 100, **{'pst%d' % S_: None, 'pqt%d' % S_: None, 'qb%d' % S_: 0.0, 'qa%d' % S_: 0.01})]), 'quote_state_missing', 'ZZZ'), 'E1e: mark %d with a price and no state (Codex: bid 12, state 0, time 0), before 04:00 too' % S_)
        ck(tfiles(gt_, gp_ + [pxrow(m=580, pst5=0, pqt5=None, qb5=0.0, qa5=0.0), pxrow(m=581, pst15=None, pqt15=None, qb15=None, qa15=None)]) == '', 'E1e: an empty mark stays valid')
        ck(has(tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pstE=None, pqtE=None)]), 'terminal_state_missing', "('AAA', 580, None)") and has(tfiles(gt_ + [tkrow(m=90)], gp_ + [pxrow(m=90, pstE=0, pqtE=0)]), 'terminal_state_missing', "('AAA', 90, 0)") and tfiles(gt_ + [tkrow(m=580, qn=0)], gp_ + [pxrow(m=580, pstE=None, pqtE=None)]) == '' and tfiles(gt_ + [tkrow(m=580)], gp_) == '',
           'E1e: a minute with a quote update and no terminal state is refused, before 04:00 too; without an update, or without a px row, it is not this rule')
        ck(has(tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pqtE=d0_ + 580 * NSMIN - 1)]), 'terminal_before_minute', "('AAA', 580, %d)" % (d0_ + 580 * NSMIN - 1)) and tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pqtE=d0_ + 580 * NSMIN)]) == '' and tfiles(gt_ + [tkrow(m=580, qn=0)], gp_ + [pxrow(m=580, pqtE=d0_ + 579 * NSMIN + 7)]) == '' and tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pqt15=d0_ + 570 * NSMIN)]) == '',
           'E1e: a terminal time 1 ns before its minute is refused; exactly at the start passes; a minute without an update and a mark carrying an earlier minute pass')
        for v_ in (10, 20):
            ck(has(tfiles(gt_, gp_ + [pxrow(m=580, pst5=v_, qb5=12.0)]), 'quote_state_unknown', 'pst5', "('AAA', 580, %d)" % v_) and has(tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pstE=v_)]), 'quote_state_unknown', 'pstE'), 'version 9: state %d (base digit 0) with a bid, at a mark and at the terminal' % v_)
        for v_ in (7, 27, 8, 9, 17, 21, -1, 100): ck(has(tfiles(gt_, gp_ + [pxrow('ZZZ', 100, pst0=v_)]), 'quote_state_unknown', 'pst0', "('ZZZ', 100, %d)" % v_), 'version 9: state %d is not one the converter writes' % v_)
        for v_ in PSTSET[1:]: ck(tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pst0=v_, pst2=v_, pst5=v_, pst15=v_, pstE=v_)]) == '', 'version 9: state %d passes on every state column' % v_)
        ck('% 10 = 0' in _in_src(tick_keys) and _in_src(tick_keys).count('% 10 = 0') >= 2 and _in_src(tick_keys).count('%% 10 = 0') == 1, 'version 9: the missing-state rules test the base digit')
        for v_ in (6, 16):
            ck(tfiles(gt_ + [tkrow(m=580, qbid=0.0, qask=0.0)], gp_ + [pxrow(m=580, pstE=v_, pqtE=d0_ + 580 * NSMIN + 20 * NSSEC, **{k_ % S_: x_ for S_ in MARKS for k_, x_ in (('pst%d', v_), ('qb%d', 0.0), ('qa%d', 0.0))})]) == '', 'version 9, valid neighbour: a clear (a counted update, both sides gone, state %d, a real time in the minute)' % v_)
        ck(tfiles(gt_ + [tkrow(m=580)], gp_ + [pxrow(m=580, pqtE=d0_ + 580 * NSMIN, pqt0=d0_ + 580 * NSMIN, pqt2=d0_ + 580 * NSMIN, pqt5=d0_ + 580 * NSMIN, pqt15=d0_ + 580 * NSMIN)]) == '', 'version 9, valid neighbour: an update exactly at the minute start')
        ck(tfiles(gt_ + [tkrow(m=580, qn=0, tn=3)], gp_ + [pxrow(m=580, pstE=None, pqtE=None, pqt0=d0_ + 570 * NSMIN, pqt2=d0_ + 570 * NSMIN, pqt5=d0_ + 570 * NSMIN, pqt15=d0_ + 570 * NSMIN)]) == '' and tfiles(gt_ + [tkrow(m=580, qn=0, tn=3)], gp_) == '', 'version 9, valid neighbour: a trade-only minute, with and without a px row')
        tf9_ = os.path.join(e1d, 't1', 'tk', D_ + '.parquet'); ck(rf(tick_symbols, tf9_, np.array(['AAA', 'BBB'])) == '' and rf(tick_symbols, tf9_, np.array([], dtype=object)) == '' and has(rfm(tick_symbols, tf9_, np.array(['AAA', 'CCC', 'ZZZ'])), 'tick_symbol_missing', '2 table stocks', 'CCC'), 'version 9: a table stock with no row at all in the tick file')
        cap_, _, _ = one_table(w_, os.path.join(e1d, 'ok', D_ + '.csv.gz'), ({}, {}, collections.deque(maxlen=HIST)), None); d_ = bar_load(os.path.join(e1d, 'ok', D_ + '.csv.gz'))
        for sy_ in (np.array(['AAA', 'BBB']), np.array(['BBB']), np.array([], dtype=object)):
            dd_ = d_[d_.ticker.isin(set(sy_))]; C0_ = np.full((len(sy_), NM), np.nan); C0_[np.searchsorted(sy_, dd_.ticker.values), dd_.m.values] = dd_.close.values
            ck(np.array_equal(closes(cap_, sy_), C0_, equal_nan=True) and closes(cap_, sy_).shape == (len(sy_), NM), 'E1e: the captured close matrix equals the old second read of the target (%d stocks)' % len(sy_))
        ck(int(np.isfinite(closes(cap_, np.array(['AAA', 'BBB']))).sum()) == 4 and rf(closes, cap_, np.array(['AAA', 'ZZZ'])) == 'closes_ticker_missing' and rf(closes, cap_, np.array(['AAB'])) == 'closes_ticker_missing', 'E1e: four closes of the fixture day; a table stock that is not in the capture is refused')
        import inspect as _in; ck('bar_load(' not in _in.getsource(_build) and 'import load' not in _in.getsource(_build) and 'read_csv' not in _in.getsource(_build), 'E1e: the build has no bar read of its own after the table')
        # publication
        pb_ = os.path.join(e1d, 'pub'); os.mkdir(pb_); out_ = os.path.join(pb_, D_ + '.parquet'); L_ = pd.DataFrame({'date': D_, 'sym': ['AAA', 'AAA', 'BBB'], 'm': np.array([330, 331, 330], np.int16), 'x': [1.0, 2.0, 3.0]}); tmps = lambda: sorted(x for x in os.listdir(pb_) if x.startswith('.writing-'))
        cl_ = claim(out_); here_ = os.path.abspath(__file__)
        ck(os.path.isfile(out_ + '.claim') and holds(cl_) and str(os.getpid()) in open(out_ + '.claim').read() and socket.gethostname() in cl_[1] and rf(claim, out_) == 'output_claimed' and holds(cl_), 'E1d: the claim names process, host and start; a second claim in the same process is refused and the first stands')
        pr_ = subprocess.run([sys.executable, '-c', 'import sys, importlib.util as u\ns = u.spec_from_file_location("l1b", sys.argv[1]); m = u.module_from_spec(s); s.loader.exec_module(m)\nm.attempt(sys.argv[2], lambda cl: print("COMPUTED"))', here_, out_], capture_output=True, text=True)
        ck(pr_.returncode != 0 and 'REFUSED: output_claimed' in pr_.stderr and 'COMPUTED' not in pr_.stdout and holds(cl_) and not os.path.exists(out_), 'E1d: two workers: the second process stops before it computes anything, the claim of the first stands')
        release(cl_); ck(not os.path.exists(out_ + '.claim'), 'E1d: the holder releases its claim')
        fdo_ = os.fdopen; unl_ = os.unlink
        def badfd(fd, *a, **k): os.close(fd); raise OSError('disk full')
        os.fdopen = badfd
        try: x = rfm(claim, out_)
        finally: os.fdopen = fdo_
        ck(has(x, 'output_not_writable', 'token', 'was removed') and not os.path.exists(out_ + '.claim') and rf(claim, out_) == '', 'E1e: a failed token write is a typed refusal and the attempt removes the claim it has just created; the next attempt is not blocked'); os.unlink(out_ + '.claim')
        def badun(p_, *a, **k):
            raise OSError('no')
        os.fdopen = badfd; os.unlink = badun
        try: x = rfm(claim, out_)
        finally: os.fdopen = fdo_; os.unlink = unl_
        ck(has(x, 'output_not_writable', 'STAYS') and os.path.exists(out_ + '.claim'), 'E1e: if that removal fails the refusal says the claim stays'); os.unlink(out_ + '.claim')
        open(out_, 'w').write('committed by another'); seen_ = []; ck(rf(attempt, out_, lambda cl: seen_.append(1)) == 'output_exists' and seen_ == [] and not os.path.exists(out_ + '.claim') and open(out_).read() == 'committed by another', 'E1e: the final name is checked again right after the claim: nothing is touched, the claim is released'); os.unlink(out_)
        class NoFlush(io.StringIO):
            def flush(self): raise OSError('pipe closed')
        nfl_ = NoFlush()
        with contextlib.redirect_stdout(nfl_): x = rfm(attempt, out_, lambda cl: publish(L_, out_, D_, cl, lambda: None))
        ck(has(x, 'report_not_written') and not os.path.exists(out_) and not os.path.exists(out_ + '.claim'), 'E1e: standard output cannot be flushed: nothing is published'); [os.unlink(os.path.join(pb_, x_)) for x_ in tmps()]
        nf_ = os.path.join(pb_, 'a-file'); open(nf_, 'w').close(); seen_ = []; ck(rf(attempt, os.path.join(nf_, D_ + '.parquet'), lambda cl: seen_.append(1)) == 'output_not_writable' and rf(claim, os.path.join(pb_, 'no-folder', D_ + '.parquet')) == 'output_not_writable' and seen_ == [], 'version 7: a claim that cannot be created is a typed refusal, nothing is computed'); os.unlink(nf_)
        def boom(): raise RuntimeError('report failed')
        try: attempt(out_, lambda cl: publish(L_, out_, D_, cl, boom)); x = ''
        except RuntimeError as e_: x = str(e_)
        t1_ = tmps(); ck(x == 'report failed' and not os.path.exists(out_) and not os.path.exists(out_ + '.claim') and len(t1_) == 1, 'E1d: a report that fails: nothing is published, the claim is released, the temporary file stays')
        ck(rf(attempt, out_, lambda cl: publish(pd.concat([L_, L_.iloc[:1]]), out_, D_, cl, lambda: None)) == 'output_verify_failed' and rf(attempt, out_, lambda cl: publish(L_.assign(date=[D_, D_, '2026-11-16']), out_, D_, cl, lambda: None)) == 'output_verify_failed'
           and rf(attempt, out_, lambda cl: publish(L_.assign(date='2026-11-16'), out_, D_, cl, lambda: None)) == 'output_verify_failed' and not os.path.exists(out_) and not os.path.exists(out_ + '.claim') and len(tmps()) == 4, 'E1d: a table with one key twice, with two dates or of another date is not published')
        tf_ = os.path.join(pb_, t1_[0]); ck(rf(verify, tf_, D_, list(L_.columns), 3) == '' and rf(verify, tf_, D_, list(L_.columns) + ['z'], 3) == 'output_verify_failed' and rf(verify, tf_, D_, ['sym', 'date', 'm', 'x'], 3) == 'output_verify_failed' and rf(verify, tf_, D_, list(L_.columns), 4) == 'output_verify_failed', 'E1d: the read-back checks the column list and the row count')
        gf_ = os.path.join(pb_, 'garbage'); open(gf_, 'wb').write(b'xx'); ck(rf(verify, gf_, D_, list(L_.columns), 3) == 'output_verify_failed', 'E1d: a temporary file that cannot be read back'); os.unlink(gf_)
        other_ = lambda: open(out_, 'w').write('another attempt')
        ck(rf(attempt, out_, lambda cl: publish(L_, out_, D_, cl, other_)) == 'output_exists' and open(out_).read() == 'another attempt' and not os.path.exists(out_ + '.claim'), 'E1d: the final name appeared during the attempt: refused at the second check, the other table is not replaced'); os.unlink(out_)
        steal_ = lambda: open(out_ + '.claim', 'w').write('pid 1 host elsewhere start_ns 1\n')
        ck(rf(attempt, out_, lambda cl: publish(L_, out_, D_, cl, steal_)) == 'output_claimed' and not os.path.exists(out_) and open(out_ + '.claim').read().startswith('pid 1 host elsewhere'), 'E1d: the claim is no longer this attempt\'s: nothing published, the foreign claim is not removed')
        seen_ = []; ck(rf(attempt, out_, lambda cl: seen_.append(1)) == 'output_claimed' and seen_ == [] and os.path.exists(out_ + '.claim'), 'E1d: a claim left by a killed attempt refuses by name and is never removed by the builder'); os.unlink(out_ + '.claim')
        n_ = len(tmps()); said_ = []; buf_ = io.StringIO()
        with contextlib.redirect_stdout(buf_): tmp_, nb_, h_ = attempt(out_, lambda cl: publish(L_, out_, D_, cl, lambda: said_.append(os.path.exists(out_))))
        back_ = duckdb.connect().execute("SELECT date, sym, m, x FROM read_parquet('%s') ORDER BY sym, m" % out_).fetchall()
        ck(nb_ == os.path.getsize(out_) and h_ == md5(out_) and said_ == [False] and back_ == [(D_, 'AAA', 330, 1.0), (D_, 'AAA', 331, 2.0), (D_, 'BBB', 330, 3.0)] and not os.path.exists(tmp_) and len(tmps()) == n_ and n_ >= 6 and not os.path.exists(out_ + '.claim'),
           'E1d: a retry with old temporary files present publishes: the report runs before the final name exists, the rename is last, the claim is released, the old temporary files stay')
        ck(rf(out_ok, out_) == 'output_exists' and len({os.path.basename(x) for x in tmps()}) == n_, 'E1d: a published table is never written over; every attempt had a temporary name of its own')
        lg_ = os.path.join(pb_, 'build.log'); open(lg_, 'w').write('something\n' + buf_.getvalue()); m_ = _READY.fullmatch(buf_.getvalue().strip()); me_ = md5(os.path.abspath(__file__)); rec_ = buf_.getvalue()
        ck(m_ is not None and m_.groups()[:5] == (D_, me_, os.path.abspath(out_), str(nb_), h_) and str(os.getpid()) in m_.group(6), 'version 9: one "table ready" record with day, builder md5, absolute output path, bytes and md5 of the temporary file and the claim token is printed before the rename')
        with contextlib.redirect_stdout(io.StringIO()) as o_: r_ = published(D_, out_, lg_, ef)
        ck(r_ == (nb_, h_) and 'claim' not in o_.getvalue() and 'published' not in rec_.replace('table ready', ''), 'version 9: published: the table equals its record; no confirmation line is needed')
        open(out_ + '.claim', 'w').write('pid 1 host gone start_ns 1\n')
        with contextlib.redirect_stdout(io.StringIO()) as o_: published(D_, out_, lg_, ef)
        ck('reported, not removed' in o_.getvalue() and os.path.exists(out_ + '.claim'), 'version 9: published: a claim left beside a verified table is reported and stays'); os.unlink(out_ + '.claim')
        def lg(name, a_, b_): p_ = os.path.join(pb_, name); open(p_, 'w').write(rec_.replace(a_, b_)); return p_
        ck(rf(published, D_, out_, lg('o.log', os.path.abspath(out_), os.path.join(pb_, '2026-11-16.parquet')), ef) == 'published_unverified' and rf(published, D_, out_, lg('o2.log', os.path.abspath(out_), os.path.basename(out_)), ef) == 'published_unverified', 'version 9: published: a record of another output, or of the same name without its folder')
        ck(rf(published, D_, out_, lg('d.log', 'day ' + D_, 'day 2026-11-13'), ef) == 'published_unverified' and rf(published, '2026-11-13', out_, lg_, ef) == 'published_unverified' and rf(published, '2026-11-14', out_, lg_, ef) == 'not_a_trading_day' and rf(published, '20261117', out_, lg_, ef) == 'day_malformed', 'version 9: published: another day in the record or asked')
        ck(rf(published, D_, out_, lg('b.log', me_, '0' * 32), ef) == 'published_unverified', 'version 9: published: a record written by another builder')
        ck(rf(published, D_, out_, lg('m.log', h_, '0' * 32), ef) == 'published_unverified' and rf(published, D_, out_, lg('e.log', rec_, ''), ef) == 'published_unverified' and rf(published, D_, out_, os.path.join(pb_, 'no.log'), ef) == 'published_unverified', 'version 9: published: another md5, no record, no log')
        keep_ = open(out_, 'rb').read(); open(out_, 'ab').write(b'x'); ck(rf(published, D_, out_, lg_, ef) == 'published_unverified', 'version 9: published: a table changed after its commit'); open(out_, 'wb').write(keep_)
        tp_ = os.path.join(pb_, '.writing-x-' + D_ + '.parquet'); os.rename(out_, tp_); ck(rf(published, D_, out_, lg_, ef) == 'published_unverified', 'version 9: published: a flushed record with only a temporary file is unverified'); os.rename(tp_, out_)
        # erratum E1f, 2c (version 10): an access spy on the candidate. Every stat, lstat and open of the table's path is counted while `published` runs.
        import builtins
        def spied(*a):
            hits = []; st_, ls_, op_ = os.stat, os.lstat, builtins.open; mine_ = lambda p: isinstance(p, (str, bytes, os.PathLike)) and os.path.abspath(os.fspath(p)) == os.path.abspath(out_)
            def w(f, nm):
                def g(p, *x, **y):
                    if mine_(p): hits.append(nm)
                    return f(p, *x, **y)
                return g
            os.stat, os.lstat, builtins.open = w(st_, 'stat'), w(ls_, 'lstat'), w(op_, 'open')
            try:
                with contextlib.redirect_stdout(io.StringIO()): r = rf(published, *a)
            finally: os.stat, os.lstat, builtins.open = st_, ls_, op_
            return r, hits
        # erratum E1h, 4 (version 12): a log that is the candidate
        ck(spied(D_, out_, out_, ef) == ('published_log_is_candidate', []), 'E1h: published with the candidate given as its own log: refused with zero accesses to it')
        sl_ = os.path.join(pb_, 'link.log'); os.symlink(out_, sl_); ck(rf(published, D_, out_, sl_, ef) == 'published_log_is_candidate', 'E1h: a log that is a link to the candidate'); os.unlink(sl_)
        hd_ = os.path.join(pb_, 'hard'); os.mkdir(hd_); ho_ = os.path.join(hd_, D_ + '.parquet'); hl_ = os.path.join(hd_, 'build.log'); open(hl_, 'w').write(rec_.replace(os.path.abspath(out_), os.path.abspath(ho_))); os.link(hl_, ho_)
        ck(rf(published, D_, ho_, hl_, ef) == 'published_log_is_candidate', 'E1h: the candidate and the log are one file under two names'); os.unlink(ho_); open(ho_, 'wb').write(open(out_, 'rb').read())
        ck(rf(published, D_, ho_, hl_, ef) == '', 'E1h: valid neighbour: a log beside its candidate')
        # erratum E1i, 6 (version 13): the ORDER of accesses, not only the result. Every stat and open of the log's and the candidate's path is recorded in sequence.
        def order(day_, tab_, log_):
            ev = []; st_, op_ = os.stat, builtins.open; nm_ = {os.path.abspath(tab_): 'table', os.path.abspath(log_): 'log'}
            def w(f, what):
                def g(p, *x, **y):
                    if isinstance(p, (str, bytes, os.PathLike)) and os.path.abspath(os.fspath(p)) in nm_: ev.append(what + ' ' + nm_[os.path.abspath(os.fspath(p))])
                    return f(p, *x, **y)
                return g
            os.stat, builtins.open = w(st_, 'stat'), w(op_, 'open')
            try:
                with contextlib.redirect_stdout(io.StringIO()): r = rf(published, day_, tab_, log_, ef)
            finally: os.stat, builtins.open = st_, op_
            return r, ev
        hk_ = os.path.join(hd_, 'k'); os.mkdir(hk_); kl_ = os.path.join(hk_, 'build.log'); ko_ = os.path.join(hk_, D_ + '.parquet'); open(kl_, 'w').write(rec_.replace(os.path.abspath(out_), os.path.abspath(ko_))); os.link(kl_, ko_)
        r_, ev_ = order(D_, ko_, kl_); ck(r_ == 'published_log_is_candidate' and 'open log' not in ev_ and 'open table' not in ev_ and 'stat log' in ev_ and 'stat table' in ev_, 'E1i 6: log and candidate are two hard links of one file: refused before any content is read (accesses: %s)' % ', '.join(ev_))
        os.unlink(ko_); open(ko_, 'wb').write(open(out_, 'rb').read()); r_, ev_ = order(D_, ko_, kl_)
        ck(r_ == '' and 'open log' in ev_ and 'open table' in ev_ and max(i for i, e in enumerate(ev_) if e in ('stat log', 'stat table') and i < ev_.index('open log')) < ev_.index('open log') < ev_.index('open table') and ev_.index('stat table') < ev_.index('open log'), 'E1i 6: valid neighbour: two different files: the identity test of both names comes before the log is opened, the log before the candidate (accesses: %s)' % ', '.join(ev_))
        ck(order('2026-07-01', ko_, kl_) == ('sealed_day', []) , 'E1i 6: a sealed day is refused first: neither file is touched, not even for the identity test')
        # erratum E1i, 5 (version 13): line ends of the log. One record under LF, CRLF and a bare CR is found; two records are two records under each ending.
        r1_ = rec_.replace(os.path.abspath(out_), os.path.abspath(ko_)).strip(); r2_ = r1_.replace('md5 ' + h_, 'md5 ' + '0' * 32)
        for nm_e, e_ in (('LF', '\n'), ('CRLF', '\r\n'), ('CR', '\r')):
            open(kl_, 'wb').write(('something' + e_ + r1_ + e_ + 'more' + e_).encode()); ck(rf(published, D_, ko_, kl_, ef) == '', 'E1i 5: one record, line ends %s: found' % nm_e)
            # erratum E1j, 1 (version 14): exactly one record
            for nm_c, two_ in (('two identical records', r1_ + e_ + r1_), ('a stale record and the table\'s', r2_ + e_ + r1_), ('the table\'s record and a stale one', r1_ + e_ + r2_), ('two stale records', r2_ + e_ + r2_)):
                open(kl_, 'wb').write(('something' + e_ + two_ + e_).encode()); r_, ev_ = order(D_, ko_, kl_)
                ck(r_ == 'log_record_repeated' and 'open table' not in ev_, 'E1j 1: %s, line ends %s: refused as repeated before the table is opened' % (nm_c, nm_e))
            open(kl_, 'wb').write(('something' + e_ + r2_ + e_).encode()); ck(rf(published, D_, ko_, kl_, ef) == 'published_unverified', 'E1j 1: one record of another md5, line ends %s: unverified' % nm_e)
        open(kl_, 'wb').write((r1_.replace('; temporary', '\rx; temporary') + '\n').encode()); ck(rf(published, D_, ko_, kl_, ef) == 'published_unverified' and _READY.fullmatch(r1_.replace('; temporary', '\rx; temporary')) is None and _READY.fullmatch(r1_.replace('; temporary', '\nx; temporary')) is None, 'E1i 5: an output field with a CR or LF inside is not a record')
        r_, h_s = spied(D_, out_, lg_, ef); ck(r_ == '' and 'open' in h_s and 'stat' in h_s, 'version 10: the spy sees the accesses of a valid check (%s)' % ', '.join(sorted(set(h_s))))
        for d_, c_ in (('2026-07-15', 'sealed_day'), ('2026-07-01', 'sealed_day'), ('2026-11-20', 'sealed_day'), ('2027-01-04', 'sealed_day'), ('2026-11-14', 'not_a_trading_day'), ('2026-08-17', 'history_in_sealed_window'), ('2026-09-14', 'history_in_sealed_window'), ('2021-11-16', 'history_before_archive_start'), ('2025-11-28', 'early_close'), ('20261117', 'day_malformed')):
            ck(spied(d_, out_, lg_, ef) == (c_, []) and spied(d_, out_, os.path.join(pb_, 'no.log'), ef) == (c_, []), 'version 10 / E1f 2a: published %s is refused as %s before the log is read and with zero accesses to the candidate' % (d_, c_))
        ck(spied(D_, out_, lg_, os.path.join(pb_, 'no-early.txt')) == ('early_close_file_missing', []), 'version 10: published without the early-close list: refused, candidate untouched')
        un_ = (('published_unverified', []), ('published_unverified', ['stat']))
        ck(spied(D_, out_, lg('e2.log', rec_, ''), ef) in un_ and spied(D_, out_, os.path.join(pb_, 'no.log'), ef) in un_ and spied(D_, out_, lg('b2.log', me_, '0' * 32), ef) in un_
           and spied(D_, out_, lg('o3.log', os.path.abspath(out_), os.path.join(pb_, '2026-11-16.parquet')), ef) in un_ and spied(D_, out_, lg('d2.log', 'day ' + D_, 'day 2026-11-13'), ef) in un_,
           'version 10 / E1f 2a, as changed by E1i 6: a log without a record, no log, a record of another builder, of another output or of another day: published_unverified; of the candidate nothing is opened or sized, the one stat is the identity test made before the log is read')
        r_, h_s = spied(D_, out_, lg('m2.log', h_, '0' * 32), ef); ck(r_ == 'published_unverified' and 'open' in h_s, 'version 10: a record of this day, builder and output with another md5: the candidate is read and refused')
        r_, h_s = spied(D_, out_, lg('z2.log', 'bytes %d;' % nb_, 'bytes %d;' % (nb_ + 1)), ef); ck(r_ == 'published_unverified', 'version 10: a record with other bytes')
        ck(spied(D_, out_, lg('rec.log', 'never there', 'x') , ef)[0] == '' and 'published:' not in open(lg_).read(), 'version 10 / E1f 2b: the recovery case: the record exists, no confirmation line after the rename, the table equals the record: passes')
        gone_ = os.path.join(pb_, '2026-11-13.parquet'); lgg_ = lg('g.log', os.path.abspath(out_), gone_); ck(rf(published, D_, gone_, lgg_, ef) == 'published_unverified', 'version 10: a record whose table is not there')
        ck(cut_minute('09:45') == 345 and cut_minute('04:00') == 0 and cut_minute('19:59') == 959 and all(rf(cut_minute, z) == 'cut_malformed' for z in ('0945', '9:45', '03:59', '20:00', '09:60', '09:45:00', '')) and rf(prefix, bars, ty, '2026-11-17', '0945') == 'cut_malformed', 'E1c: a prefix cut is HH:MM inside 04:00 .. 19:59')
        ck(rf(bounds_ok, '2026-06-01', '2026-06-30') == '' and rf(bounds_ok, '20260601', '2026-06-30') == 'bound_malformed' and rf(bounds_ok, '2026-06-30', '2026-06-01') == 'bound_malformed' and rf(coverage, src, ef, '2026-06-30', '2026-06-01') == 'bound_malformed' and rf(calcheck, '-', src, '2026-06-01', '2026-6-30') == 'bound_malformed', 'E1c: bounds of coverage and calcheck')
        hid = os.path.join(os.path.dirname(os.path.abspath(__file__)), CALF); os.rename(hid, hid + '.away')
        try: x = rf(calendar)
        finally: os.rename(hid + '.away', hid)
        ck(x == 'calendar_file_missing', 'E1c: no calendar file'); hid = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'wm2tk.py'); os.rename(hid, hid + '.away')
        try: x = rf(load_wm2)
        finally: os.rename(hid + '.away', hid)
        ck(x == 'table_code_missing', 'E1c: a pinned table file is absent')
        import types as _ty
        ck(rf(codes, '12,ab') == 'code_token_unreadable' and codes('12, 37') == frozenset((12, 37)), 'E1c: a condition code that is not a number')
        cf_ = os.path.join(root, 'cal.txt'); open(cf_, 'w').write('2026-06-02\n2026-06-01\n'); ck(rf(calendar, cf_) == 'calendar_file_md5' and rf(calendar, cf_, md5(cf_)) == 'calendar_file_unreadable', 'E1c: a calendar that is not the pinned one, or not ascending')
        open(cf_, 'w').write('2026-06-01\n2026-06-01\n'); ck(rf(calendar, cf_, md5(cf_)) == 'calendar_file_unreadable', 'E1c: a calendar with a day twice')
        ck(rf(opened_ok, [os.path.join(bars, d + '.csv.gz') for d in nd], nd) == '' and rf(opened_ok, [os.path.join(bars, d + '.csv.gz') for d in nd[1:]], nd) == 'bar_files_opened_differ', 'E1c: the files opened are the list checked')
        op_ = [os.path.join(bars, d + '.csv.gz') for d in nd]; r0_ = bars_readable(bars, nd); ck(rf(files_same, r0_, op_) == '', 'E1c: bar files unchanged between the check and the build')
        keep_ = open(op_[2], 'rb').read(); open(op_[2], 'ab').write(gzip.compress(b'AAA,1,1,1,1,1,1,1\n')); ck(rf(files_same, r0_, op_) == 'bar_files_opened_differ', 'E1c: a bar file that changed after the check'); open(op_[2], 'wb').write(keep_)
        ck(rf(tickers_ok, ['A', 'AA', 'B']) == '' and rf(tickers_ok, ['B', 'A']) == 'tickers_not_ascending' and rf(tickers_ok, ['A', 'A']) == 'tickers_not_ascending' and rf(tickers_ok, ['A', 'Bé']) == 'tickers_not_ascending', 'E1c: tickers ASCII and ascending')
        ck(rf(rows_ok, None, 'd') == 'no_table_rows' and rf(rows_ok, pd.DataFrame({'a': []}), 'd') == 'no_table_rows' and rf(rows_ok, pd.DataFrame({'a': [1]}), 'd') == '', 'E1c: a day without table rows')
        ck(rf(gaps_refuse, 'd', 0, 0) == '' and rf(gaps_refuse, 'd', 3, 0) == 'price_row_missing' and rf(gaps_refuse, 'd', 0, 2) == 'early_quote_price_row_missing', 'E1c: price rows missing')
        pf_ = os.path.join(root, 'pin'); os.mkdir(pf_); open(os.path.join(pf_, 'a.py'), 'w').write('x'); ck(rf(pins_ok, pf_, {'a.py': md5(os.path.join(pf_, 'a.py'))}) == '' and rf(pins_ok, pf_, {'a.py': '0' * 32}) == 'table_code_md5' and rf(pins_ok, pf_, {'a.py': '0' * 32, 'b.py': '0' * 32}) == 'table_code_missing', 'E1c: pinned table files')
        n2_ = 'N2 = %d\n' % N2_PIN; ck(patched('one two\n' + n2_, (('x', 'one', 'uno'),)) == 'uno two\n' + n2_ and rf(patched, 'one one\n' + n2_, (('x', 'one', 'uno'),)) == 'table_code_replacement' and rf(patched, 'two\n' + n2_, (('x', 'one', 'uno'),)) == 'table_code_replacement'
           and rf(patched, 'one two\n' + n2_, (('x', 'one', 'two'), ('y', 'two', 'dos'))) == 'table_code_replacement' and rf(patched, 'one two\nN2 = %d\n' % (N2_PIN + 1), (('x', 'one', 'uno'),)) == 'table_code_settings', 'E1c: a declared replacement applies exactly once; second list depth')
        ok_ = dict(NDAYS=HIST, SEAL=list(SEAL), HARD_LAST=HARD_LAST, TOPN=TOPN_PIN, LAG=0, REF=None, TK=None, TSPAN=None)
        ck(rf(settings_ok, _ty.SimpleNamespace(**ok_)) == '' and all(rf(settings_ok, _ty.SimpleNamespace(**dict(ok_, **{k_: v_}))) == 'table_code_settings' for k_, v_ in (('NDAYS', HIST + 1), ('SEAL', ['2026-07-01', '2026-08-13']), ('HARD_LAST', '2026-11-20'), ('TOPN', TOPN_PIN + 1), ('LAG', 1), ('REF', 'x'), ('TK', 'x'), ('TSPAN', 5))), 'E1c: settings of the table code')
        ck(rf(load_old_rule, 'v9') == 'old_rule_unknown' and rf(load_old_rule, 'v2', ('0' * 32, V3_WM2L1)) == 'old_rule_md5' and rf(load_old_rule, 'v3', (V2_WM2L1, '0' * 32)) == 'old_rule_md5', 'E1c: the old rule of keydiff')
        if os.environ.get(ENVREF[0]) is None:
            os.environ[ENVREF[0]] = '1'
            try: x = rf(env_refuse)
            finally: del os.environ[ENVREF[0]]
            ck(x == 'table_variable_set' and rf(env_refuse) == '', 'E1c: a variable of the table code is set')
        # erratum E1i (version 13): each closed case with the file changed between the readings
        import builtins as bi_
        i1 = os.path.join(td, 'e1i'); ib_ = os.path.join(i1, 'minute_aggs'); os.makedirs(ib_); os.mkdir(os.path.join(i1, 'reference')); isp_ = os.path.join(i1, 'reference', 'splits.jsonl')
        for d in [x for x in cal if '2026-10-20' <= x <= '2026-11-17']: open(os.path.join(ib_, d + '.csv.gz'), 'w').close()
        sl_ = lambda to_: '{"execution_date": "2026-11-17", "ticker": "AAA", "split_from": 1, "split_to": %d}\n' % to_
        open(isp_, 'w').write(sl_(2)); ism_ = md5(isp_); cap_ = real_inputs(ib_, ty, '2026-11-17', pins=(tm, ism_))[1]
        def adj_(c_=None):
            c_ = c_ or cap_; pv_ = {'AAA': 100.0, 'BBB': 7.0}; _split(splits_taken(c_), '2026-11-17', pv_, {}, []); return pv_, c_.path, c_.digest
        ck(adj_() == ({'AAA': 50.0, 'BBB': 7.0}, isp_, ism_), 'E1i 1: the pinned 1-for-2 split takes a previous close of 100 to 50; the digest is that of the captured bytes')
        open(isp_, 'w').write(sl_(4)); ck(md5(isp_) != ism_ and adj_() == ({'AAA': 50.0, 'BBB': 7.0}, isp_, ism_), 'E1i 1: the splits file changed after the capture: the adjustment is still 50 (never 25) and the reported digest is still the pinned one')
        os.unlink(isp_); ck(adj_() == ({'AAA': 50.0, 'BBB': 7.0}, isp_, ism_), 'E1i 1: the splits file removed after the capture: nothing changes')
        open(isp_, 'w').write(sl_(4)); ck(rf(real_inputs, ib_, ty, '2026-11-17', pins=(tm, ism_)) == 'splits_file_md5', 'E1i 1: a new run with the changed file is refused by the pin')
        open(isp_, 'w').write(sl_(2)); cnt_ = []; op_ = bi_.open
        def co_(p, *x, **y):
            if isinstance(p, (str, bytes, os.PathLike)) and os.path.abspath(os.fspath(p)) == isp_: cnt_.append(1)
            return op_(p, *x, **y)
        bi_.open = co_
        try: c2_ = real_inputs(ib_, ty, '2026-11-17', pins=(tm, ism_))[1]; r_ = adj_(c2_); r2_ = adj_(c2_)
        finally: bi_.open = op_
        ck(len(cnt_) == 1 and r_ == r2_ == ({'AAA': 50.0, 'BBB': 7.0}, isp_, ism_), 'E1i 1: the splits file is opened once for the check, the parse and the report')
        # erratum E1j, 2 (version 14): the name re-pointed after the verification (Codex's case): reference/splits.jsonl is a link; after real_inputs it points to a 1-for-4 file
        ja_ = os.path.join(i1, 'splits-a.jsonl'); jb_ = os.path.join(i1, 'splits-b.jsonl'); open(ja_, 'w').write(sl_(2)); open(jb_, 'w').write(sl_(4)); os.unlink(isp_); os.symlink(ja_, isp_)
        c3_ = real_inputs(ib_, ty, '2026-11-17', pins=(tm, ism_))[1]; os.unlink(isp_); os.symlink(jb_, isp_)
        ck(md5(isp_) != ism_ and adj_(c3_) == ({'AAA': 50.0, 'BBB': 7.0}, isp_, ism_), 'E1j 2: the splits name re-pointed to another file after the check: the factors and the digest stay those of the verified capture (50, never 25)')
        ck(rf(real_inputs, ib_, ty, '2026-11-17', pins=(tm, ism_)) == 'splits_file_md5', 'E1j 2: a new run through the re-pointed name is refused by the pin')
        spy_n = lambda *a, **k: (_ for _ in ()).throw(AssertionError('a bar file was read'))
        ck(rf(history, ib_, None, ['2026-11-14', '2026-11-17'], spy_n, None) == 'splits_not_captured' and rf(table, ib_, '-', ['2026-11-17'], spy_n, None) == 'splits_not_captured' and rf(history, ib_, None, ['2026-11-17'], spy_n, {'2026-11-17': []}) == 'splits_not_captured', 'E1j 2: a table asked for without the verified splits object is refused before any bar file is read (None, or a plain dict of factors)')
        os.unlink(isp_); n0_ = os.path.join(i1, 'nosplit', 'minute_aggs'); os.makedirs(n0_); f0_ = splits_fixture(n0_); ck((f0_.factors, f0_.path, f0_.digest) == ({}, None, None) and splits_taken(f0_) == {}, 'E1j 2: the fixtures\' own reader: no splits file = no adjustment')
        ck(rfm(splits_parse, b'{"execution_date": "2026-11-17", "ticker": "AAA", "split_from": 1, "split_to": 0}\n').startswith('REFUSED: splits_file_unreadable: line 1') and rf(splits_parse, b'not json\n') == 'splits_file_unreadable' and rf(splits_parse, (sl_(2) + '\n' + sl_(2)).encode()) == 'splits_file_unreadable' and splits_parse((sl_(2).strip() + '\r\n').encode()) == {'2026-11-17': [('AAA', 0.5)]} and splits_parse(b'') == {}, 'E1j 2: a line that is not a split record (split_to 0, not JSON, an empty line) is a typed refusal; valid neighbours: CRLF line end, an empty file')
        # 2 the manifest
        it_ = [os.path.join(i1, x) for x in ('tk', 'px', 'man')]; [os.mkdir(x) for x in it_]; open(os.path.join(it_[0], D_ + '.parquet'), 'wb').write(b'tick'); open(os.path.join(it_[1], D_ + '.parquet'), 'wb').write(b'price')
        imf_ = os.path.join(it_[2], D_ + '.json'); ig_ = dict(date=D_, script_md5=FROZEN, files={'tk': {'bytes': 4, 'md5': hashlib.md5(b'tick').hexdigest()}, 'px': {'bytes': 5, 'md5': hashlib.md5(b'price').hexdigest()}})
        json.dump(ig_, open(imf_, 'w')); im_ = md5(imf_); cnt_ = []
        def cm_(p, *x, **y):
            if isinstance(p, (str, bytes, os.PathLike)) and os.path.abspath(os.fspath(p)) == imf_: cnt_.append(1)
            return op_(p, *x, **y)
        bi_.open = cm_
        try: tkf_i, pxf_i, _, mf_i, got_i = tick_inputs(D_, *it_)
        finally: bi_.open = op_
        json.dump(dict(ig_, date='2026-11-16'), open(imf_, 'w'))
        ck(len(cnt_) == 1 and got_i['manifest'] == im_ and mf_i['date'] == D_ and md5(imf_) != im_, 'E1i 2: the manifest is opened once; what was parsed and the digest in the record are of that capture, also when the file changes afterwards')
        ck(rf(tick_inputs, D_, *it_) == 'manifest_wrong_day', 'E1i 2: a new run with the changed manifest is refused'); json.dump(ig_, open(imf_, 'w'))
        # 3 tk and px read again after their last use
        ck(rf(ticks_same, got_i, tkf_i, pxf_i, mf_i) == '', 'E1i 3: valid neighbour: tick and price file unchanged after their use')
        open(pxf_i, 'wb').write(b'pricf'); ck(rf(ticks_same, got_i, tkf_i, pxf_i, mf_i) == 'tick_files_changed', 'E1i 3: the price file changed between the check and the end of its use (same size, other bytes)'); open(pxf_i, 'wb').write(b'price')
        open(tkf_i, 'wb').write(b'tick+'); ck(rf(ticks_same, got_i, tkf_i, pxf_i, mf_i) == 'tick_files_changed', 'E1i 3: the tick file grew'); open(tkf_i, 'wb').write(b'tick'); ck(rf(ticks_same, got_i, tkf_i, pxf_i, mf_i) == '', 'E1i 3: put back: passes again (a change that is put back between the readings is a named limit)')
        # 4 the table code read again
        ic_ = os.path.join(i1, 'code'); os.mkdir(ic_); open(os.path.join(ic_, 'a.py'), 'w').write('x = 1\n'); open(os.path.join(ic_, 'b.py'), 'w').write('y = 1\n'); ipin_ = {'a.py': md5(os.path.join(ic_, 'a.py')), 'b.py': md5(os.path.join(ic_, 'b.py'))}
        irec_ = pins_ok(ic_, ipin_); ck(irec_ == {'a.py': (6, ipin_['a.py']), 'b.py': (6, ipin_['b.py'])} and [x[0] for x in code_same('t', ic_, irec_)] == ['a.py', 'b.py'], 'E1i 4: valid neighbour: the code files read again are the ones checked before the import')
        open(os.path.join(ic_, 'b.py'), 'w').write('y = 2\n'); x_ = rfm(code_same, 'at the end of the run', ic_, irec_); ck(has(x_, 'code_files_changed', 'at the end of the run', 'b.py') and 'a.py' not in x_, 'E1i 4: a code file changed after the import (same size): refused, the file named')
        os.unlink(os.path.join(ic_, 'b.py')); ck(rf(code_same, 't', ic_, irec_) == 'code_files_changed' and rf(pins_ok, ic_, ipin_) == 'table_code_missing', 'E1i 4: a code file removed after the import')
        wi_, wd_i = load_wm2(); ck(set(_CODE) >= set(PIN) | {'wm2_l1.py'} and _CODE['wm2_l1.py'][1] == wd_i == md5(wi_.__file__) and all(_CODE[k][1] == PIN[k] for k in PIN) and len(code_same('t')) == len(_CODE), 'E1i 4: after loading, the record holds the pinned files and wm2_l1.py; the file that was imported has the md5 of the text that was written')
        sv_ = dict(_CODE); _CODE['wm.py'] = (_CODE['wm.py'][0], '0' * 32)
        try: ck(rf(code_same, 'after the table code was loaded') == 'code_files_changed' and rf(load_wm2) == '', 'E1i 4: a record that is not the file refuses; a fresh load takes the record from the pinned files again')
        finally: _CODE.clear(); _CODE.update(sv_)
        allc_ = sorted(set(re.findall(r"REFUSED: ([a-z_0-9]+): ", src_))); tst_ = inspect.getsource(test); hdr_ = src_[:src_.index('\nimport ')]
        ck(all("'%s'" % c_ in tst_ for c_ in allc_) and all(re.search(r'\b%s\b' % c_, hdr_) for c_ in allc_), 'E1c: every refusal code has a fixture and is listed in the header (%d codes)' % len(allc_))

        ck(len([l_ for l_ in re.findall(r"SystemExit\('REFUSED: ([^']{0,60})", src_) if not re.match(r'(%s[a-z_0-9]*|[a-z_0-9]+): ', l_)]) == 0, 'E1c: every refusal starts with a code word')
        with gzip.open(os.path.join(bars, nd[3] + '.csv.gz'), 'wt') as g: g.write('')
        try: bars_readable(bars, nd); x = ''
        except SystemExit as e_: x = str(e_)
        ck('bar_file_unreadable' in x and nd[3] in x, 'E1c: an empty gzip names its file')
        with gzip.open(os.path.join(bars, nd[3] + '.csv.gz'), 'wt') as g: g.write('ticker,volume,open,close,high,window_start\n')
        ck(rf(bars_readable, bars, nd) == 'bar_file_unreadable', 'E1c: a bar file without the low column')
        with gzip.open(os.path.join(bars, nd[3] + '.csv.gz'), 'wt') as g: g.write(','.join(BARCOLS) + '\n')
        ck(rf(bars_readable, bars, nd) == 'bar_file_unreadable', 'E1c: a bar file with a header and no row')
        ex_ = os.path.join(root, 'there.parquet'); open(ex_, 'w').close(); ck(rf(out_ok, os.path.join(root, 'nofolder', 'x.parquet')) == 'output_folder_missing' and rf(out_ok, ex_) == 'output_exists' and rf(out_ok, os.path.join(root, 'new.parquet')) == '', 'E1c: the output folder must be there and the output must not')
        tkd_ = os.path.join(root, 'tk'); pxd_ = os.path.join(root, 'px'); mand_ = os.path.join(root, 'man'); [os.mkdir(x) for x in (tkd_, pxd_, mand_)]; ti = lambda: rf(tick_inputs, '2026-11-17', tkd_, pxd_, mand_); mfp = os.path.join(mand_, '2026-11-17.json')
        ck(ti() == 'manifest_missing', 'E1c: no manifest'); open(mfp, 'w').write('{not json'); ck(ti() == 'manifest_unreadable', 'E1c: a manifest that is not JSON')
        good = dict(date='2026-11-17', script_md5=FROZEN, files=dict(tk=dict(bytes=3, md5=hashlib.md5(b'abc').hexdigest()), px=dict(bytes=2, md5=hashlib.md5(b'de').hexdigest())))
        for miss in ('date', 'script_md5', 'files'): json.dump({k: v for k, v in good.items() if k != miss}, open(mfp, 'w')); ck(ti() == 'manifest_unreadable', 'E1c: a manifest without %s' % miss)
        g2 = json.loads(json.dumps(good)); del g2['files']['px']; json.dump(g2, open(mfp, 'w')); ck(ti() == 'manifest_unreadable', 'E1c: a manifest without files.px'); g2 = json.loads(json.dumps(good)); del g2['files']['tk']['md5']; json.dump(g2, open(mfp, 'w')); ck(ti() == 'manifest_unreadable', 'E1c: a manifest without the md5 of the tick file')
        json.dump(good, open(mfp, 'w')); ck(ti() == 'tick_file_missing', 'E1c: no tick file'); open(os.path.join(tkd_, '2026-11-17.parquet'), 'wb').write(b'abc'); ck(ti() == 'price_file_missing', 'E1c: no price file'); open(os.path.join(pxd_, '2026-11-17.parquet'), 'wb').write(b'dX')
        ck(ti() == 'manifest_mismatch', 'a price file that is not the manifest\'s'); open(os.path.join(pxd_, '2026-11-17.parquet'), 'wb').write(b'deX'); ck(ti() == 'manifest_mismatch', 'E1c: a price file of another size')
        g2 = dict(good, date='2026-11-16'); json.dump(g2, open(mfp, 'w')); ck(ti() == 'manifest_wrong_day', 'E1c: a manifest of another day'); g2 = dict(good, script_md5='0' * 32); json.dump(g2, open(mfp, 'w')); ck(ti() == 'manifest_mismatch', 'E1c: a manifest of another converter'); json.dump(good, open(mfp, 'w'))
        open(os.path.join(pxd_, '2026-11-17.parquet'), 'wb').write(b'de'); ck(tick_inputs('2026-11-17', tkd_, pxd_, mand_)[4] == {'tk': good['files']['tk']['md5'], 'px': good['files']['px']['md5'], 'manifest': md5(mfp)}, 'E1c: the three tick inputs present and consistent (E1i 2: with the digest of the manifest as read)')
        json.dump(dict(good, files=str(good['files'])), open(mfp, 'w')); ck(tick_inputs('2026-11-17', tkd_, pxd_, mand_)[3]['date'] == '2026-11-17', 'a manifest whose files entry is stored as text is read as before')
        # the first HIST sessions of the archive are not targets
        i0 = cal.index(ARCH_FIRST); 
        try: needed(cal, cs, cal[i0 + HIST - 1]); x = ''
        except SystemExit as e_: x = str(e_)
        ck('history_before_archive_start' in x and needed(cal, cs, cal[i0 + HIST])[0] == ARCH_FIRST and len(needed(cal, cs, cal[i0 + HIST])) == HIST + 1, 'E1a: the first 20 sessions of the archive are not targets')
        try: needed(cal, cs, cal[i0 - 1]); x = ''
        except SystemExit as e_: x = str(e_)
        ck('history_before_archive_start' in x, 'E1a: a day before the archive')
        # run rule: a set table variable refuses
        for k_ in ENVREF:
            os.environ[k_] = '0'
            try: env_refuse(); x = ''
            except SystemExit as e_: x = str(e_)
            del os.environ[k_]; ck(k_ in x, 'run rule: %s set refuses' % k_)
        env_refuse()
        # E6a: a stop triggered in the minute before the flatten keeps section 4 (0, 2, 5, 15 s of the flatten minute, then 5 s of each later minute); the flatten's own sale starts at second 5
        def sf(mod): M = blank(); M['tlo'][0, 709] = 9.9; mod(M); return one(M, q=690)
        def m1(M): M['qb'][0][0, 710] = 9.0; M['qb'][5][0, 710] = 8.0
        r = sf(m1); ck(r['b_cls'] == 2 and r['b_dec'] == 709 and r['b_min'] == 710 and r['b_sec'] == 0 and eq(r['b_val'], (9.0 / 10.02 - 1) / u), 'E6a: stop triggered at flatten - 1, bid 9 at second 0 and 8 at second 5: fills at 9')
        ck(r['h_min'] == 710 and r['h_sec'] == 2 and eq(r['h_val'], 8.0 / 10.02 - 1), 'E6a: the flatten sale of the same row starts at second 5')
        def m2(M): M['bb'][0][0, 710] = True; M['qb'][2][0, 710] = 9.5; M['qb'][5][0, 710] = 8.0
        r = sf(m2); ck(r['b_min'] == 710 and r['b_sec'] == 1 and eq(r['b_val'], (9.5 / 10.02 - 1) / u), 'E6a: a bid only at second 2')
        def m3(M):
            for S in MARKS: M['bb'][S][0, 710] = True
            M['qb'][5][0, 711] = 9.4; M['qb'][0][0, 711] = 9.9; M['qb'][15][0, 711] = 9.8
        r = sf(m3); ck(r['b_min'] == 711 and r['b_sec'] == 2 and eq(r['b_val'], (9.4 / 10.02 - 1) / u), 'E6a: no bid at the four marks of the flatten minute: second 5 of the next minute')
        def m4(M):
            for S in MARKS: M['bb'][S][0, 710:] = True
        r = sf(m4); ck(r['b_cls'] == 2 and r['b_ret_ok'] == 0 and r['b_mark_ok'] == 1, 'E6a: never filled = marked')
        # E9a: the chosen quote update before 04:00 needs its px row; a present row with unknown codes is another case
        def mini(nm, seedrow, pst):
            tk_ = pd.DataFrame({'sym': ['AAA', 'AAA', 'AAA'], 'm': [230, 238, 250], 'qn': [1, 1, 1], 'tn': [0, 0, 1], 'qbid': [9.0, 9.9, 10.0], 'qask': [9.5, 10.1, 10.02], 'qbsl': [100.0] * 3, 'qasl': [100.0] * 3, 'tlo': [np.nan, np.nan, 10.0], 'thi': [np.nan, np.nan, 10.0]})
            rows = [230, 250] + ([238] if seedrow else []); px_ = pd.DataFrame({'sym': ['AAA'] * len(rows), 'm': rows})
            for c in PXCOLS: px_[c] = '' if c.startswith(('pqi', 'pqc')) else (1 if c.startswith('pqt') else 1.0)
            if seedrow: px_.loc[px_.m == 238, 'pstE'] = pst
            con = duckdb.connect(); con.register('a_', tk_); con.register('b_', px_); tf = os.path.join(td, nm + '-tk.parquet'); pf = os.path.join(td, nm + '-px.parquet'); con.execute("COPY a_ TO '%s' (FORMAT PARQUET)" % tf); con.execute("COPY b_ TO '%s' (FORMAT PARQUET)" % pf)
            return load_day('x', np.array(['AAA'], dtype=object), tf, pf, np.full((1, NM), np.nan))[0]
        M = mini('gap', False, 1.0); ck(M['init']['has'][0] and M['init']['m'][0] == 238 and seed_gaps(M['init']) == 1, 'E9a: the last update before 04:00 (03:58) has no px row: the day is refused (the earlier update at 03:50 is not taken instead)')
        M = mini('ok', True, 1.0); ck(seed_gaps(M['init']) == 0 and M['init']['pstE'][0] == 1 and eq(derive(M)['TQA'][0, 5], 10.1), 'E9a: the px row is there')
        M = mini('unk', True, np.nan); D = derive(M); ck(seed_gaps(M['init']) == 0 and np.isnan(M['init']['pstE'][0]) and D['ERS'][0, 5] == 1 and np.isnan(D['EA'][0, 5]), 'E9a: a present px row with unknown codes is not a gap; the standing quote is then "side validity unresolved"')
        # E2b: lists rank only names that have traded so far, ties by ticker; the table of a day cut after minute c equals the table of the whole day up to c
        w, _ = load_wm2(); N_ = 1300; day_ = '2026-11-17'; z0 = int(datetime.datetime.fromisoformat(day_).replace(tzinfo=zoneinfo.ZoneInfo('America/New_York')).timestamp()) * 10 ** 9 + OFF * 60 * 10 ** 9
        nm_ = ['N%04d' % i for i in range(N_)]; late = 'N0432Z'; rows = []   # the late name stands at position 433 in ticker order and first prints after the cut
        def bar(tk__, m, close, vol): rows.append(dict(ticker=tk__, volume=vol, open=close, close=close, high=close, low=close, window_start=z0 + m * 60 * 10 ** 9, transactions=1))
        for i, x in enumerate(nm_):
            g = i // 3 + 1; bar(x, 0, 10.0 * (1 + g * 1e-4), 1 + i % 5)   # changes tied in groups of three, cumulative volumes tied in five levels
            if i % 7 == 0: bar(x, 1, 10.0 * (1 + g * 1e-4), 1)
            bar(x, 400, 10.0 * (1 - g * 1e-4), 1 + i % 4); bar(x, 730, 10.0 * (1 - g * 1e-4) * (1 + (i // 4) * 1e-5), 1 + i % 3)
        for m in (2, 401, 731): bar(late, m, 10.5, 100000)
        for m in (731, 735): bar('N0900Z', m, 9.5, 100000)   # a second late name, first printed in the post-market
        fb = os.path.join(td, day_ + '.csv.gz'); pd.DataFrame(rows).to_csv(fb, index=False, compression='gzip'); st_ = ({x: 10.0 for x in nm_ + [late, 'N0900Z']}, {}, collections.deque(maxlen=HIST))
        full = one_table(w, fb, st_, None); capf = full[0]; ck(len(capf['tick']) == N_ + 2 and list(capf['tick']).index(late) == 433 and len(full[1]) > 0, 'E2b fixture: the table of the synthetic day')
        # erratum E1g: the stock whose ticker is NA, on the generated day: in the types file as text, in the bars, and so in the table (same bars as the late name: same number of row keys)
        rows_na = rows + [dict(r_, ticker='NA') for r_ in rows if r_['ticker'] == late]; fbn = os.path.join(td, 'na'); os.mkdir(fbn); fbn = os.path.join(fbn, day_ + '.csv.gz'); pd.DataFrame(rows_na).to_csv(fbn, index=False, compression='gzip')
        tyn = os.path.join(td, 'types-na.csv'); open(tyn, 'w').write('ticker,type\n' + ''.join('%s,CS\n' % x for x in nm_ + [late, 'N0900Z', 'NA'])); stn = ({x: 10.0 for x in nm_ + [late, 'N0900Z', 'NA']}, {}, collections.deque(maxlen=HIST))
        fna = one_table(w, fbn, stn, read_types(tyn)); kl_ = sorted(m for s_, m in fna[1] if s_ == late); kn2_ = sorted(m for s_, m in fna[1] if s_ == 'NA')
        ck('NA' in list(fna[0]['tick']) and len(fna[0]['tick']) == N_ + 3 and kn2_ == kl_ and len(kn2_) > 0 and set(fna[2].sym[fna[2].sym == 'NA']) == {'NA'}, 'E1g: the stock NA goes from the types file and the bars into the table of the generated day (%d row keys, as its twin)' % len(kn2_))
        tyo = os.path.join(td, 'types-no-na.csv'); open(tyo, 'w').write('ticker,type\n' + ''.join('%s,CS\n' % x for x in nm_ + [late, 'N0900Z'])); ck('NA' not in list(one_table(w, fbn, stn, read_types(tyo))[0]['tick']), 'E1g: without its types line the stock NA is not in the table')
        # version 11 (E1g): keydiff's reference side = the same table code with the types minus the lost tickers; captures aligned by ticker; the swap reason
        import l1compare as lc_
        ck(all(lost_ticker(x) == lc_.lost_ticker(x) for x in ('NA', 'NULL', 'None', 'nan', 'NaN', 'N/A', 'null', 'AAA', 'NAN', 'TRUE', 'INF', '', 'A')) and lost_ticker('NA') and not lost_ticker('NAN'), 'version 11: the builder\'s class of lost tickers is the comparator\'s')
        tyall = read_types(tyn); ck({t for t in tyall if lost_ticker(t)} == {'NA'} and tyall - {'NA'} == read_types(tyo), 'version 11: the reference types are the types minus the lost tickers')
        fref = one_table(w, fbn, stn, read_types(tyo)); cr_ = align(fref[0], fna[0]['tick']); a_ = fna[1] - fref[1]; r_ = fref[1] - fna[1]; ina_ = list(fna[0]['tick']).index('NA')
        ck(cr_['tick'] == list(fna[0]['tick']) and not cr_['inl'][ina_].any() and not cr_['has'][ina_].any() and np.isnan(cr_['c'][ina_]).all() and keys_of(cr_, w.MINP) == fref[1] and cr_['comp'].shape == fna[0]['comp'].shape, 'version 11: the reference capture aligned by ticker keeps its keys; the lost ticker is on no list there')
        rw, _g = key_reasons(fna[0], cr_, a_, r_, w.MINP, w.TOPN, frozenset(['NA'])); rr_ = [x for x in rw if x['change'] == 'removed']
        ck({s_ for s_, _ in a_} == {'NA'} and len(r_) > 0 and all(x['reason'].startswith('E1g: the stock was not in the reference universe') for x in rw if x['change'] == 'added') and all(x['reason'].startswith('E1g: place on the') and 'taken by NA' in x['reason'] for x in rr_) and len(rw) == len(a_) + len(r_),
           'version 11: on the generated day the stock NA adds its keys and takes a list place: every added and removed key is explained (%d added, %d removed in %d stock-sessions)' % (len(a_), len(r_), len({(x['sym'], x['session']) for x in rr_})))
        rw, _g = key_reasons(fna[0], cr_, a_, r_, w.MINP, w.TOPN, frozenset()); ck(all(x['reason'] == 'UNEXPLAINED' for x in rw), 'version 11: with no lost ticker named, nothing is explained (a removal with no lost ticker in that list fails)')
        dsym_ = rr_[0]['sym']; di_ = list(fna[0]['tick']).index(dsym_); dm_ = rr_[0]['watch_start_old']; kk_ = [k for k in (0, 2, 4, 6, 8) if fna[0]['comp'][k, di_, dm_] != cr_['comp'][k, di_, dm_]]
        two_ = dict(fna[0]); two_['comp'] = fna[0]['comp'].copy(); oth_ = int(np.flatnonzero(two_['comp'][kk_[0]][:, dm_] & cr_['comp'][kk_[0]][:, dm_])[0]); two_['comp'][kk_[0], oth_, dm_] = False
        rw2, _g = key_reasons(two_, cr_, a_, r_, w.MINP, w.TOPN, frozenset(['NA'])); ck(all(x['reason'] == 'UNEXPLAINED' for x in rw2 if x['sym'] == dsym_ and x['session'] == rr_[0]['session']), 'version 11: a list that differs from the reference by two names does not explain the removal')
        mid_ = sorted(k for k in r_ if k[0] == dsym_); rw3, _g = key_reasons(fna[0], cr_, a_, r_ - {mid_[len(mid_) // 2]}, w.MINP, w.TOPN, frozenset(['NA'])) if len(mid_) > 1 else (None, None)
        ck(rw3 is None or all(x['reason'] == 'UNEXPLAINED' for x in rw3 if x['sym'] == dsym_ and x['session'] == rr_[0]['session']), 'version 11: keys removed with one missing from the middle are not the row rule\'s set: UNEXPLAINED')
        g_ = global_swaps(fna[0], cr_, frozenset(['NA'])); ck(g_[0] == 5 * NM and g_[1] > 0 and g_[2] >= g_[1] and g_[3] == 0 and global_swaps(fna[0], cr_, frozenset())[3] == g_[1], 'version 11: over the whole generated day every membership difference is a swap with the lost ticker (%d pairs, %d names displaced); with no lost ticker named every one is of another kind' % (g_[1], g_[2]))
        late_ = dict(fna[0]); late_['comp'] = fna[0]['comp'].copy(); ml_ = max(m for m in range(dm_ + 1, NM) if (late_['comp'][kk_[0]][:, m] & cr_['comp'][kk_[0]][:, m]).any()); late_['comp'][kk_[0], int(np.flatnonzero(late_['comp'][kk_[0]][:, ml_] & cr_['comp'][kk_[0]][:, ml_])[0]), ml_] = False
        rwl, _g = key_reasons(late_, cr_, a_, r_, w.MINP, w.TOPN, frozenset(['NA'])); gl_ = global_swaps(late_, cr_, frozenset(['NA']))
        ck(ml_ > dm_ and gl_[3] == g_[3] + 1 and all(x['reason'].startswith('E1g') for x in rwl if x['sym'] == dsym_ and x['session'] == rr_[0]['session']), 'version 11: a membership difference that is not a swap, in a minute after the activation minute, is counted by the global line although the activation minute is a clean swap')
        ck(rf(align, dict(fref[0], tick=list(fref[0]['tick'][:-1]) + ['ZZZZ9']), fna[0]['tick']).startswith('KEYDIFF FAILED'), 'version 11: a reference capture with a ticker the new one lacks is refused')
        for m in (0, 1, 2, 400, 401, 730, 731):
            rl = rule_lists(capf, m); ck(all(set(capf['tick'][capf['comp'][k][:, m]]) == rl[k] for k in range(10)), 'E2b: every component list of minute %d at both depths equals the stated rule (value, then ticker ascending; only names that have traded)' % m)
            ck((capf['inl'][:, m] == capf['comp'][0::2, :, m].any(axis=0)).all() and (capf['in2'][:, m] == capf['comp'][1::2, :, m].any(axis=0)).all(), 'the two unions of minute %d are the unions of their components' % m)
        ck(keys_of(capf, w.MINP) == full[1], 'the row rule restated from the capture gives the keys of the table')
        v0 = capf['cv'][:, 0]; cutv = np.sort(v0[v0 > 0])[-TOPN_PIN]; ck((v0 >= cutv).sum() > TOPN_PIN and not capf['inl'][433, 0] and not capf['inl'][433, 1] and capf['inl'][433, 2], 'E2b fixture: a tie stands across the cut of Most Active, and the late name is on no list before its first print')
        c0 = capf['chg'][:, 0]; c0 = c0[~np.isnan(c0)]; ck((c0 >= np.sort(c0)[-TOPN_PIN]).sum() > TOPN_PIN and (c0 <= np.sort(c0)[TOPN_PIN - 1]).sum() > TOPN_PIN, 'E2b fixture: ties stand across the cuts of the gain and loss lists')
        for c_ in (0, 1, 400, 730):
            pc_ = os.path.join(td, 'cut%d' % c_); os.mkdir(pc_); fc = os.path.join(pc_, day_ + '.csv.gz'); cut_file(fb, c_, fc); r = prefix_cmp(full[:2], one_table(w, fc, st_, None)[:2], c_)
            ck(r['bad'] == 0 and r['names_first_printed_after_cut'] >= 1 and r['component_lists_compared'] == 10 and r['row_keys_to_cut'] > 0, 'E2b: prefix test (ten component lists, two unions, row keys), cut after minute %d: %s' % (c_, r))
        x_ = {k: (v.copy() if k in ('inl', 'comp') else v) for k, v in full[0].items()}; x_['comp'][4, 5, 0] ^= True; ck(prefix_cmp(full[:2], (x_, full[1]), 0)['bad'] == 1, 'the prefix test sees one changed component membership even when the unions agree')
        # E2c: Most Active does not wait for a known change: one stock, one bar of 10.00 at 09:35, volume 100, no previous close
        rows.clear(); bar('ONE', 335, 10.0, 100); f1 = os.path.join(td, 'one'); os.mkdir(f1); f1 = os.path.join(f1, day_ + '.csv.gz'); pd.DataFrame(rows).to_csv(f1, index=False, compression='gzip'); o1 = one_table(w, f1, ({}, {}, collections.deque(maxlen=HIST)), None)
        ck(np.isnan(o1[0]['chg']).all() and o1[0]['comp'][4, 0, 335] and o1[0]['comp'][5, 0, 335] and o1[0]['inl'][0, 335] and o1[0]['in2'][0, 335] and o1[1] == {('ONE', 335)} and int(o1[0]['inl'].sum()) == NM - 335, 'E2c: without any known change the stock is on Most Active at both depths from its bar on and has one row key')
        w3 = load_old_rule('v3'); o3 = one_table(w3, f1, ({}, {}, collections.deque(maxlen=HIST)), None); rw, _g = key_reasons(o1[0], o3[0], o1[1] - o3[1], o3[1] - o1[1], w.MINP, w.TOPN)
        ck(o3[1] == set() and not o3[0]['inl'].any() and len(rw) == 1 and rw[0]['change'] == 'added' and rw[0]['reason'].startswith('E2c'), 'E2c keydiff: under the rule of builder e6f36ed3 the stock has no key; the added key is traced to E2c')
        # keydiff with controlled masks: an old rule that differs from the new one by ONE tied pair on the gain list at minute 0 (no platform sort involved); additions and removals both appear and both are explained
        g0 = capf['comp'][0][:, 0]; cv_ = capf['chg'][:, 0]; vv_ = capf['cv'][:, 0]; kn_ = ~np.isnan(cv_)
        def tied(val, known, i):
            better = int((known & (val > val[i])).sum()); return better < TOPN_PIN < better + int((known & (val == val[i])).sum())
        vals = {0: (cv_, kn_), 2: (-cv_, kn_), 4: (vv_, vv_ > 0)}; pair = None
        for i in np.flatnonzero(g0):
            mine = [k for k in (0, 2, 4) if capf['comp'][k, i, 0]]
            if not all(tied(vals[k][0], vals[k][1], i) for k in mine): continue   # the name can leave every list it is on at minute 0 only through ties
            for j in np.flatnonzero(~capf['inl'][:, 0] & (cv_ == cv_[i]) & capf['has'][:, 0]): pair = (int(i), int(j), mine); break
            if pair: break
        ck(pair is not None, 'keydiff fixture: a tied pair across the cut of the gain list exists (one name listed, one not)')
        i_, j_, mine = pair; old_ = dict(capf); old_['inl'] = capf['inl'].copy(); old_['comp'] = capf['comp'].copy(); old_['inl'][i_, 0] = False; old_['inl'][j_, 0] = True; old_['comp'][0, j_, 0] = True
        for k in mine: old_['comp'][k, i_, 0] = False
        ko_ = keys_of(old_, w.MINP); a_ = full[1] - ko_; r_ = ko_ - full[1]; rw, _g = key_reasons(capf, old_, a_, r_, w.MINP, w.TOPN)
        ck((capf['tick'][i_], 0) in a_ and (capf['tick'][j_], 0) in r_ and len(rw) == len(a_) + len(r_) and all(x['reason'].startswith('tie at the cut of the gain list') for x in rw) and {x['change'] for x in rw} == {'added', 'removed'}, 'keydiff: keys added and removed by a controlled tie swap, each traced to the lists whose membership changed')
        rw, _g = key_reasons(capf, old_, a_ | {('N0000', 3)}, r_, w.MINP, w.TOPN); ck(sum(x['reason'] == 'UNEXPLAINED' for x in rw) >= 1, 'keydiff: a key that no list change explains is UNEXPLAINED')
        old2 = dict(old_); old2['comp'] = capf['comp'].copy(); rw, _g = key_reasons(capf, old2, a_, r_, w.MINP, w.TOPN); ck(all(x['reason'] == 'UNEXPLAINED' for x in rw), 'keydiff: a union that changed while no component did names no list: UNEXPLAINED')
        # the platform's own old sort (the rule of builder 5561d4e3): whatever keys it adds or removes on the synthetic day, in either direction, every one must be explained
        wo = load_old_rule('v2'); o2 = one_table(wo, fb, st_, None); a_ = full[1] - o2[1]; r_ = o2[1] - full[1]; rw, _g = key_reasons(capf, o2[0], a_, r_, w.MINP, w.TOPN)
        ck(len(rw) == len(a_) + len(r_) and all(x['reason'].startswith('tie at the cut') for x in rw), 'keydiff against the platform\'s old sort: every difference is explained by a tie at a cut (%d added, %d removed on this platform)' % (len(a_), len(r_)))
        print('note: against the old unstable sort this platform gives %d added and %d removed keys on the synthetic day (the counts depend on the platform; the check does not)' % (len(a_), len(r_)))
    ck(codes('1,82') == {1, 82} and codes('') == frozenset() and codes(None) == frozenset(), 'codes')
    try: codes('1,x'); bad = False
    except SystemExit: bad = True
    ck(bad, 'unparsable code refuses')
    import platform, pandas, duckdb
    print('platform: %s %s, Python %s, numpy %s, pandas %s, numba %s, duckdb %s; builder md5 %s' % (platform.system(), platform.machine(), platform.python_version(), np.__version__, pandas.__version__, nb.__version__, duckdb.__version__, md5(os.path.abspath(__file__))))
    print('fixtures passed: %d checks' % n)


# ---------------------------------------------------------------- one day
def load_day(day, syms, tkf, pxf, C):
    import duckdb, pandas as pd
    con = duckdb.connect(); con.execute('SET threads=4'); n = len(syms); con.register('ss_', pd.DataFrame({'sym': syms, 'i': np.arange(n)}))
    w = "FROM read_parquet('%s') t JOIN ss_ USING (sym) WHERE m >= %d AND m < %d"
    t = con.execute(("SELECT i, CAST(m AS INTEGER) AS m, qn, tn, qbid, qask, qbsl, qasl, tlo, thi " + w) % (tkf, OFF, OFF + NM)).df()
    pc = PXCOLS
    p = con.execute(("SELECT i, CAST(m AS INTEGER) AS m, %s " % ', '.join(pc) + w) % (pxf, OFF, OFF + NM)).df()
    # erratum P: per stock its last tk row with a quote update before 04:00, with the terminal mark of that minute
    i0 = con.execute("SELECT i, t.m, t.qbid, t.qask, t.qbsl, t.qasl, p.pstE, p.pqiE, p.pqcE, p.pqtE, (p.sym IS NOT NULL) AS pxrow FROM (SELECT sym, m, qbid, qask, qbsl, qasl, row_number() OVER (PARTITION BY sym ORDER BY m DESC) rn FROM read_parquet('%s') WHERE m < %d AND qn > 0) t JOIN ss_ USING (sym) "
                     "LEFT JOIN read_parquet('%s') p ON p.sym = t.sym AND p.m = t.m WHERE rn = 1" % (tkf, OFF, pxf)).df()
    def mat(d, col, fill=np.nan, dt=np.float64):
        a = np.full((n, NM), fill, dt); a[d.i.values, d.m.values - OFF] = d[col].values if dt != np.float64 else d[col].astype('float64').values; return a
    def flag(d, col, bad):
        u = {s: bool(codes(s) & bad) for s in d[col].dropna().unique()}; a = np.zeros((n, NM), bool); a[d.i.values, d.m.values - OFF] = d[col].map(u).fillna(False).astype(bool).values; return a
    def tsm(d, col):
        a = np.zeros((n, NM), np.int64); a[d.i.values, d.m.values - OFF] = d[col].fillna(0).astype('int64').values; return a
    def strm(d, col):
        a = np.full((n, NM), '', object); a[d.i.values, d.m.values - OFF] = d[col].fillna('').astype(str).values; return a
    M = dict(close=C, qn=mat(t, 'qn', 0.0), tn=mat(t, 'tn', 0.0))
    for k in ('qbid', 'qask', 'qbsl', 'qasl', 'tlo', 'thi'): M[k] = mat(t, k)
    hp = np.zeros((n, NM), bool); hp[p.i.values, p.m.values - OFF] = True; M['haspx'] = hp
    for k in ('xlos', 'xhis', 'pblof', 'pblofs', 'pblo5', 'pblo5s', 'pstE'): M[k] = mat(p, k)
    M['bbE'] = flag(p, 'pqiE', BIDBAD); M['abE'] = flag(p, 'pqiE', ASKBAD); M['c20E'] = flag(p, 'pqcE', {20}); M['pqtE'] = tsm(p, 'pqtE')
    for k in ('qb', 'qa', 'qbz', 'qaz', 'pst'): M[k] = {S: mat(p, '%s%d' % (k, S)) for S in MARKS}
    M['bb'] = {S: flag(p, 'pqi%d' % S, BIDBAD) for S in MARKS}; M['ab'] = {S: flag(p, 'pqi%d' % S, ASKBAD) for S in MARKS}; M['c20'] = {S: flag(p, 'pqc%d' % S, {20}) for S in MARKS}; M['pqt'] = {S: tsm(p, 'pqt%d' % S) for S in MARKS}
    I0 = dict(has=np.zeros(n, bool), px=np.zeros(n, bool), pstE=np.full(n, np.nan), bb=np.zeros(n, bool), ab=np.zeros(n, bool), c20=np.zeros(n, bool), pqt=np.zeros(n, np.int64), qi=np.full(n, '', object), qc=np.full(n, '', object), m=np.full(n, -1))
    for k in ('bid', 'ask', 'bsz', 'asz'): I0[k] = np.zeros(n)
    if len(i0):
        ii = i0.i.values; I0['has'][ii] = True; I0['px'][ii] = i0.pxrow.astype(bool).values; I0['m'][ii] = i0.m.values; I0['pstE'][ii] = i0.pstE.astype('float64').values; I0['pqt'][ii] = i0.pqtE.fillna(0).astype('int64').values
        for k, c in (('bid', 'qbid'), ('ask', 'qask'), ('bsz', 'qbsl'), ('asz', 'qasl')): I0[k][ii] = i0[c].astype('float64').fillna(0).values
        I0['bb'][ii] = [bool(codes(x) & BIDBAD) for x in i0.pqiE]; I0['ab'][ii] = [bool(codes(x) & ASKBAD) for x in i0.pqiE]; I0['c20'][ii] = [20 in codes(x) for x in i0.pqcE]; I0['qi'][ii] = i0.pqiE.fillna('').astype(str).values; I0['qc'][ii] = i0.pqcE.fillna('').astype(str).values
    M['init'] = I0
    STR = {k: strm(p, k) for k in ('pqi5', 'pqc5', 'pqiE', 'pqcE')}
    return M, STR, dict(tk_rows=len(t), px_rows=len(p), px_rows_without_tk=int((hp & ~((M['qn'] > 0) | (M['tn'] > 0))).sum()), stocks_with_quote_before_0400=int(I0['has'].sum()))


def early_days(earlyf):
    if not os.path.isfile(earlyf): raise SystemExit('REFUSED: early_close_file_missing: no early-close days file %s' % earlyf)
    b = raw(earlyf)
    if md5b(b) != EARLY_MD5: raise SystemExit('REFUSED: early_close_file_md5: the early-close days file differs from the pinned md5')
    return {l.strip()[:10] for l in b.decode('utf-8', 'replace').split('\n') if l.strip() and not l.startswith('#')}


def frame(day, t, D, hp, I0, STR, ri, rq, rs, vu, rkmin):
    # every stored column of the day except chg_p, from the derived arrays D
    import pandas as pd
    o = run(D, ri, rq, rs, vu); L = pd.DataFrame({'sym': t.sym.values, 'm': rq.astype(np.int16), 'sess': rs.astype(np.int8)})
    for k, j in O.items(): L[k] = o[:, j]
    L['b_cls_alt'] = run(D, ri, rq, rs, vu, True)[:, O['b_cls']]   # class when order-unknown lows of the entry minute are not taken as a stop
    L['hs'] = D['HS'][ri, rq]; L['tq_ask'] = D['TQA'][ri, rq]; L['tq_bid'] = D['TQB'][ri, rq]; L['rk30e'], L['rk_n'] = rank30(rq, L.h_val.values)
    L['pu30e'] = np.where(L.cls_ok == 1, (L.b_cls == 1).astype(float), np.nan); L['pl30e'] = np.where(L.cls_ok == 1, (L.b_cls == 2).astype(float), np.nan)
    # erratum H: the quote that refused an entry (the 5 s mark of minute m+1): where it was read, and both code strings
    e = np.minimum(rq + 1, NM - 1); px = hp[ri, e]; lp = D['LP'][ri, e]; noe = (L.st.values == 1); lpc = np.maximum(lp, 0)
    L['e_src'] = np.where(noe, np.where(px, 0, np.where(lp >= 0, 1, np.where(I0['has'][ri], 2, 3))), -1).astype(np.int8)   # 0 the px mark of that minute, 1 carried from the terminal mark of an earlier minute of the grid, 2 carried from before 04:00, 3 no quote yet today
    L['e_qmin'] = np.where(noe, np.where(px, e, np.where(lp >= 0, lp, np.where(I0['has'][ri], I0['m'][ri] - OFF, -9999))), -9999).astype(np.int16)   # minute index of the quote's row (negative = before 04:00)
    L['e_qi'] = np.where(noe, np.where(px, STR['pqi5'][ri, e], np.where(lp >= 0, STR['pqiE'][ri, lpc], np.where(I0['has'][ri], I0['qi'][ri], ''))), ''); L['e_qc'] = np.where(noe, np.where(px, STR['pqc5'][ri, e], np.where(lp >= 0, STR['pqcE'][ri, lpc], np.where(I0['has'][ri], I0['qc'][ri], ''))), '')
    L['pre_q'] = D['PRE'][ri, rq].astype(np.int8)   # the quote known at T is the one from before 04:00
    # exact time and age of every value that came from a mark (h, t, b): SIP time of the quote and seconds between the mark where it was read and that time
    z0 = int(datetime.datetime.fromisoformat(day).replace(tzinfo=zoneinfo.ZoneInfo('America/New_York')).timestamp()) * 10 ** 9; secs = np.array([0, 2, 5, 15, 60])
    for p_ in 'htb':
        mk = (L[p_ + '_mark_ok'].values == 1); mm = np.where(mk, L[p_ + '_min'].values, 0).astype(np.int64); sx = np.where(mk, L[p_ + '_sec'].values, 0).astype(np.int64)
        ts = np.where(sx == 4, D['TTS'][ri, mm], D['TS'][np.minimum(sx, 3), ri, mm]); L[p_ + '_mark_ts'] = np.where(mk, ts, 0); L[p_ + '_mark_st'] = np.where(mk, np.where(sx == 4, D['TST'][ri, mm], D['SS'][np.minimum(sx, 3), ri, mm]), -1)
        L[p_ + '_mark_age'] = np.where(mk & (ts > 0), (z0 + ((mm + OFF) * 60 + secs[sx]) * 10 ** 9 - ts) / 1e9, np.nan)
    # old labels: empty where the old table has no row (erratum B); the old rank is taken among the old rows only, as the old table did
    old = t.pb.notna().values; L['old_row'] = old.astype(np.int8)
    for k in ('pb', 'h30', 'd30', 't10', 'b30', 'vu', 'c'): L['old_' + k] = np.where(old, t[k].values, np.nan) if k not in ('vu', 'c') else t[k].values
    oh = pd.Series(np.where(old, t.h30.values, np.nan)); g_ = oh.groupby(rq); L['old_rk30'] = g_.rank(pct=True).where(g_.transform('count') >= rkmin).astype(np.float32).values
    return L


def col_ne(a, b):
    # rows where two stored columns differ: integers (time stamps in nanoseconds) in their own type, strings as strings, the rest as numbers with empty equal to empty
    import pandas as pd
    a = np.asarray(a); b = np.asarray(b)
    if a.dtype == object or b.dtype == object: return ~((a == b) | (pd.isna(a) & pd.isna(b)))
    if np.issubdtype(a.dtype, np.integer) and np.issubdtype(b.dtype, np.integer): return a != b
    a = a.astype(np.float64); b = b.astype(np.float64); return ~((a == b) | (np.isnan(a) & np.isnan(b)))


def pbit(c):
    # the group of a stored column in chg_p: 1 entry, 2 tq, 4 band, 8 h (value, fill, mark with its time, state and age), 16 d, 32 t, 64 b (with unit, classes, flags), 128 the refusing quote of a failed entry, 256 rank and its count, 512 any other column
    if c in ('st', 'est', 'ereason', 'A', 'ebid'): return 1
    if c in ('tq_ask', 'tq_bid'): return 2
    if c == 'hs': return 4
    for pre, bit in (('h_', 8), ('d_', 16), ('t_', 32), ('b_', 64), ('e_', 128)):
        if c.startswith(pre): return bit
    if c in ('u', 'cls_ok', 'pu30e', 'pl30e'): return 64
    if c in ('rk30e', 'rk_n'): return 256
    return 512


def build(src, types, day, tkd, pxd, mand, earlyf, out):
    import pandas as pd
    env_refuse(); day_ok(day); early = early_days(earlyf); need, sp = real_inputs(src, types, day)   # errata A / E1a / E1c: every refusal of the calendar, the bar folder, the types and the splits file is raised here, before any data file is opened
    if day in early: raise SystemExit('REFUSED: early_close: %s is an early-close day; the table has no early-close sessions' % day)
    tkf, pxf, mff, mf, got = tick_inputs(day, tkd, pxd, mand); read_types(types); brec = bars_readable(src, need); out_ok(out)
    attempt(out, lambda cl: _build(src, types, day, earlyf, out, need, brec, cl, tkf, pxf, mff, mf, got, sp))


def _build(src, types, day, earlyf, out, need, brec, cl, tkf, pxf, mff, mf, got, sp):
    import pandas as pd
    krows = tick_keys(day, tkf, pxf); wm2_l1, wmd = load_wm2()
    t, opened = table(src, types, need, wm2_l1.day2, sp); cap = dict(wm2_l1._CAP); tk_ = cap['tick']; rows_ok(t, day); tickers_ok(tk_)
    spf, spd = sp.path, sp.digest   # erratum E1j, 2: of the object the table was built with
    print('build record: builder md5 %s; table code pinned %s; wm2_l1.py (wm2.py with the declared replacements: row rule; list code in %d places: %s; capture for the prefix test) md5 %s' % (md5(os.path.abspath(__file__)), ', '.join('%s %s' % (k, v[:8]) for k, v in sorted(PIN.items())), len(LISTPATCH), ', '.join(x[0] for x in LISTPATCH), wmd))
    print('build record: effective settings of the table code (none of %s is set): %s' % (', '.join(ENVREF), settings(wm2_l1)))
    print('build record: trading-day calendar %s md5 %s; early-close days file md5 %s (both pinned); first day of the archive %s' % (CALF, CAL_MD5, md5b(raw(earlyf)), ARCH_FIRST))   # erratum E1j, 3: the digest of the buffer early_days verified (raw keeps it), not a new hash of the path
    print('build record: daily bar files opened %d, equal to the list checked before the build (target %s; sealed files opened: %d): %s' % (len(opened), day, sum(sealed(os.path.basename(f)[:10]) for f in opened), ' '.join(os.path.basename(f)[:10] for f in opened)))
    files_same(brec, opened)
    for x in brec: print('build record: bar file %s.csv.gz bytes %d md5 %s' % x)
    print('build record: bar read: wide.load replaced by the builder\'s bar_load in the table code (erratum E1d: every row on the file\'s New York date, (ticker, minute) unique, in the read that feeds the table)')
    print('build record: tick file rows %d, price file rows %d: keys valid and unique over all rows; quote times on the day, not after their mark, terminal before the next minute (erratum E1d)' % (krows['tick'], krows['price']))
    print('build record: the target bar file is read once: the minute closes of the labels are the close matrix the table code captured from that read (erratum E1e, 2c); bytes and md5 of every bar file were taken again after it')
    print('build record: one protocol per output folder: every writer of %s is this builder; a file put there by anything else is outside the guarantee (erratum E1e, 4a)' % os.path.dirname(os.path.abspath(out)))
    print('build record: types file %s md5 %s; splits file %s md5 %s (both pinned)' % (os.path.basename(types), md5b(raw(types)), os.path.basename(spf), spd))
    print('build record: target tick file md5 %s, price file md5 %s (both equal to the manifest), manifest %s md5 %s, manifest day %s, frozen converter %s' % (got['tk'], got['px'], os.path.basename(mff), got['manifest'], mf['date'], FROZEN))
    t = t.reset_index(drop=True); syms, ri = np.unique(t.sym.values, return_inverse=True); rq = t.m.values.astype(np.int64); rs = t.sess.values.astype(np.int64); vu = t.vu.values.astype(np.float64)
    C = closes(cap, syms); tick_symbols(tkf, syms)
    M, STR, info = load_day(day, syms, tkf, pxf, C); gaps = px_gaps(M); sg = seed_gaps(M['init'])
    ticks_same(got, tkf, pxf, mf)   # erratum E1i, 3: load_day was the last read of the two files
    print('build record: tick file and price file read again after their last use: bytes and md5 equal to the manifest (erratum E1i, 3)')
    gaps_refuse(day, gaps, sg)
    # errata P / E9a are measured, not assumed: the same day once without the quotes from before 04:00, every stored column compared
    I0 = M.pop('init'); Z0 = dict(I0); Z0['has'] = np.zeros(len(syms), bool); M['init'] = Z0; D0 = derive(M); hp = M['haspx']; L0 = frame(day, t, D0, hp, Z0, STR, ri, rq, rs, vu, wm2_l1.RKMIN); del D0
    M['init'] = I0; D = derive(M); del M; L = frame(day, t, D, hp, I0, STR, ri, rq, rs, vu, wm2_l1.RKMIN)
    cp = np.zeros(len(L), np.int64); pc_ = {}
    for c in L.columns:
        if c == 'pre_q': continue
        x = col_ne(L[c].values, L0[c].values); pc_[c] = int(x.sum()); cp |= np.where(x, pbit(c), 0)
    L['chg_p'] = cp.astype(np.int16)   # which stored groups differ from a run without the quotes from before 04:00 (pbit)
    L.insert(0, 'date', day)
    def rep_():
        print('day %s: table rows %d (with an old label %d, added by erratum B %d), stocks %d; tk rows %d, px rows %d; minutes with a tk quote update or trade and no px row 0 (else refused); px rows without a tk row %d; table stocks with a quote before 04:00: %d, of them without a px row 0 (else refused)'
              % (day, len(L), int((L.old_row == 1).sum()), int((L.old_row == 0).sum()), len(syms), info['tk_rows'], info['px_rows'], info['px_rows_without_tk'], info['stocks_with_quote_before_0400']))
        print('errata P / E9a, rows that differ from a run whose carry starts at 04:00 (r1pilot reads from 04:00: a named limit of the pilot); EVERY stored column compared (%d columns; pre_q is the indicator itself): columns with a difference: %s; by group: entry %d (of them no_entry -> filled %d, filled -> no_entry %d), tq %d, band %d, h %d, d %d, t %d, b %d, refusing quote of a failed entry %d, rank or its count %d, other %d; rows whose quote known at T is the one from before 04:00: %d'
              % (len(pc_), {k: v for k, v in pc_.items() if v} or 'none', int((cp & 1 > 0).sum()), int(((L0.st == 1) & (L.st == 0)).sum()), int(((L0.st == 0) & (L.st == 1)).sum()), int((cp & 2 > 0).sum()), int((cp & 4 > 0).sum()), int((cp & 8 > 0).sum()), int((cp & 16 > 0).sum()), int((cp & 32 > 0).sum()), int((cp & 64 > 0).sum()), int((cp & 128 > 0).sum()), int((cp & 256 > 0).sum()), int((cp & 512 > 0).sum()), int(L.pre_q.sum())))
        report(L)
    cr_ = code_same('at the end of the run')   # erratum E1i, 4: before anything is published
    print('build record: table code read again after loading and at the end of the run, before the table is published: bytes and md5 unchanged (erratum E1i, 4): %s' % ', '.join('%s %d %s' % x for x in cr_))
    tmp_, nb_, h_ = publish(L, out, day, cl, rep_)
    print('build record: published %s (bytes %d, md5 %s) from the temporary name %s after the read-back and the report; claim %s' % (out, nb_, h_, os.path.basename(tmp_), cl[1]))


def select_report(minute, pred):
    # erratum N, for every result printout that uses the scorer's best 1%: selected rows per minute, minutes where tied rows stand across the cut (the scorer takes such a group whole or drops it whole; both counted), minutes with nothing selected
    import pandas as pd
    d = pd.DataFrame({'m': np.asarray(minute), 'p': np.asarray(pred, float)}).dropna(); sel = []; tie = 0; tied_out = 0; empty = 0
    for _, g in d.groupby('m'):
        k = int(select1(g.p.values).sum()); sel.append(k); n = len(g); cap = max(1.0, 0.01 * n); r = g.p.rank(ascending=False, method='min').values; rx = g.p.rank(ascending=False, method='max').values
        x = (r <= cap) & (rx > cap)
        if x.any(): tie += 1; tied_out += int(not select1(g.p.values)[x].any())
        if k == 0: empty += 1
    return dict(minutes=len(sel), selected=int(np.sum(sel)), per_minute=dict(sorted(collections.Counter(sel).items())), minutes_with_a_tie_across_the_cut=tie, of_them_tied_rows_dropped=tied_out, minutes_with_nothing_selected=empty)


def coverage(src, earlyf, first, last):
    # errata O / E1a: every calendar day of a range, buildable or refused by reason. src = the minute_aggs folder, or a text file with one bar-file name or date per line (names only; nothing is opened).
    bounds_ok(first, last); cal = calendar(); have, _ = bar_names(src); early = early_days(earlyf); R = collections.defaultdict(list)
    for d in cal:
        if not (first <= d <= last): continue
        try: needed(cal, have, d); R['early_close' if d in early else 'buildable'].append(d)
        except SystemExit as x:
            k = str(x).split(': ')[1]; R[k].append(d if k != 'bar_file_missing' else '%s(needs %s)' % (d, str(x).split()[-1]))
    for d in sorted(have - set(cal)):
        if first <= d <= last: R['bar_file_not_a_calendar_day'].append(d)
    print('coverage %s .. %s: calendar days in range %d' % (first, last, sum(len(v) for k, v in R.items() if k != 'bar_file_not_a_calendar_day')))
    for k, v in R.items(): print('  %s: %d%s' % (k, len(v), '' if k == 'buildable' else ': ' + ' '.join(v) if len(v) <= 60 else ': %s .. %s' % (v[0], v[-1])))
    return R


def report(L):
    import pandas as pd
    from scipy.stats import spearmanr
    pd.set_option('display.width', 250); pd.set_option('display.max_columns', 60); pd.set_option('display.max_rows', 200)
    L = L.copy(); L['session'] = L.sess.map({0: 'pre', 1: 'regular', 2: 'post'}); L['band'] = pd.cut(L.hs, [-1, 0.001, 0.0025, 0.005, 0.01, 9], labels=['<0.1%', '0.1-0.25%', '0.25-0.5%', '0.5-1%', '1%+'], right=False).astype(object).fillna('none')
    fg = L.b_flags.fillna(0).astype(int); ok = L.st == 0; bu = ok & (L.b_unit == 0)
    def tab(title, cols):
        print('\n== ' + title); r = []
        for (s, b), g in list(L.groupby(['session', 'band'], sort=False)) + [((s, 'ALL'), g) for s, g in L.groupby('session', sort=False)] + [(('ALL', 'ALL'), L)]:
            r.append(dict(session=s, band=b, rows=len(g), **{k: f(g) for k, f in cols}))
        x = pd.DataFrame(r); x['o1'] = x.session.map({'pre': 0, 'regular': 1, 'post': 2, 'ALL': 3}); x['o2'] = x.band.map({'<0.1%': 0, '0.1-0.25%': 1, '0.25-0.5%': 2, '0.5-1%': 3, '1%+': 4, 'none': 5, 'ALL': 6})
        print(x.sort_values(['o1', 'o2']).drop(columns=['o1', 'o2']).to_string(index=False, float_format=lambda v: '%.4f' % v))
    sh = lambda m: (lambda g: float(m.loc[g.index].mean()) if len(g) else np.nan); shc = lambda m, c: (lambda g: float(m.loc[g.index][c.loc[g.index]].mean()) if c.loc[g.index].any() else np.nan)
    tab('entry and coverage (shares of rows; ret/mark shares among filled entries)', [('no_entry', sh(L.st == 1))] + [('ne:' + EREASON[k][:14], sh((L.st == 1) & (L.ereason == k))) for k in (1, 2, 3, 4, 5)] +
        [(p_ + '_ret_ok', shc(L[p_ + '_ret_ok'] == 1, ok)) for p_ in 'htb'] + [(p_ + '_mark_ok', shc(L[p_ + '_mark_ok'] == 1, ok)) for p_ in 'htb'] + [('d_has', shc(L.d_val.notna(), ok)), ('cls_ok', shc(L.cls_ok == 1, ok)), ('n_no_entry', lambda g: int((g.st == 1).sum())), ('n_marks_h_t_b', lambda g: '%d/%d/%d' % tuple(int((g[p_ + '_mark_ok'] == 1).sum()) for p_ in 'htb')), ('n_entered_no_bid', lambda g: int(((g.st == 0) & (g.ebid <= 0)).sum()))])
    tab('no_unit by reason (share of filled entries)', [('no_unit:' + UNIT[k], shc(L.b_unit == k, ok)) for k in (1, 2, 3, 4)])
    tab('b30e flags (share of rows with a b label); bid_under_L_at_entry and spread_over_u are one condition, printed once', [(nm.replace('bid_under_L_at_entry', 'bid_under_L=spread_over_u'), shc((fg & bit) > 0, bu)) for nm, bit in FLAGS if bit != F_SPR])
    unk = (fg & F_UNK) > 0; cl = L.b_cls
    tab('b30e classes (share of rows with a class): primary (order_unknown = lower); _exU = those rows excluded; _alt = those rows taken the other way (not a stop)',
        [('upper', shc(cl == 1, bu)), ('lower', shc(cl == 2, bu)), ('time', shc(cl == 0, bu)), ('upper_exU', shc(cl == 1, bu & ~unk)), ('lower_exU', shc(cl == 2, bu & ~unk)), ('time_exU', shc(cl == 0, bu & ~unk)), ('upper_alt', shc(L.b_cls_alt == 1, bu)), ('lower_alt', shc(L.b_cls_alt == 2, bu)), ('time_alt', shc(L.b_cls_alt == 0, bu)), ('order_unknown', shc(unk, bu)),
         ('stopped_in_entry_min', shc((fg & F_ENTMIN) > 0, bu))])
    bv = L.b_val; hv = bu & bv.notna(); mean = lambda v, c: (lambda g: float(v.loc[g.index][c.loc[g.index]].mean()) if c.loc[g.index].any() else np.nan)
    bf = hv & (L.b_ret_ok == 1); sr = bu & L.b_stress.notna()
    tab('b30e value: observed fills only beside marked values included (rows with a value), with coverage; mean_opt = the optimistic variant', [('mean_fill', mean(bv, bf)), ('share<-3_fill', shc(bv < -3, bf)), ('share>+4_fill', shc(bv > 4, bf)), ('mean_marked_in', mean(bv, hv)), ('share<-3', shc(bv < -3, hv)), ('mean<-3', mean(bv, hv & (bv < -3))), ('share>+4', shc(bv > 4, hv)), ('mean>+4', mean(bv, hv & (bv > 4))), ('coverage_fill', shc(bf, bu)), ('n_fill', lambda g: int(bf.loc[g.index].sum())), ('n_marked', lambda g: int((hv & ~bf).loc[g.index].sum())), ('mean_opt', mean(L.b_opt, hv))])
    tab('b30e stress against primary ON THE SAME ROWS (rows with a stress value = observed fills)', [('n', lambda g: int(sr.loc[g.index].sum())), ('mean_primary', mean(bv, sr)), ('mean_stress', mean(L.b_stress, sr)), ('rows_stress_under_primary', lambda g: int((sr & (L.b_stress < bv)).loc[g.index].sum()))])
    for p_ in 'ht':
        v = L[p_ + '_val']; r_ = ok & (L[p_ + '_ret_ok'] == 1); a_ = ok & v.notna()
        tab('%s: mean and winners, observed fills only and with marked values' % {'h': 'h30e', 't': 't10e'}[p_], [('mean_fill', mean(v, r_)), ('win_fill', shc(v > 0, r_)), ('mean_marked_in', mean(v, a_)), ('win_marked_in', shc(v > 0, a_)), ('coverage_fill', shc(r_, ok))])
    hsum = lambda g: float(g.h_val[(g.st == 0)].sum())
    def over(x): return lambda g: (float(((g.st == 0) & (g.tq_ask > 0) & (g.A > g.tq_ask * (1 + x))).sum()) / int(((g.st == 0) & (g.tq_ask > 0)).sum())) if ((g.st == 0) & (g.tq_ask > 0)).any() else np.nan
    def part(x): return lambda g: (float(g.h_val[(g.st == 0) & (g.tq_ask > 0) & (g.A > g.tq_ask * (1 + x))].sum()) / hsum(g)) if hsum(g) != 0 else np.nan
    tab('entry price A against the ask known at T, a SNAPSHOT COMPARISON, NOT A LIMIT-ORDER REPLAY: share, among entered rows WITH an ask at T (n_with_ask_at_T), of rows with A over that ask by more than x; their part of the h30e sum of all entered rows; n_no_ask_at_T = entered rows without an ask at T, in neither',
        [c_ for x in (0.01, 0.02, 0.05, 0.10) for c_ in (('A>ask+%d%%' % round(x * 100), over(x)), ('part_h30e_%d%%' % round(x * 100), part(x)))] + [('n_with_ask_at_T', lambda g: int(((g.st == 0) & (g.tq_ask > 0)).sum())), ('n_no_ask_at_T', lambda g: int(((g.st == 0) & ~(g.tq_ask > 0)).sum()))])
    tab('d30e (rows with a value)', [('mean', mean(L.d_val, ok & L.d_val.notna())), ('median_n_minutes', lambda g: float(g.d_n[g.st == 0].median()) if (g.st == 0).any() else np.nan)])
    # agreement with the minute-price forms on the same rows
    oc = np.where(L.old_b30 >= UP, 1, np.where(L.old_b30 <= -DN, 2, 0)); both = bu & L.old_b30.notna()
    def spear(g, a, b):
        tot = 0.0; w = 0
        for _, x in g.groupby('m'):
            x = x[x[a].notna() & x[b].notna()]
            if len(x) >= 20 and x[a].nunique() > 1 and x[b].nunique() > 1: tot += spearmanr(x[a], x[b])[0] * len(x); w += len(x)
        return tot / w if w else np.nan
    tab('agreement with the minute-price forms: b30e class against b30 class; Spearman inside the minute (minutes with 20+ valid rows, weighted by rows)',
        [('class_agree', shc(pd.Series(oc, index=L.index) == cl, both)), ('rho_b30e_b30', lambda g: spear(g[g.b_unit == 0], 'b_val', 'old_b30')), ('rho_rk30e_rk30', lambda g: spear(g, 'rk30e', 'old_rk30')), ('rk_rows', lambda g: int(g.rk30e.notna().sum()))])
    print('\n== b30e class against b30 class, all sessions (rows)'); print(pd.crosstab(pd.Series(oc, index=L.index)[both].map({0: 'old time', 1: 'old upper', 2: 'old lower'}), cl[both].map({0: 'time', 1: 'upper', 2: 'lower'})).to_string())
    print('\n== marked values: age in seconds of the inventory mark (h30e)'); print(L.h_mark_age[L.h_mark_ok == 1].describe().to_string())
    print('\n== inventory marks by the state of the quote they were read from (rows; state 5 = crossed)')
    for p_, nm in (('h', 'h30e'), ('t', 't10e'), ('b', 'b30e')): print(nm, L[p_ + '_mark_st'][L[p_ + '_mark_ok'] == 1].value_counts().sort_index().to_dict())
    print('\n== b30e_opt, the OPTIMISTIC VARIANT (a trade high at or over U counts as +2 units): rows that took +2: %d = a later window minute %d + the entry minute alone, high first printed at or after second 5 %d + the entry minute alone, not proven after the entry mark %d; rows where it is UNDER the realised value (an upper exit sold above U) %d; rows with a b value %d. max(b30e, b30e_opt) is the hindsight maximum of two constructed outcomes, derived, never stored.'
          % (int((L.b_opt_src > 0).sum()), int((L.b_opt_src == 2).sum()), int((L.b_opt_src == 1).sum()), int((L.b_opt_src == 3).sum()), int((hv & (L.b_opt < bv)).sum()), int(hv.sum())))
    st_ = bu & (cl == 2) & (L.b_ret_ok == 1); c_ = [int((st_ & (L.b_stress_src == k)).sum()) for k in (0, 1, 2)]
    print('== stress coverage (erratum D): filled stops %d: no stress low in the trigger minute %d, stress low with its order after the trigger known %d (%.1f%% = the coverage), stress low of unknown order %d; outside the covered rows the stress value equals the primary' % (int(st_.sum()), c_[0], c_[1], 100.0 * c_[1] / max(1, st_.sum()), c_[2]))
    fl_ = pd.Series(FL[L.sess.values.astype(int)], index=L.index); at = lambda p_: (L[p_ + '_ret_ok'] == 1) & (L[p_ + '_min'] == fl_) & (L[p_ + '_sec'] == 3)
    print('== erratum J, sales decided before the flatten minute - 1, still pending at the flatten minute and filled at its 15 s mark: h30e %d, t10e %d, b30e (upper or time exit) %d' % (int((ok & at('h') & (L.m + HOR < fl_ - 1)).sum()), int((ok & at('t') & (L.t_dec < fl_ - 1)).sum()), int((bu & at('b') & (cl != 2) & (L.b_dec < fl_ - 1)).sum())))
    for nm, p_, dec, base in (('h30e', 'h', np.minimum(L.m + HOR, fl_ - 1), ok), ('t10e', 't', L.t_dec, ok), ('b30e (upper or time exit)', 'b', L.b_dec, bu & (cl != 2))):
        pend = base & (dec < fl_ - 1) & ~((L[p_ + '_ret_ok'] == 1) & (L[p_ + '_min'] < fl_)); f_ = pend & (L[p_ + '_ret_ok'] == 1); a_ = lambda s_: int((f_ & (L[p_ + '_min'] == fl_) & (L[p_ + '_sec'] == s_)).sum())
        print('== errata E6 / E6a, %s: sales decided before the minute before the flatten and still pending on entering the flatten minute %d: filled at its second 5 %d, at its second 15 %d, at second 5 of a later minute %d, not filled (marked or no value) %d' % (nm, int(pend.sum()), a_(2), a_(3), int((f_ & (L[p_ + '_min'] > fl_)).sum()), int((pend & ~f_).sum())))
    sp_ = bu & (cl == 2) & (L.b_dec == fl_ - 1); fs_ = sp_ & (L.b_ret_ok == 1); a_ = lambda s_: int((fs_ & (L.b_min == fl_) & (L.b_sec == s_)).sum())
    print('== erratum E6a, stops triggered in the minute before the flatten %d (section 4: the four marks of the flatten minute, then second 5 of each later minute): filled in the flatten minute at second 0 %d, second 2 %d, second 5 %d, second 15 %d; at second 5 of a later minute %d; not filled (marked or no value) %d' % (int(sp_.sum()), a_(0), a_(1), a_(2), a_(3), int((fs_ & (L.b_min > fl_)).sum()), int((sp_ & ~fs_).sum())))
    print('== quote known at T: rows with no quote yet today (no tq_ask, tq_bid, band) %d; rows whose quote at T is the one from before 04:00 %d' % (int(L.tq_ask.isna().sum()), int(L.pre_q.sum()) if 'pre_q' in L else -1))
    if 'e_src' in L: print('== no_entry rows by where the refusing quote was read (0 px mark of the minute, 1 carried inside the grid, 2 carried from before 04:00, 3 no quote yet today): %s' % L.e_src[L.st == 1].value_counts().sort_index().to_dict())
    if 'old_row' in L:
        print('\n== rows added by erratum B (no old label) beside the old rows')
        for nm, g in (('old rows', L[L.old_row == 1]), ('added rows', L[L.old_row == 0])):
            k_ = g.st == 0; f_ = k_ & (g.h_ret_ok == 1); print('%s: %d (pre %d, regular %d, post %d); no_entry %.4f; h30e fill coverage %.4f, marked %.4f; h30e mean fill-only %.5f' % (nm, len(g), int((g.sess == 0).sum()), int((g.sess == 1).sum()), int((g.sess == 2).sum()), float((g.st == 1).mean()), float(f_.sum() / max(1, k_.sum())), float((k_ & (g.h_mark_ok == 1)).sum() / max(1, k_.sum())), float(g.h_val[f_].mean())))
    print('\nrows by status: ' + ', '.join('%s %d' % (k, v) for k, v in L.st.map({0: 'entry filled', 1: 'no_entry', 3: 'no window'}).value_counts().items()))


if __name__ == '__main__':
    if len(sys.argv) == 2 and sys.argv[1] == 'test': test()
    elif len(sys.argv) == 6 and sys.argv[1] == 'coverage': coverage(*sys.argv[2:])
    elif len(sys.argv) >= 6 and sys.argv[1] == 'calcheck': calcheck(*sys.argv[2:])
    elif len(sys.argv) >= 6 and sys.argv[1] == 'prefix': prefix(*sys.argv[2:])
    elif len(sys.argv) == 9 and sys.argv[1] == 'keydiff': keydiff(*sys.argv[2:])
    elif len(sys.argv) == 6 and sys.argv[1] == 'published': published(*sys.argv[2:])
    elif len(sys.argv) == 9: build(*sys.argv[1:])
    else: raise SystemExit('usage: l1labels.py <minute_aggs dir> <types.csv> <day> <tk dir> <px dir> <manifest dir> <early-close days file> <out.parquet> | test | coverage <minute_aggs dir or file of dates> <early-close days file> <first day> <last day>')

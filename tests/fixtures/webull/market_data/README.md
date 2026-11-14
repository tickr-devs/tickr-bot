# Market-data shapes (names, JSON types and per-field counts)

The old bot's probes never stored raw market-data bodies (`raw_bodies_stored: false`), so these are
not full responses. They are what was recorded, copied unchanged from the owner's evidence folder
(read only). No account id appears in them (tested).

| File | Source | What it proves |
|---|---|---|
| `screener_gainers_losers_keys.json` | field-names census, Sandbox, 2026-11-10/11 | `/market-data/screeners/gainers-losers/list` v3: 200 rows, the 20 key names, for PRE_MARKET, DAY_1, AFTER_MARKET |
| `screener_top_actives_keys.json` | same | `/market-data/screeners/top-actives/list` (RELATIVE_VOLUME_10D): 200 rows, same 20 keys |
| `census_2026-11-11_after_hours.json` | capacity probe r16, Sandbox, 2026-11-11 22:00Z | per-field JSON type and missing counts: gainers DESC/ASC, top actives, snapshot regular and extended; extra snapshot members with one example value each |
| `instruments_stocks_profiles_list_mixed_types.json` | probe M1, Sandbox, 2026-11-14 (body only) | `/trading/instruments/stocks/profiles/list`: `sub_category` = COMMON_STOCK (AAPL, ADRs BABA/TSM), ETF (SPY), WARRANT (GRABW, SOUNW, OPENW x2 ids) |
| `stocks_bars_list_aapl_m1.json` | probe M2, Sandbox, 2026-11-14 (body only) | raw M1 bars: `time` "...T20:59:00.000+0000" = bar start UTC, newest first, `trading_session` RTH |
| `stocks_bars_list_aapl_m1_end_time_all_sessions_trimmed.json` | probe M4, Sandbox, 2026-11-14 (body only, 12 of 1200 bars) | `end_time` exclusive (newest bar = end - 1 min); PRE/RTH/ATH labels and boundaries; a missing PRE minute |
| `stocks_bars_list_aapl_m1_window_pre_rth.json` | probe M4, same (6 of 61 bars) | `start_time` returns one extra bar at start - 1 min |
| `streaming_subscribe_capacity_ramp.json` | probe M3, Sandbox, 2026-11-14 (event log, market closed) | stream subscribe ramp 5 → 100 accepted (empty 200), 150 refused with HTTP 417 `TOO_MANY_SYMBOLS_SUBSCRIPTION`; notice 1001 `{drop, rtt, sent}` |
| `streaming_subscribe_slot_reuse.json` | probe M3b, Sandbox, 2026-11-14 (event log, market closed) | 100 in one subscribe accepted; explicit unsubscribe of 20 frees 20 slots; `unsubscribe_all` frees all; the cap is on the current set |
| `snapshot_bars_keys.md` | owner run 2026-11-11 ~02:45Z, AAPL | snapshot regular/extended key names and types; M1 bars structure `$.result[] {delay_minutes, instrument_id, symbol, result[] {time, open, high, low, close, volume, trading_session}}` |

Facts worth reading off: all screener and snapshot values are strings except `quote_time`,
`last_trade_time`, `extend_hour_last_trade_time` (epoch ms numbers); screener `turnover` is always
absent and `relative_volume_10d` is missing in 33 to 68 of 200 rows; screeners carry no provider
time. The security type is the instrument profile's `sub_category` (probe M1).

Not recorded: an L1 quote frame, any snapshot during the regular session.

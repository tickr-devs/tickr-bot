# Market-data shapes (names, JSON types and per-field counts)

The old bot's probes never stored raw market-data bodies (`raw_bodies_stored: false`), so these are
not full responses. They are what was recorded, copied unchanged from the owner's evidence folder
(read only). No account id appears in them (tested).

| File | Source | What it proves |
|---|---|---|
| `screener_gainers_losers_keys.json` | field-names census, Sandbox, 2026-11-10/11 | `/market-data/screeners/gainers-losers/list` v3: 200 rows, the 20 key names, for PRE_MARKET, DAY_1, AFTER_MARKET |
| `screener_top_actives_keys.json` | same | `/market-data/screeners/top-actives/list` (RELATIVE_VOLUME_10D): 200 rows, same 20 keys |
| `census_2026-11-11_after_hours.json` | capacity probe r16, Sandbox, 2026-11-11 22:00Z | per-field JSON type and missing counts: gainers DESC/ASC, top actives, snapshot regular and extended; extra snapshot members with one example value each |
| `snapshot_bars_keys.md` | owner run 2026-11-11 ~02:45Z, AAPL | snapshot regular/extended key names and types; M1 bars structure `$.result[] {delay_minutes, instrument_id, symbol, result[] {time, open, high, low, close, volume, trading_session}}` |

Facts worth reading off: all screener and snapshot values are strings except `quote_time`,
`last_trade_time`, `extend_hour_last_trade_time` (epoch ms numbers); screener `turnover` is always
absent and `relative_volume_10d` is missing in 33 to 68 of 200 rows; screeners carry no provider
time. No instruments/profile response was ever recorded, so whether Webull returns a security-type
field (e.g. COMMON_STOCK) is unproven.

Not recorded: a raw bars body, an L1 stream frame, any regular-session snapshot.

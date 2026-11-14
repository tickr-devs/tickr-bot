Owner-run 2026-11-11 ~02:45Z, Sandbox, AAPL, names and JSON types only.
snapshot regular: ask ask_size bid bid_size bps change change_ratio close eps eps_ttm fifty_two_wk_high fifty_two_wk_low high instrument_id last_trade_time(number) list_status lot_size low market_value neg_market_value open out_standing_shares pb_ratio pe_ratio pre_close price ps_ratio quote_time(number) symbol total_shares trade_status turnover volume yield
snapshot extended: same + extend_hour_change extend_hour_change_ratio extend_hour_high extend_hour_last_price extend_hour_last_trade_time(number) extend_hour_low extend_hour_volume
bars M1 RTH: $.result[] {delay_minutes(number), instrument_id, symbol, result[] {close high low open time trading_session volume}}
Not in SNAPSHOT_FIELDS: fifty_two_wk_high fifty_two_wk_low list_status market_value neg_market_value out_standing_shares pb_ratio pe_ratio ps_ratio quote_time total_shares trade_status yield
Not in bar row allow-list: trading_session

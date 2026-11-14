# Owner decisions

Newest first. Only decisions that are in force. Each line: date, decision, where it applies.

| Date | Decision | Applies to |
|---|---|---|
| 2026-11-14 | Phone alert channel: iMessage to the owner himself, sent by Messages.app on the bot's Mac. Every alert goes to both the Mac notification and iMessage. The address is personal data: only the name of its environment variable is in config. | runtime.alerts, config `alerts` |
| 2026-11-14 | No ETFs and no warrants; no hardcoded symbol lists. Screener rows carry no security type, so the Webull adapter looks the type up per symbol in the instrument profile (`/trading/instruments/stocks/profiles/list`), cached per New York day; only `allowed_security_types` pass, unknown type = refused. US-listed foreign issuers (ADRs) are not excluded: the universe filter is US exchange + USD, never the issuer's country. Proven 2026-11-14 (probe M1): the type is `sub_category`, ADRs are COMMON_STOCK, identity is the instrument_id. | config `market_data`, data.discovery, broker.webull |
| 2026-11-14 | The strategy computes the position quantity: min(floor(planned loss / per-share risk), floor(notional cap / entry window top)), below 1 share = refuse; returned in `Decision.quantity`. The platform checks it against its own `[risk]` limits and refuses on breach. | strategy, spec-v1 section 5 |
| 2026-11-14 | Rewrite in this new repository from scratch; the old `Trading` project is frozen and used as a read-only reference. Port knowledge (broker facts, recorded responses, config values), not code. | everything |
| 2026-11-14 | First release scope: one active strategy, regular session only, long only, one open position, no trailing stop, minimal status screen. | spec-v1 |
| 2026-11-14 | Bot is a headless service exposing state and commands through a small local API (127.0.0.1). Next UI stage: native macOS app in SwiftUI, later iOS with push notifications. The old web dashboard is not ported. | spec-v1, backlog |
| 2026-11-14 | Near the daily loss ceiling a trade is sized down to the room left (ceiling minus net realized loss minus open reservations), never refused only because the full planned loss no longer fits; planned losses never exceed the ceiling (a gap through a stop can still take the realized result past it; owner accepted this). Below `min_daily_room_usd` (10 USD, owner confirmed) entries stop. The strategy still computes the quantity, from the smaller of its planned loss and the room. | config `risk`, strategy/base.py `loss_budget_usd` |
| 2026-11-14 | Per-symbol pause of 30 min after a trade in that symbol closes at a loss; no limit on the number of entries per day or per symbol. | config `risk`, risk/admission.py |
| 2026-11-14 | Daily loss ceiling 300 USD: today's net realized loss + open reservations + new planned loss. | config `risk` |
| 2026-11-14 | "Realized loss today" in the daily rule is the net realized result of the day: winning trades offset losing ones. Replaces "losses count gross during the proving period" (2026-10-31). | runtime/entries.py |
| 2026-11-14 | First protected probe order: 1 share, price at most 50 USD, entry at ask + 0.01, stop 2 percent below the fill, maximum loss 5 USD, window 10:00-15:30 ET, owner present. | config `probe` |
| 2026-11-14 | Historical M1-bar replay of the strategy is wanted as an evaluation tool (never an acceptance gate); the same tool later replays recorded days through other strategy versions. | stage 5, backlog |
| 2026-11-14 | Standing requirements: all numbers in commented config; Sandbox/Production only an environment switch; strategies swappable and versioned; record every signal (taken or not) and all inputs from day one for "what if we had entered" and "what if another strategy" analysis; live observer strategy versions; AI advisor that proposes strategy versions (never applies them). | everything |
| 2026-11-07 | Planned loss per trade 50 USD. | config `risk` |
| 2026-10-31 | Per-position notional cap 2,500 USD. | config `risk` |
| 2026-10-31 | Regular session only for now: last entry about 15:45 ET, everything closed by 15:55 ET. | config `session` |
| 2026-10-28 | Ceiling reached with a position open: keep the position and its stop, block new entries only. | risk |
| 2026-10-28 | No price floor; protection against thin stocks comes from liquidity rules. Minimum stop distance = max(3 spreads, 0.5 percent of price); closer = refuse. | strategy |
| 2026-10-23 | No short selling in the first Sandbox cycle. | spec-v1 |
| 2026-10-16 | Common stocks only (NMS, NAS, NSQ, NYS, ASE; COMMON_STOCK). No options, ETFs, OTC, fractional, crypto. No overnight positions. No paid market data. | config `universe` |
| 2026-10-16 | Strategy is a recipe the owner versions; changing a value means a new strategy version. No minimum-sample gate on changes. | strategy |
| 2026-10-15 | Quote age for decisions at most 5 s, end to end from provider time. | config |
| 2026-10-14 | Broker is the source of truth; order rules are exactly Webull's. | everything |
| 2026-10-13 | A bot with an open position is never restarted blindly; startup always reconciles first. | runtime |
| 2026-10-12 | Production is built ready but disabled; enabling it is configuration plus an owner authorization record, never a code change. Only Production call allowed now: the read-only trading calendar. | config, gate |
| 2026-10-11 | Secrets are never read, printed or stored. | everything |

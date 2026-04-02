# Kalshi Konnektor 2 — Re-Entry File
*Re-entry: kalshi-konnektor2*

## What This Is
Autonomous Kalshi trading bot. Builds fair-value models from external probability sources (Pinnacle, The Odds API, ESPN, Polymarket) and trades when the Kalshi price diverges enough from the model price to clear the edge threshold.

## Re-Entry Phrase
"Re-entry: kalshi-konnektor2"

## GitHub
https://github.com/papjamzzz/Kalshi-Konnektor2

## Current Status
- **LIVE** — DRY_RUN=false in .env
- NHL, NBA, MLB auto-pregame scanning enabled
- Pinnacle guest API is the primary odds source (no key needed)
- The Odds API is the secondary source (SPORTSODDSAPI_KEY_1 / ODDS_API_KEY)
- ESPN scoreboard is the tertiary fallback
- odds_cache.json provides last-resort fallback

## File Structure
```
Kalshi-Konnektor2/
├── kalshi_bot.py           ← Main bot — all logic lives here
├── injury_watchers.py      ← NBA/NHL/MLB news watchers
├── odds_keys.py            ← Multi-key rotation for The Odds API
├── requirements.txt
├── run_bot.command         ← Mac launcher
├── watch_quotes.command    ← Quote monitor launcher
├── .env                    ← API keys + all config (gitignored)
├── bot_state.json          ← Persistent position + daily loss state
├── injury_watcher_state.json ← Last watcher snapshots
├── odds_cache.json         ← Cached sportsbook events per league
├── quote_window_monitor.json ← Matched market quote timing log
├── mlb_trigger_ledger.json ← MLB starter-change trigger log
└── CLAUDE.md               ← This file
```

## How to Run
```bash
cd ~/Documents/New\ project/Kalshi-Konnektor2
pip install -r requirements.txt   # first time only
python kalshi_bot.py              # live bot loop
```

### CLI reports (no trading)
```bash
python kalshi_bot.py --quote-report
python kalshi_bot.py --mlb-probables-report
python kalshi_bot.py --mlb-trigger-report
```

## Odds Source Chain
Order the bot tries per league scan:
1. **Pinnacle** — guest API, no key, hardcoded guest token — primary
2. **The Odds API** — SPORTSODDSAPI_KEY_1…10 (or ODDS_API_KEY) — secondary
3. **ESPN scoreboard** — public, no key — tertiary
4. **odds_cache.json** — last successful snapshot — last resort

Multi-key rotation (odds_keys.py) handles The Odds API:
- Parks a key on 429/402 for the Retry-After duration
- Exhausts a key on 401/403 for the session
- Falls through to next available key instantly

## Key Config (.env)
```
KALSHI_API_KEY_ID=...
KALSHI_PRIVATE_KEY=...

# The Odds API — up to 10 keys
SPORTSODDSAPI_KEY_1=...        # or ODDS_API_KEY for single-key compat
SPORTSODDSAPI_KEY_2=...        # optional

# Auto-scan leagues
AUTO_NHL_PREGAME=true
AUTO_NBA_PREGAME=false
AUTO_MLB_PREGAME=true

# Risk gates
DRY_RUN=false
MAX_ACTIVE_POSITIONS=3
MAX_DAILY_TRADES=4
MAX_DAILY_REALIZED_LOSS_CENTS=500
DISABLE_NEW_ENTRIES=false      # emergency kill switch
```

## Models Wired
| Model | Provider | Key |
|-------|----------|-----|
| NHL moneyline | Pinnacle guest API | none — hardcoded |
| NBA moneyline | Pinnacle guest API | none — hardcoded |
| MLB moneyline | Pinnacle guest API | none — hardcoded |
| All leagues fallback | The Odds API | SPORTSODDSAPI_KEY_1…10 |
| All leagues fallback | ESPN scoreboard | none — public |
| Watchlist: Vegas | The Odds API | SPORTSODDSAPI_KEY_1…10 |
| Watchlist: Polymarket | Polymarket Gamma API | none — public |

## Risk Parameters
| Param | Current | Effect |
|-------|---------|--------|
| MIN_EDGE_CENTS | 3 | Minimum edge after spread penalty |
| MIN_VOLUME | 500 | Skip thin markets |
| MAX_ACTIVE_POSITIONS | 3 | Cap simultaneous exposure |
| MAX_DAILY_TRADES | 4 | Daily turnover cap |
| MAX_DAILY_REALIZED_LOSS_CENTS | 500 | Daily loss kill switch |
| AUTO_TAKE_PROFIT_CENTS | 8 | Auto trade take-profit |
| AUTO_STOP_LOSS_CENTS | 10 | Auto trade stop-loss |
| AUTO_MAX_PRICE_CENTS | 70 | Don't overpay on auto trades |
| COOLDOWN_MINUTES | 60 | Re-entry cooldown after exit |

## Last Session Summary (2026-04-01/02)
- Added Pinnacle guest API as primary odds source
- All three league auto-scanners (NHL/NBA/MLB) running in parallel
- Multi-key rotation module (odds_keys.py) added for The Odds API fallback
- DRY_RUN=false — bot is live

## What's Next (ROADMAP.md)
1. Validate NHL auto-scan end-to-end with real fills
2. Return to NBA market discovery reliability
3. MLB if it gives more daily action than NBA
4. Third-wave data (Rotowire, goalie confirmations, lineup news)

---
*Last updated: 2026-04-02*

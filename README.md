# Kalshi Konnektor - v3

Automated Kalshi trading bot with a fair-value model built from external probability sources.

See what the market misses, then move fast.

The bot can now run in either:

- manual watchlist mode
- automatic pregame NHL scanning mode
- automatic pregame NBA scanning mode

Build sequence:

1. NHL pregame only
2. NBA regular season + playoffs, pregame only
3. MLB and third-wave data after the core leagues are stable

## What's new in v3

| Feature | v2 | v3 |
|---|---|---|
| Entry signal | Internal heuristic score | Weighted fair probability from external sources |
| Data sources | Kalshi prices only | Polymarket, Vegas odds, optional manual input |
| Exit logic | Take-profit + stop-loss | Take-profit + stop-loss + fair-value exit |
| State handling | In-memory only | Persistent `bot_state.json` |
| Injury/news watch | None | NBA official report watcher + NHL status watcher |

## How the edge model works

Each watchlist entry can define one or more source mappings:

- `polymarket`: public Polymarket market slug + outcome
- `vegas`: sportsbook consensus through The Odds API
- `manual`: your own estimate, mainly as a fallback or blend input

The bot converts those into a weighted fair probability, then:

1. Converts fair probability into a fair Kalshi price for the side you trade.
2. Subtracts half the live spread as a simple liquidity penalty.
3. Buys only if the adjusted edge is at least `MIN_EDGE_CENTS`.
4. Skips markets where sources disagree too much or 24h volume is too low.

This is much better than the previous placeholder score, but it is not a guarantee of profitability. Source mapping quality matters.

## Setup

### 1. Credentials
```bash
cp .env.example .env
```

Fill in:

- `KALSHI_API_KEY_ID`
- `KALSHI_PRIVATE_KEY`
- `ODDS_API_KEY` if you want Vegas sportsbook input

### 2. Edit `WATCHLIST` in `kalshi_bot.py`

Set the trading fields plus one or more source mappings for each market:

- `ticker`, `side`, `max_price`, `take_profit`, `stop_loss`, `contracts`
- `polymarket=PolymarketSource(...)` for Polymarket market odds
- `vegas=VegasOddsSource(...)` for sportsbook consensus
- `manual=ManualSource(...)` if you want a fallback or blend estimate

The current watchlist includes placeholder manual values only. Replace those before live trading.

Example:

```python
WatchEntry(
    ticker="KXBTC-...",
    side="yes",
    max_price=47,
    stop_loss=37,
    take_profit=62,
    contracts=10,
    min_edge_cents=4,
    polymarket=PolymarketSource(
        slug="bitcoin-above-100k-by-december-31",
        outcome="Yes",
        weight=0.6,
    ),
    manual=ManualSource(
        probability=0.54,
        weight=0.4,
        label="research-model",
    ),
)
```

For Vegas odds, you can either provide a stable `event_id` from The Odds API or match by `home_team` and `away_team`.

### NHL auto mode

If you want the bot to choose its own pregame NHL candidates:

- set `AUTO_NHL_PREGAME=true`
- provide `ODDS_API_KEY`
- leave `AUTO_NBA_PREGAME=false` while we focus on NHL

In this mode the bot will:

1. Pull NHL moneyline consensus from sportsbooks.
2. Query Kalshi's live NHL game series.
3. Match Kalshi events and markets to the sportsbook slate.
4. Rank candidates by implied edge.
5. Trade only the best few that still pass spread, volume, and risk gates.

### NBA auto mode

If you want the bot to choose its own pregame NBA candidates:

- set `AUTO_NBA_PREGAME=true`
- provide `ODDS_API_KEY`
- leave the watchlist alone unless you want a manual fallback mode

In this mode the bot will:

1. Pull NBA moneyline consensus from sportsbooks.
2. Scan open Kalshi markets that are closing soon.
3. Match markets to NBA games by team names.
4. Ignore side markets like first-half, quarter, or first-five contracts.
5. Rank candidates by implied edge.
6. Trade only the best few that still pass spread, volume, and risk gates.

Important: this is the right way to automate selection. Letting the bot pick from all of Kalshi is not.
`MIN_AUTO_MATCH_CONFIDENCE` controls how strict the NBA market matcher is. A modest default is `0.6`.

### Injury watchers

The bot now includes two news-diff watchers:

- `NBA Official Injury Report watcher`
- `NHL Status Report watcher`

Why both:

- NHL has live Kalshi game events available right now, so it is the fastest route to a working league module.
- NBA has a clearer official reporting cadence, so it remains a strong second league once market discovery is reliable.

Current use:

1. Watch official updates.
2. Diff the new report/article against the prior snapshot.
3. Log the changed lines so we can connect news timing to sportsbook and Kalshi moves.

This gives us the foundation for faster league-specific triggers without pretending both leagues disclose news the same way.

## Strategy sequencing

This repo is intentionally being built in phases.

Phase 1:
- NHL pregame games
- same core risk engine
- sportsbook confirmation plus status-report watcher support

Phase 2:
- NBA regular season and playoff games
- pregame only
- official injury/news timing plus sportsbook confirmation

Phase 3:
- MLB if it gives more steady action than NBA at a given time
- third-wave data like fantasy/news feeds once the core engine is trustworthy

We are not trying to trade every sport or category at once. That is a feature, not a limitation.

### 3. Run locally

```bash
pip install -r requirements.txt
python kalshi_bot.py
```

### 4. Deploy to Railway

1. Push all files to a private GitHub repo.
2. In Railway, create a new project from GitHub.
3. Add environment variables from `.env`.
4. Start with `DRY_RUN=true`.
5. Watch the logs and confirm the model output looks right.
6. Flip `DRY_RUN=false` only after you have validated the source mappings.
7. Keep `DISABLE_NEW_ENTRIES=true` handy as an emergency kill switch that still allows exits.

## Risk parameters to tune

| Param | Default | Effect |
|---|---|---|
| `MIN_EDGE_CENTS` | 3 | Minimum modeled edge after spread penalty |
| `MIN_VOLUME` | 500 | Avoid thinner markets |
| `MAX_SOURCE_DISAGREEMENT_CENTS` | 20 | Skip markets where sources conflict too much |
| `MAX_ACTIVE_POSITIONS` | 3 | Cap simultaneous exposure |
| `AUTO_NHL_PREGAME` | false | Enable automatic pregame NHL scanning |
| `AUTO_NBA_PREGAME` | false | Enable automatic pregame NBA scanning |
| `NBA_LOOKAHEAD_HOURS` | 24 | Only consider markets closing within this many hours |
| `MAX_AUTO_CANDIDATES` | 5 | How many NBA candidates to keep per scan |
| `AUTO_CONTRACTS` | 2 | Default size per automatically selected trade |
| `AUTO_MAX_PRICE_CENTS` | 70 | Avoid paying too much for auto-selected contracts |
| `MIN_AUTO_MATCH_CONFIDENCE` | 0.6 | Minimum confidence for matching a Kalshi market to an NBA game |
| `ENABLE_NBA_INJURY_WATCHER` | true | Poll and diff the official NBA injury report |
| `ENABLE_NHL_INJURY_WATCHER` | true | Poll and diff the latest NHL status report |
| `INJURY_WATCHER_INTERVAL_SECONDS` | 900 | Minimum seconds between watcher polls so a slow source does not stall the loop |
| `MAX_CONTRACTS_PER_POSITION` | 10 | Hard cap on contracts per trade |
| `MAX_POSITION_COST_CENTS` | 2000 | Hard cap on cost of a single new position |
| `MAX_TOTAL_EXPOSURE_CENTS` | 5000 | Hard cap on open position cost across the bot |
| `MAX_DAILY_TRADES` | 10 | Stops opening more trades after daily turnover gets too high |
| `MAX_DAILY_REALIZED_LOSS_CENTS` | 1500 | Stops new entries after the bot loses this much in one UTC day |
| `COOLDOWN_MINUTES` | 60 | Prevent immediate re-entry after an exit |
| `FAIR_EXIT_BUFFER_CENTS` | 1 | Sell near modeled fair value before full convergence |
| `DISABLE_NEW_ENTRIES` | false | Emergency switch for unattended mode |
| `take_profit` | per entry | Exit at a fixed gain |
| `stop_loss` | per entry | Exit at a fixed loss |

## Notes

- `bot_state.json` stores open-position state between restarts.
- `injury_watcher_state.json` stores the last NBA and NHL watcher snapshots.
- Daily trade count and realized PnL are also stored in `bot_state.json`, so safety limits survive restarts.
- Polymarket is queried through the public Gamma API.
- Vegas odds are pulled through The Odds API and de-vigged at the bookmaker level before averaging.
- If no external source can be fetched for a market, the bot skips the trade.
- The bot is intentionally built to keep managing existing positions even when new entries are disabled by a risk cap or kill switch.
- The NBA auto scanner is intentionally constrained to pregame NBA-style mappings because that is safer than letting the bot choose from every market category.
- The NHL auto scanner is the current first working league path because Kalshi is already exposing NHL game events.
- Kalshi sports discovery now prefers sports series and event metadata rather than sweeping generic open markets.

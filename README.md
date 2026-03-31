# Kalshi Konnektor — v2

Automated Kalshi trading bot with edge scoring, take-profit, and volume filtering.

## What's new in v2

| Feature | v1 | v2 |
|---|---|---|
| Entry signal | Price threshold only | Price + edge score |
| Exit logic | Stop-loss only | Take-profit + stop-loss |
| Market filter | None | Minimum volume check |

## How the edge score works

Before entering any position the bot computes a score from three factors:

- **Spread width** — a wide bid/ask spread means the market is less efficiently priced, more edge available
- **Volume** — markets below `MIN_VOLUME` are skipped entirely to avoid spread bleed on exit
- **Midpoint distance** — prices near 0¢ or 100¢ extremes carry higher binary risk, scored down slightly

Score must meet `MIN_EDGE_SCORE` (default 1.0) or the bot skips the trade. Raise to 1.5 to be more selective.

## Setup

### 1. Credentials
```
cp .env.example .env
# Fill in KALSHI_API_KEY_ID and KALSHI_PRIVATE_KEY
```

### 2. Edit WATCHLIST in kalshi_bot.py
Set `ticker`, `side`, `max_price`, `take_profit`, `stop_loss`, `contracts` for each market.
Tickers are in the URL of any Kalshi market page.

### 3. Run locally
```
pip install -r requirements.txt
python kalshi_bot.py
```

### 4. Deploy to Railway (free, always on)
1. Push all files to a **private** GitHub repo
2. railway.app → New Project → Deploy from GitHub
3. Variables tab — add:
   - `KALSHI_API_KEY_ID`
   - `KALSHI_PRIVATE_KEY` (full PEM contents)
   - `DRY_RUN=true`
   - `POLL_SECONDS=60`
4. Deploy — check Logs tab
5. When dry run looks good, set `DRY_RUN=false`

## Risk parameters to tune

| Param | Default | Effect |
|---|---|---|
| `MIN_EDGE_SCORE` | 1.0 | Raise to be more selective |
| `MIN_VOLUME` | 500 | Raise to avoid thinner markets |
| `take_profit` | per entry | How much gain before exit |
| `stop_loss` | per entry | How much loss before exit |

## FAQ

**Bot crashes?** Railway auto-restarts. Bot has built-in reconnect loop.  
**Laptop off?** Irrelevant — runs on Railway servers 24/7.  
**Add markets?** Add to WATCHLIST, commit, Railway redeploys.  
**Stop the bot?** Railway → Settings → Remove service.

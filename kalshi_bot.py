"""
Kalshi Konnektor — Upgraded Bot
=================================
Improvements over v1:
  1. Edge scoring  — only enters when price is genuinely mispriced
  2. Take-profit   — exits on gain, not just stop-loss
  3. Volume filter — skips thin markets to avoid spread bleed

Setup:
  pip install -r requirements.txt
  Copy .env.example to .env and fill in credentials
  Edit WATCHLIST below
  Run: python kalshi_bot.py
"""

import os
import time
import logging
from dataclasses import dataclass, field
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kalshi")

# ── Config ─────────────────────────────────────────────────────────────────────
API_KEY_ID   = os.environ["KALSHI_API_KEY_ID"]
PRIVATE_KEY  = os.environ["KALSHI_PRIVATE_KEY"]
DRY_RUN      = os.environ.get("DRY_RUN", "true").lower() == "true"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
BASE_URL     = "https://api.elections.kalshi.com/trade-api/v2"

# ── Edge scoring thresholds ────────────────────────────────────────────────────
# Bot only enters a position when BOTH conditions are met:
#   1. Price is at or below your max_price target
#   2. Computed edge score meets MIN_EDGE_SCORE
#
# Edge score is calculated from:
#   - Spread width (wide spread = less efficient market = more edge)
#   - Volume vs MIN_VOLUME (thin book = skip entirely)
#   - Price distance from 50¢ midpoint (extreme prices carry more risk)
#
# A score of 1.0 is the minimum useful signal. Raise to 1.5+ to be selective.
MIN_EDGE_SCORE   = 1.0    # minimum edge to enter
MIN_VOLUME       = 500    # minimum 24h volume in contracts (skips thin markets)


# ── Watchlist ──────────────────────────────────────────────────────────────────
# ticker      : Kalshi market ticker — find in the URL on kalshi.com
# side        : "yes" or "no"
# max_price   : enter if ask price (cents) is AT or BELOW this
# take_profit : exit if bid price rises TO or ABOVE this (set None to hold to resolution)
# stop_loss   : exit if bid price drops TO or BELOW this
# contracts   : number of contracts per entry
@dataclass
class WatchEntry:
    ticker:      str
    side:        str
    max_price:   int
    stop_loss:   int
    contracts:   int
    take_profit: Optional[int] = None


WATCHLIST: list[WatchEntry] = [
    WatchEntry(
        ticker      = "KXBTCD-25DEC31-T50000",  # Example — replace with real tickers
        side        = "yes",
        max_price   = 38,   # buy YES if ask ≤ 38¢
        take_profit = 55,   # sell if bid ≥ 55¢  (+17¢ gain)
        stop_loss   = 25,   # sell if bid ≤ 25¢  (-13¢ loss)
        contracts   = 5,
    ),
    WatchEntry(
        ticker      = "KXETHUSD-25DEC31-T2000", # Example — replace with real tickers
        side        = "no",
        max_price   = 42,
        take_profit = 60,
        stop_loss   = 28,
        contracts   = 3,
    ),
]


# ── Kalshi client ──────────────────────────────────────────────────────────────
def build_client():
    try:
        from kalshi_python import Configuration, KalshiClient
        config = Configuration(host=BASE_URL)
        config.api_key_id  = API_KEY_ID
        config.private_key_pem = PRIVATE_KEY
        return KalshiClient(configuration=config)
    except Exception as e:
        log.error(f"Failed to build Kalshi client: {e}")
        raise


# ── Market data helpers ────────────────────────────────────────────────────────
def get_market(client, ticker: str) -> Optional[dict]:
    """Fetch current market data for a ticker."""
    try:
        resp = client.market_api.get_market(ticker)
        return resp.market if hasattr(resp, "market") else None
    except Exception as e:
        log.warning(f"Could not fetch market {ticker}: {e}")
        return None


def get_orderbook(client, ticker: str) -> Optional[dict]:
    """Fetch orderbook to get bid/ask spread."""
    try:
        resp = client.market_api.get_market_orderbook(ticker)
        return resp.orderbook if hasattr(resp, "orderbook") else None
    except Exception as e:
        log.warning(f"Could not fetch orderbook {ticker}: {e}")
        return None


# ── Edge scoring ───────────────────────────────────────────────────────────────
def compute_edge_score(
    ask: int,
    bid: int,
    volume_24h: int,
    side: str,
) -> float:
    """
    Returns a float edge score.  Higher = more edge.  Below MIN_EDGE_SCORE = skip.

    Components:
      spread_component : wide spread means the market is less efficiently priced
      volume_component : penalises thin markets
      midpoint_component: prices far from 50¢ are riskier to enter
    """
    if volume_24h < MIN_VOLUME:
        log.info(f"  Volume {volume_24h} below minimum {MIN_VOLUME} — skipping")
        return 0.0

    spread = ask - bid  # cents

    # Spread component: spread of 10+ cents = good, 0-2 cents = marginal
    spread_score = min(spread / 10.0, 2.0)

    # Volume component: scales from 0.5 (at minimum) to 1.5 (at 5× minimum)
    volume_score = min(0.5 + (volume_24h / MIN_VOLUME) * 0.2, 1.5)

    # Midpoint component: penalise entries very close to 0 or 100 (binary extremes)
    price = ask if side == "yes" else (100 - ask)
    midpoint_distance = abs(price - 50)
    midpoint_score = 1.0 - (midpoint_distance / 100.0)

    score = round((spread_score + volume_score + midpoint_score) / 3.0, 3)
    return score


# ── Order execution ────────────────────────────────────────────────────────────
def place_order(client, ticker: str, action: str, side: str, price: int, count: int):
    """Place a buy or sell order.  Skips entirely in DRY_RUN mode."""
    label = "BUY" if action == "buy" else "SELL"
    log.info(f"  {'[DRY RUN] ' if DRY_RUN else ''}ORDER: {label} {count}x {side.upper()} on {ticker} @ {price}¢")

    if DRY_RUN:
        return

    try:
        from kalshi_python.models import CreateOrderRequest
        req = CreateOrderRequest(
            ticker    = ticker,
            client_order_id = f"kkbot-{ticker}-{int(time.time())}",
            type      = "limit",
            action    = action,
            side      = side,
            count     = count,
            yes_price = price if side == "yes" else (100 - price),
            no_price  = price if side == "no"  else (100 - price),
        )
        client.order_api.create_order(req)
        log.info(f"  Order placed successfully.")
    except Exception as e:
        log.error(f"  Order failed for {ticker}: {e}")


# ── Main loop ──────────────────────────────────────────────────────────────────
def run():
    log.info("=" * 60)
    log.info(f"Kalshi Konnektor starting — DRY_RUN={DRY_RUN}")
    log.info(f"Monitoring {len(WATCHLIST)} markets, polling every {POLL_SECONDS}s")
    log.info(f"Min edge score: {MIN_EDGE_SCORE}  |  Min volume: {MIN_VOLUME}")
    log.info("=" * 60)

    client = build_client()

    bought: dict[str, int]  = {}   # ticker -> entry price (cents)
    sold:   set[str]        = set()

    while True:
        for entry in WATCHLIST:
            ticker = entry.ticker
            log.info(f"Checking {ticker} ({entry.side.upper()})...")

            market   = get_market(client, ticker)
            if not market:
                continue

            # ── Pull prices ──────────────────────────────────────────────────
            ask = getattr(market, f"{entry.side}_ask", None)
            bid = getattr(market, f"{entry.side}_bid", None)
            vol = getattr(market, "volume_24h", 0) or 0

            if ask is None or bid is None:
                log.warning(f"  Missing price data for {ticker}")
                continue

            log.info(f"  Ask: {ask}¢  Bid: {bid}¢  Volume 24h: {vol}")

            # ── SELL logic ───────────────────────────────────────────────────
            if ticker in bought and ticker not in sold:
                entry_price = bought[ticker]

                # Take-profit
                if entry.take_profit and bid >= entry.take_profit:
                    log.info(f"  ✅ TAKE PROFIT triggered @ {bid}¢ (entry: {entry_price}¢, target: {entry.take_profit}¢)")
                    place_order(client, ticker, "sell", entry.side, bid, entry.contracts)
                    sold.add(ticker)
                    continue

                # Stop-loss
                if bid <= entry.stop_loss:
                    log.warning(f"  🛑 STOP LOSS triggered @ {bid}¢ (entry: {entry_price}¢, floor: {entry.stop_loss}¢)")
                    place_order(client, ticker, "sell", entry.side, bid, entry.contracts)
                    sold.add(ticker)
                    continue

                log.info(f"  Holding. Entry: {entry_price}¢  TP: {entry.take_profit}¢  SL: {entry.stop_loss}¢")
                continue

            # ── BUY logic ────────────────────────────────────────────────────
            if ticker in bought or ticker in sold:
                log.info(f"  Already traded — skipping.")
                continue

            if ask > entry.max_price:
                log.info(f"  Price {ask}¢ above target {entry.max_price}¢ — waiting.")
                continue

            # Edge score check
            score = compute_edge_score(ask, bid, vol, entry.side)
            log.info(f"  Edge score: {score} (min: {MIN_EDGE_SCORE})")

            if score < MIN_EDGE_SCORE:
                log.info(f"  Edge too low — skipping.")
                continue

            log.info(f"  ✅ Entry conditions met. Score: {score}")
            place_order(client, ticker, "buy", entry.side, ask, entry.contracts)
            bought[ticker] = ask

        log.info(f"Sleeping {POLL_SECONDS}s...\n")
        time.sleep(POLL_SECONDS)


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    while True:
        try:
            run()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as e:
            log.error(f"Unexpected crash: {e} — restarting in 30s...")
            time.sleep(30)

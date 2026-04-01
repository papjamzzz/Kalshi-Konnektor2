"""
Kalshi Konnektor — multi-source edge bot
========================================

This version turns the previous placeholder "edge score" into a real fair-value
model. Each watchlist entry can pull probability inputs from:

- Polymarket public market data
- Vegas odds via The Odds API
- An optional manual probability estimate

The bot aggregates those inputs into a fair probability, converts that into a
fair Kalshi price, and only buys when the modeled edge clears a configurable
threshold after accounting for spread.

Build direction:
- Phase 1: NBA regular season + playoffs, pregame only
- Phase 2: NHL pregame only
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

import requests
from dotenv import load_dotenv

from injury_watchers import NBAInjuryWatcher, NHLStatusWatcher

load_dotenv()


# -- Logging ------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("kalshi")


# -- Config -------------------------------------------------------------------
API_KEY_ID = os.environ["KALSHI_API_KEY_ID"]
PRIVATE_KEY = os.environ["KALSHI_PRIVATE_KEY"]
DRY_RUN = os.environ.get("DRY_RUN", "true").lower() == "true"
POLL_SECONDS = int(os.environ.get("POLL_SECONDS", "60"))
BASE_URL = os.environ.get("KALSHI_BASE_URL", "https://api.elections.kalshi.com/trade-api/v2")

REQUEST_TIMEOUT_SECONDS = int(os.environ.get("REQUEST_TIMEOUT_SECONDS", "10"))
MIN_VOLUME = int(os.environ.get("MIN_VOLUME", "500"))
MIN_EDGE_CENTS = float(os.environ.get("MIN_EDGE_CENTS", "3"))
MAX_SOURCE_DISAGREEMENT_CENTS = float(os.environ.get("MAX_SOURCE_DISAGREEMENT_CENTS", "20"))
MAX_ACTIVE_POSITIONS = int(os.environ.get("MAX_ACTIVE_POSITIONS", "3"))
EXIT_ON_FAIR_VALUE = os.environ.get("EXIT_ON_FAIR_VALUE", "true").lower() == "true"
FAIR_EXIT_BUFFER_CENTS = float(os.environ.get("FAIR_EXIT_BUFFER_CENTS", "1"))
COOLDOWN_MINUTES = int(os.environ.get("COOLDOWN_MINUTES", "60"))
MAX_CONTRACTS_PER_POSITION = int(os.environ.get("MAX_CONTRACTS_PER_POSITION", "10"))
MAX_POSITION_COST_CENTS = int(os.environ.get("MAX_POSITION_COST_CENTS", "2000"))
MAX_TOTAL_EXPOSURE_CENTS = int(os.environ.get("MAX_TOTAL_EXPOSURE_CENTS", "5000"))
MAX_DAILY_TRADES = int(os.environ.get("MAX_DAILY_TRADES", "10"))
MAX_DAILY_REALIZED_LOSS_CENTS = int(os.environ.get("MAX_DAILY_REALIZED_LOSS_CENTS", "1500"))
DISABLE_NEW_ENTRIES = os.environ.get("DISABLE_NEW_ENTRIES", "false").lower() == "true"
STATE_FILE = Path(os.environ.get("STATE_FILE", "bot_state.json"))
AUTO_NBA_PREGAME = os.environ.get("AUTO_NBA_PREGAME", "false").lower() == "true"
NBA_LOOKAHEAD_HOURS = int(os.environ.get("NBA_LOOKAHEAD_HOURS", "24"))
MAX_AUTO_CANDIDATES = int(os.environ.get("MAX_AUTO_CANDIDATES", "5"))
AUTO_CONTRACTS = int(os.environ.get("AUTO_CONTRACTS", "2"))
AUTO_TAKE_PROFIT_CENTS = int(os.environ.get("AUTO_TAKE_PROFIT_CENTS", "8"))
AUTO_STOP_LOSS_CENTS = int(os.environ.get("AUTO_STOP_LOSS_CENTS", "10"))
AUTO_MAX_PRICE_CENTS = int(os.environ.get("AUTO_MAX_PRICE_CENTS", "70"))
AUTO_MIN_TIME_TO_CLOSE_MINUTES = int(os.environ.get("AUTO_MIN_TIME_TO_CLOSE_MINUTES", "20"))
MIN_AUTO_MATCH_CONFIDENCE = float(os.environ.get("MIN_AUTO_MATCH_CONFIDENCE", "0.6"))

DEFAULT_POLYMARKET_WEIGHT = float(os.environ.get("DEFAULT_POLYMARKET_WEIGHT", "0.55"))
DEFAULT_VEGAS_WEIGHT = float(os.environ.get("DEFAULT_VEGAS_WEIGHT", "0.35"))
DEFAULT_MANUAL_WEIGHT = float(os.environ.get("DEFAULT_MANUAL_WEIGHT", "0.10"))

ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
ODDS_API_BASE_URL = "https://api.the-odds-api.com/v4/sports"
POLYMARKET_BASE_URL = "https://gamma-api.polymarket.com"
NBA_SPORT_KEY = "basketball_nba"
NBA_KALSHI_SERIES = ("KXNBAGAMES", "KXMVENBASINGLEGAME")
NHL_KALSHI_SERIES = ("KXNHLGAME",)
ENABLE_NBA_INJURY_WATCHER = os.environ.get("ENABLE_NBA_INJURY_WATCHER", "true").lower() == "true"
ENABLE_NHL_INJURY_WATCHER = os.environ.get("ENABLE_NHL_INJURY_WATCHER", "true").lower() == "true"
INJURY_WATCHER_STATE_FILE = Path(os.environ.get("INJURY_WATCHER_STATE_FILE", "injury_watcher_state.json"))
NBA_SERIES_DISCOVERY_TERMS = ("nba", "pro basketball")
NHL_SERIES_DISCOVERY_TERMS = ("nhl", "pro hockey")
INJURY_WATCHER_INTERVAL_SECONDS = int(os.environ.get("INJURY_WATCHER_INTERVAL_SECONDS", "900"))
WATCHER_LAST_POLLED: dict[str, float] = {}


# -- Data model ----------------------------------------------------------------
@dataclass
class PolymarketSource:
    slug: str
    outcome: str = "Yes"
    weight: float = DEFAULT_POLYMARKET_WEIGHT


@dataclass
class VegasOddsSource:
    sport: str
    outcome: str
    event_id: Optional[str] = None
    home_team: Optional[str] = None
    away_team: Optional[str] = None
    market: str = "h2h"
    regions: str = "us"
    bookmakers: Optional[str] = None
    weight: float = DEFAULT_VEGAS_WEIGHT


@dataclass
class ManualSource:
    probability: float
    weight: float = DEFAULT_MANUAL_WEIGHT
    label: str = "manual"


@dataclass
class WatchEntry:
    ticker: str
    side: str
    max_price: int
    stop_loss: int
    contracts: int
    take_profit: Optional[int] = None
    min_edge_cents: Optional[float] = None
    polymarket: Optional[PolymarketSource] = None
    vegas: Optional[VegasOddsSource] = None
    manual: Optional[ManualSource] = None
    notes: str = ""
    league: str = "custom"


@dataclass
class SourceSignal:
    name: str
    probability: float
    weight: float
    detail: str


@dataclass
class EdgeDecision:
    fair_probability: float
    fair_price_cents: float
    raw_edge_cents: float
    adjusted_edge_cents: float
    spread_cents: int
    disagreement_cents: float
    signals: list[SourceSignal]


@dataclass
class OddsEvent:
    event_id: str
    commence_ts: int
    home_team: str
    away_team: str
    home_probability: float
    away_probability: float
    sport_key: str


@dataclass
class MarketEventMatch:
    event: OddsEvent
    confidence: float


@dataclass
class Position:
    ticker: str
    side: str
    entry_price: int
    contracts: int
    stop_loss: int
    take_profit: Optional[int]
    opened_at: int


# -- Watchlist -----------------------------------------------------------------
# Replace these examples with real Kalshi tickers and source mappings.
WATCHLIST: list[WatchEntry] = [
    WatchEntry(
        ticker="KXBTCD-25DEC31-T50000",
        side="yes",
        max_price=38,
        take_profit=55,
        stop_loss=25,
        contracts=5,
        min_edge_cents=4,
        manual=ManualSource(probability=0.44, weight=1.0, label="placeholder"),
        # polymarket=PolymarketSource(slug="bitcoin-above-50000-on-december-31", outcome="Yes"),
        notes="Replace placeholder source mapping before live trading.",
    ),
    WatchEntry(
        ticker="KXETHUSD-25DEC31-T2000",
        side="no",
        max_price=42,
        take_profit=60,
        stop_loss=28,
        contracts=3,
        min_edge_cents=4,
        manual=ManualSource(probability=0.57, weight=1.0, label="placeholder"),
        # vegas=VegasOddsSource(
        #     sport="basketball_nba",
        #     event_id="replace-with-odds-api-event-id",
        #     outcome="Los Angeles Lakers",
        # ),
        notes="Replace placeholder source mapping before live trading.",
    ),
]


TEAM_ALIASES: dict[str, set[str]] = {
    "atlanta hawks": {"atlanta hawks", "hawks", "atl"},
    "boston celtics": {"boston celtics", "celtics", "bos"},
    "brooklyn nets": {"brooklyn nets", "nets", "bkn", "bk"},
    "charlotte hornets": {"charlotte hornets", "hornets", "cha"},
    "chicago bulls": {"chicago bulls", "bulls", "chi"},
    "cleveland cavaliers": {"cleveland cavaliers", "cavaliers", "cavs", "cle"},
    "dallas mavericks": {"dallas mavericks", "mavericks", "mavs", "dal"},
    "denver nuggets": {"denver nuggets", "nuggets", "den"},
    "detroit pistons": {"detroit pistons", "pistons", "det"},
    "golden state warriors": {"golden state warriors", "warriors", "gsw", "golden state"},
    "houston rockets": {"houston rockets", "rockets", "hou"},
    "indiana pacers": {"indiana pacers", "pacers", "ind"},
    "los angeles clippers": {"los angeles clippers", "clippers", "lac", "la clippers"},
    "los angeles lakers": {"los angeles lakers", "lakers", "lal", "la lakers"},
    "memphis grizzlies": {"memphis grizzlies", "grizzlies", "mem"},
    "miami heat": {"miami heat", "heat", "mia"},
    "milwaukee bucks": {"milwaukee bucks", "bucks", "mil"},
    "minnesota timberwolves": {"minnesota timberwolves", "timberwolves", "wolves", "min"},
    "new orleans pelicans": {"new orleans pelicans", "pelicans", "nop", "no pelicans"},
    "new york knicks": {"new york knicks", "knicks", "nyk"},
    "oklahoma city thunder": {"oklahoma city thunder", "thunder", "okc"},
    "orlando magic": {"orlando magic", "magic", "orl"},
    "philadelphia 76ers": {"philadelphia 76ers", "76ers", "sixers", "phi"},
    "phoenix suns": {"phoenix suns", "suns", "phx", "pho"},
    "portland trail blazers": {"portland trail blazers", "trail blazers", "blazers", "por"},
    "sacramento kings": {"sacramento kings", "kings", "sac"},
    "san antonio spurs": {"san antonio spurs", "spurs", "sas"},
    "toronto raptors": {"toronto raptors", "raptors", "tor"},
    "utah jazz": {"utah jazz", "jazz", "uta"},
    "washington wizards": {"washington wizards", "wizards", "was"},
}


# -- State ---------------------------------------------------------------------
def load_state() -> dict[str, Any]:
    if not STATE_FILE.exists():
        return {"positions": {}, "cooldowns": {}, "daily": {}}

    try:
        with STATE_FILE.open("r", encoding="utf-8") as handle:
            state = json.load(handle)
        state.setdefault("positions", {})
        state.setdefault("cooldowns", {})
        state.setdefault("daily", {})
        return state
    except Exception as exc:
        log.warning(f"Could not load state file {STATE_FILE}: {exc}")
        return {"positions": {}, "cooldowns": {}, "daily": {}}


def save_state(state: dict[str, Any]) -> None:
    try:
        with STATE_FILE.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, indent=2, sort_keys=True)
    except Exception as exc:
        log.error(f"Could not save state file {STATE_FILE}: {exc}")


def get_open_position(state: dict[str, Any], ticker: str) -> Optional[Position]:
    payload = state.get("positions", {}).get(ticker)
    if not payload:
        return None
    try:
        return Position(**payload)
    except TypeError:
        log.warning(f"State for {ticker} is malformed; ignoring persisted position.")
        return None


def set_open_position(state: dict[str, Any], position: Position) -> None:
    state.setdefault("positions", {})[position.ticker] = asdict(position)
    save_state(state)


def clear_open_position(state: dict[str, Any], ticker: str) -> None:
    state.setdefault("positions", {}).pop(ticker, None)
    state.setdefault("cooldowns", {})[ticker] = int(time.time())
    save_state(state)


def in_cooldown(state: dict[str, Any], ticker: str) -> bool:
    last_exit = state.get("cooldowns", {}).get(ticker)
    if not last_exit:
        return False
    elapsed_minutes = (time.time() - int(last_exit)) / 60
    return elapsed_minutes < COOLDOWN_MINUTES


def current_day_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def get_day_stats(state: dict[str, Any]) -> dict[str, Any]:
    day = current_day_key()
    daily = state.setdefault("daily", {})
    stats = daily.setdefault(day, {"trades": 0, "realized_pnl_cents": 0})
    return stats


def record_trade_count(state: dict[str, Any]) -> None:
    stats = get_day_stats(state)
    stats["trades"] = int(stats.get("trades", 0)) + 1
    save_state(state)


def record_realized_pnl(state: dict[str, Any], pnl_cents: int) -> None:
    stats = get_day_stats(state)
    stats["realized_pnl_cents"] = int(stats.get("realized_pnl_cents", 0)) + pnl_cents
    save_state(state)


def daily_trade_limit_reached(state: dict[str, Any]) -> bool:
    stats = get_day_stats(state)
    return int(stats.get("trades", 0)) >= MAX_DAILY_TRADES


def daily_loss_limit_reached(state: dict[str, Any]) -> bool:
    stats = get_day_stats(state)
    realized_pnl_cents = int(stats.get("realized_pnl_cents", 0))
    return realized_pnl_cents <= (-1 * MAX_DAILY_REALIZED_LOSS_CENTS)


def current_total_exposure_cents(state: dict[str, Any]) -> int:
    total = 0
    for payload in state.get("positions", {}).values():
        try:
            position = Position(**payload)
        except TypeError:
            continue
        total += position.entry_price * position.contracts
    return total


def proposed_position_cost_cents(entry: WatchEntry, ask: int) -> int:
    return ask * entry.contracts


def can_open_new_position(state: dict[str, Any], entry: WatchEntry, ask: int) -> tuple[bool, str]:
    if DISABLE_NEW_ENTRIES:
        return False, "new entries are disabled by config"
    if daily_trade_limit_reached(state):
        return False, f"daily trade cap {MAX_DAILY_TRADES} reached"
    if daily_loss_limit_reached(state):
        return False, f"daily realized loss cap {MAX_DAILY_REALIZED_LOSS_CENTS}c reached"
    if entry.contracts > MAX_CONTRACTS_PER_POSITION:
        return False, f"contracts {entry.contracts} exceed cap {MAX_CONTRACTS_PER_POSITION}"

    position_cost_cents = proposed_position_cost_cents(entry, ask)
    if position_cost_cents > MAX_POSITION_COST_CENTS:
        return False, f"position cost {position_cost_cents}c exceeds cap {MAX_POSITION_COST_CENTS}c"

    if current_total_exposure_cents(state) + position_cost_cents > MAX_TOTAL_EXPOSURE_CENTS:
        return False, f"total exposure would exceed cap {MAX_TOTAL_EXPOSURE_CENTS}c"

    return True, ""


# -- Kalshi client -------------------------------------------------------------
def build_client():
    try:
        from kalshi_python import Configuration, KalshiClient
        from kalshi_python.api.events_api import EventsApi
        from kalshi_python.api.markets_api import MarketsApi
        from kalshi_python.api.portfolio_api import PortfolioApi
        from kalshi_python.api.series_api import SeriesApi

        config = Configuration(host=BASE_URL)
        config.api_key_id = API_KEY_ID
        config.private_key_pem = PRIVATE_KEY
        api_client = KalshiClient(configuration=config)
        return {
            "client": api_client,
            "events_api": EventsApi(api_client),
            "markets_api": MarketsApi(api_client),
            "portfolio_api": PortfolioApi(api_client),
            "series_api": SeriesApi(api_client),
        }
    except Exception as exc:
        log.error(f"Failed to build Kalshi client: {exc}")
        raise


def get_market(client, ticker: str) -> Optional[Any]:
    try:
        resp = client["markets_api"].get_market(ticker)
        return resp.market if hasattr(resp, "market") else None
    except Exception as exc:
        log.warning(f"Could not fetch market {ticker}: {exc}")
        return None


def place_order(client, ticker: str, action: str, side: str, price: int, count: int) -> bool:
    label = "BUY" if action == "buy" else "SELL"
    log.info(
        f"  {'[DRY RUN] ' if DRY_RUN else ''}ORDER: "
        f"{label} {count}x {side.upper()} on {ticker} @ {price}c"
    )

    if DRY_RUN:
        return True

    try:
        from kalshi_python.models import CreateOrderRequest

        req = CreateOrderRequest(
            ticker=ticker,
            client_order_id=f"kkbot-{ticker}-{int(time.time())}",
            type="limit",
            action=action,
            side=side,
            count=count,
            yes_price=price if side == "yes" else (100 - price),
            no_price=price if side == "no" else (100 - price),
        )
        client["portfolio_api"].create_order(req)
        log.info("  Order placed successfully.")
        return True
    except Exception as exc:
        log.error(f"  Order failed for {ticker}: {exc}")
        return False


# -- HTTP helpers --------------------------------------------------------------
def get_json(url: str, params: Optional[dict[str, Any]] = None) -> Any:
    response = requests.get(url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    return response.json()


def clamp_probability(value: float) -> float:
    return max(0.0, min(1.0, value))


def implied_probability_from_american(price: Any) -> Optional[float]:
    try:
        odds = float(price)
    except (TypeError, ValueError):
        return None

    if odds == 0:
        return None
    if odds > 0:
        return 100.0 / (odds + 100.0)
    return abs(odds) / (abs(odds) + 100.0)


def parse_json_list(raw_value: Any) -> list[Any]:
    if isinstance(raw_value, list):
        return raw_value
    if isinstance(raw_value, str):
        return json.loads(raw_value)
    raise ValueError(f"Unsupported list payload: {type(raw_value)}")


def to_unix_timestamp(value: Any) -> Optional[int]:
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return int(value)

    if isinstance(value, str):
        parsed = value.strip()
        if not parsed:
            return None
        parsed = parsed.replace("Z", "+00:00")
        try:
            return int(datetime.fromisoformat(parsed).timestamp())
        except ValueError:
            return None

    return None


def normalize_text(value: str) -> str:
    normalized = value.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9\s]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized


def canonical_team_name(value: str) -> str:
    normalized = normalize_text(value)
    for canonical, aliases in TEAM_ALIASES.items():
        if normalized == canonical or normalized in aliases:
            return canonical
    return normalized


def matches_team(text: str, team_name: str) -> bool:
    haystack = f" {normalize_text(text)} "
    canonical = canonical_team_name(team_name)
    aliases = TEAM_ALIASES.get(canonical, {canonical})
    return any(f" {alias} " in haystack for alias in aliases)


def identify_team_from_text(text: str, teams: list[str]) -> Optional[str]:
    matches = [team for team in teams if matches_team(text, team)]
    if len(matches) == 1:
        return matches[0]
    return None


def team_match_count(text: str, teams: list[str]) -> int:
    return sum(1 for team in teams if matches_team(text, team))


# -- External sources ----------------------------------------------------------
def fetch_polymarket_signal(source: PolymarketSource) -> Optional[SourceSignal]:
    try:
        market = get_json(f"{POLYMARKET_BASE_URL}/markets/slug/{source.slug}")
        outcomes = parse_json_list(market.get("outcomes", []))
        prices = [float(value) for value in parse_json_list(market.get("outcomePrices", []))]

        lookup = {
            str(outcome).strip().lower(): clamp_probability(float(price))
            for outcome, price in zip(outcomes, prices)
        }
        probability = lookup.get(source.outcome.strip().lower())
        if probability is None:
            log.warning(f"  Polymarket outcome '{source.outcome}' not found for slug '{source.slug}'.")
            return None

        detail = f"slug={source.slug} outcome={source.outcome}"
        return SourceSignal(
            name="Polymarket",
            probability=probability,
            weight=source.weight,
            detail=detail,
        )
    except Exception as exc:
        log.warning(f"  Polymarket fetch failed for {source.slug}: {exc}")
        return None


def find_odds_event(source: VegasOddsSource) -> Optional[dict[str, Any]]:
    if source.event_id:
        return get_json(
            f"{ODDS_API_BASE_URL}/{source.sport}/events/{source.event_id}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": source.regions,
                "markets": source.market,
                "oddsFormat": "american",
                **({"bookmakers": source.bookmakers} if source.bookmakers else {}),
            },
        )

    events = get_json(
        f"{ODDS_API_BASE_URL}/{source.sport}/odds",
        params={
            "apiKey": ODDS_API_KEY,
            "regions": source.regions,
            "markets": source.market,
            "oddsFormat": "american",
            **({"bookmakers": source.bookmakers} if source.bookmakers else {}),
        },
    )

    if not source.home_team or not source.away_team:
        raise ValueError("Vegas source requires event_id or both home_team and away_team.")

    for event in events:
        home_team = str(event.get("home_team", "")).strip().lower()
        away_team = str(event.get("away_team", "")).strip().lower()
        if home_team == source.home_team.strip().lower() and away_team == source.away_team.strip().lower():
            return event

    return None


def fetch_vegas_signal(source: VegasOddsSource) -> Optional[SourceSignal]:
    if not ODDS_API_KEY:
        log.warning("  Vegas odds source configured but ODDS_API_KEY is missing.")
        return None

    try:
        event = find_odds_event(source)
        if not event:
            log.warning("  Vegas odds event not found.")
            return None

        target_outcome = source.outcome.strip().lower()
        bookmaker_probabilities: list[float] = []

        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != source.market:
                    continue

                implied: dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    label = str(outcome.get("name", "")).strip().lower()
                    probability = implied_probability_from_american(outcome.get("price"))
                    if probability is not None:
                        implied[label] = probability

                if target_outcome not in implied:
                    continue

                total = sum(implied.values())
                if total <= 0:
                    continue

                bookmaker_probabilities.append(clamp_probability(implied[target_outcome] / total))

        if not bookmaker_probabilities:
            log.warning(f"  Vegas odds outcome '{source.outcome}' not found in bookmaker data.")
            return None

        probability = sum(bookmaker_probabilities) / len(bookmaker_probabilities)
        detail = f"sport={source.sport} market={source.market} outcome={source.outcome}"
        return SourceSignal(
            name="VegasOdds",
            probability=probability,
            weight=source.weight,
            detail=detail,
        )
    except Exception as exc:
        log.warning(f"  Vegas odds fetch failed: {exc}")
        return None


def fetch_manual_signal(source: ManualSource) -> Optional[SourceSignal]:
    probability = clamp_probability(source.probability)
    return SourceSignal(
        name=source.label,
        probability=probability,
        weight=source.weight,
        detail=f"manual={probability:.3f}",
    )


def fetch_moneyline_events_for_sport(sport_key: str) -> list[OddsEvent]:
    if not ODDS_API_KEY:
        log.warning(f"  Auto-scan for {sport_key} is enabled but ODDS_API_KEY is missing.")
        return []

    try:
        events = get_json(
            f"{ODDS_API_BASE_URL}/{sport_key}/odds",
            params={
                "apiKey": ODDS_API_KEY,
                "regions": "us",
                "markets": "h2h",
                "oddsFormat": "american",
            },
        )
    except Exception as exc:
        log.warning(f"  Could not fetch NBA odds events: {exc}")
        return []

    results: list[OddsEvent] = []
    for event in events:
        home_team = str(event.get("home_team", "")).strip()
        away_team = str(event.get("away_team", "")).strip()
        event_id = str(event.get("id", "")).strip()
        commence_ts = to_unix_timestamp(event.get("commence_time"))

        if not home_team or not away_team or not event_id or commence_ts is None:
            continue

        home_probs: list[float] = []
        away_probs: list[float] = []

        for bookmaker in event.get("bookmakers", []):
            for market in bookmaker.get("markets", []):
                if market.get("key") != "h2h":
                    continue

                implied: dict[str, float] = {}
                for outcome in market.get("outcomes", []):
                    label = canonical_team_name(str(outcome.get("name", "")).strip())
                    probability = implied_probability_from_american(outcome.get("price"))
                    if probability is not None:
                        implied[label] = probability

                home_key = canonical_team_name(home_team)
                away_key = canonical_team_name(away_team)
                if home_key not in implied or away_key not in implied:
                    continue

                total = implied[home_key] + implied[away_key]
                if total <= 0:
                    continue

                home_probs.append(clamp_probability(implied[home_key] / total))
                away_probs.append(clamp_probability(implied[away_key] / total))

        if not home_probs or not away_probs:
            continue

        results.append(
            OddsEvent(
                event_id=event_id,
                commence_ts=commence_ts,
                home_team=home_team,
                away_team=away_team,
                home_probability=sum(home_probs) / len(home_probs),
                away_probability=sum(away_probs) / len(away_probs),
                sport_key=sport_key,
            )
        )

    return results


def fetch_nba_odds_events() -> list[OddsEvent]:
    return fetch_moneyline_events_for_sport(NBA_SPORT_KEY)


def get_markets_page(
    client,
    cursor: Optional[str] = None,
    min_close_ts: Optional[int] = None,
    max_close_ts: Optional[int] = None,
) -> tuple[list[Any], Optional[str]]:
    try:
        kwargs: dict[str, Any] = {"status": "open", "limit": 200}
        if cursor:
            kwargs["cursor"] = cursor
        if min_close_ts is not None:
            kwargs["min_close_ts"] = min_close_ts
        if max_close_ts is not None:
            kwargs["max_close_ts"] = max_close_ts
        response = client["markets_api"].get_markets(**kwargs)
    except Exception as exc:
        log.warning(f"  Could not fetch markets page: {exc}")
        return [], None

    markets = list(getattr(response, "markets", []) or [])
    next_cursor = getattr(response, "cursor", None) or getattr(response, "next_cursor", None)
    return markets, next_cursor


def fetch_open_kalshi_markets(client) -> list[Any]:
    markets: list[Any] = []
    cursor: Optional[str] = None
    now = int(time.time())
    min_close_ts = now + (AUTO_MIN_TIME_TO_CLOSE_MINUTES * 60)
    max_close_ts = now + (NBA_LOOKAHEAD_HOURS * 3600)

    for _ in range(20):
        page, cursor = get_markets_page(
            client,
            cursor=cursor,
            min_close_ts=min_close_ts,
            max_close_ts=max_close_ts,
        )
        if not page:
            break
        markets.extend(page)
        if not cursor:
            break

    return markets


def fetch_events_for_series(client, series_tickers: tuple[str, ...]) -> list[Any]:
    now = int(time.time()) + (AUTO_MIN_TIME_TO_CLOSE_MINUTES * 60)
    events: list[Any] = []

    for series_ticker in series_tickers:
        cursor: Optional[str] = None
        for _ in range(10):
            try:
                response = client["events_api"].get_events(
                    status="open",
                    limit=200,
                    cursor=cursor,
                    with_nested_markets=True,
                    series_ticker=series_ticker,
                    min_close_ts=now,
                )
            except Exception as exc:
                log.warning(f"  Could not fetch events for series {series_ticker}: {exc}")
                break

            page = list(getattr(response, "events", []) or [])
            if not page:
                break
            events.extend(page)
            cursor = getattr(response, "cursor", None) or getattr(response, "next_cursor", None)
            if not cursor:
                break

    return events


def discover_series_tickers(client, league: str) -> tuple[str, ...]:
    preferred = NBA_KALSHI_SERIES if league == "NBA" else NHL_KALSHI_SERIES
    terms = NBA_SERIES_DISCOVERY_TERMS if league == "NBA" else NHL_SERIES_DISCOVERY_TERMS

    try:
        response = client["series_api"].get_series(status="open")
    except Exception as exc:
        log.warning(f"  Could not fetch Kalshi series metadata for {league}: {exc}")
        return preferred

    discovered: list[str] = []
    for series in list(getattr(response, "series", []) or []):
        title = str(getattr(series, "title", "") or "")
        category = str(getattr(series, "category", "") or "")
        ticker = str(getattr(series, "ticker", "") or "")
        searchable = normalize_text(" ".join([title, category, ticker]))
        if "sports" not in searchable:
            continue
        if not any(term in searchable for term in terms):
            continue
        if "game" in searchable or "winner" in searchable or "moneyline" in searchable or "single game" in searchable:
            discovered.append(ticker)

    ordered = list(dict.fromkeys([*preferred, *sorted(discovered)]))
    return tuple(ordered)


def market_closes_soon(market: Any) -> bool:
    close_ts = to_unix_timestamp(getattr(market, "close_time", None) or getattr(market, "expiration_time", None))
    if close_ts is None:
        return False

    now = int(time.time())
    min_close_delta = AUTO_MIN_TIME_TO_CLOSE_MINUTES * 60
    max_close_delta = NBA_LOOKAHEAD_HOURS * 3600
    delta = close_ts - now
    return min_close_delta <= delta <= max_close_delta


def match_market_to_event(market: Any, events: list[OddsEvent]) -> Optional[MarketEventMatch]:
    searchable = " ".join(
        [
            str(getattr(market, "title", "") or ""),
            str(getattr(market, "subtitle", "") or ""),
            str(getattr(market, "yes_sub_title", "") or ""),
            str(getattr(market, "no_sub_title", "") or ""),
            str(getattr(market, "ticker", "") or ""),
        ]
    )
    searchable_normalized = normalize_text(searchable)
    market_close_ts = to_unix_timestamp(getattr(market, "close_time", None) or getattr(market, "expiration_time", None))
    best_match: Optional[MarketEventMatch] = None

    for event in events:
        teams = [event.home_team, event.away_team]
        score = 0.0

        matched_teams = team_match_count(searchable, teams)
        if matched_teams == 2:
            score += 0.7
        elif matched_teams == 1:
            score += 0.35
        else:
            continue

        if "nba" in searchable_normalized:
            score += 0.1
        if "game" in searchable_normalized or "winner" in searchable_normalized or "win" in searchable_normalized:
            score += 0.05

        if market_close_ts is not None:
            delta_hours = abs(market_close_ts - event.commence_ts) / 3600.0
            if delta_hours <= 1:
                score += 0.2
            elif delta_hours <= 3:
                score += 0.1
            elif delta_hours > 8:
                score -= 0.1

        confidence = max(0.0, min(score, 1.0))
        if confidence < MIN_AUTO_MATCH_CONFIDENCE:
            continue

        candidate = MarketEventMatch(event=event, confidence=confidence)
        if best_match is None or candidate.confidence > best_match.confidence:
            best_match = candidate

    return best_match


def match_kalshi_event_to_odds_event(kalshi_event: Any, events: list[OddsEvent]) -> Optional[MarketEventMatch]:
    searchable = " ".join(
        [
            str(getattr(kalshi_event, "title", "") or ""),
            str(getattr(kalshi_event, "sub_title", "") or ""),
            str(getattr(kalshi_event, "event_ticker", "") or ""),
            str(getattr(kalshi_event, "series_ticker", "") or ""),
        ]
    )

    pseudo_market = type("PseudoMarket", (), {})()
    pseudo_market.title = searchable
    pseudo_market.subtitle = str(getattr(kalshi_event, "sub_title", "") or "")
    pseudo_market.yes_sub_title = None
    pseudo_market.no_sub_title = None
    pseudo_market.ticker = str(getattr(kalshi_event, "event_ticker", "") or "")
    pseudo_market.close_time = None
    pseudo_market.expiration_time = None
    return match_market_to_event(pseudo_market, events)


def choose_market_side(market: Any, event: OddsEvent) -> Optional[tuple[str, float]]:
    yes_label = str(getattr(market, "yes_sub_title", "") or getattr(market, "title", "") or "")
    no_label = str(getattr(market, "no_sub_title", "") or "")
    teams = [event.home_team, event.away_team]

    yes_team = identify_team_from_text(yes_label, teams)
    no_team = identify_team_from_text(no_label, teams) if no_label else None

    yes_ask = getattr(market, "yes_ask", None)
    no_ask = getattr(market, "no_ask", None)

    if yes_team:
        probability = event.home_probability if canonical_team_name(yes_team) == canonical_team_name(event.home_team) else event.away_probability
        if yes_ask is not None and yes_ask <= AUTO_MAX_PRICE_CENTS:
            return "yes", probability

    if no_team:
        no_probability = event.home_probability if canonical_team_name(no_team) == canonical_team_name(event.home_team) else event.away_probability
        if no_ask is not None and no_ask <= AUTO_MAX_PRICE_CENTS:
            return "no", no_probability

    if yes_team and no_ask is not None and no_ask <= AUTO_MAX_PRICE_CENTS:
        return "no", 1.0 - (
            event.home_probability if canonical_team_name(yes_team) == canonical_team_name(event.home_team) else event.away_probability
        )

    if no_team and yes_ask is not None and yes_ask <= AUTO_MAX_PRICE_CENTS:
        return "yes", 1.0 - (
            event.home_probability if canonical_team_name(no_team) == canonical_team_name(event.home_team) else event.away_probability
        )

    return None


def build_watch_entry_from_market(
    market: Any,
    event: OddsEvent,
    side: str,
    probability: float,
    confidence: float,
    league: str,
) -> Optional[tuple[float, WatchEntry]]:
    ask = getattr(market, f"{side}_ask", None)
    if ask is None or ask > AUTO_MAX_PRICE_CENTS:
        return None

    fair_price = probability * 100.0 if side == "yes" else (1.0 - probability) * 100.0
    edge_guess = fair_price - ask
    ticker = str(getattr(market, "ticker", "") or "").strip()
    if not ticker:
        return None

    return (
        edge_guess,
        WatchEntry(
            ticker=ticker,
            side=side,
            max_price=AUTO_MAX_PRICE_CENTS,
            stop_loss=max(1, ask - AUTO_STOP_LOSS_CENTS),
            take_profit=min(99, ask + AUTO_TAKE_PROFIT_CENTS),
            contracts=AUTO_CONTRACTS,
            min_edge_cents=MIN_EDGE_CENTS,
            manual=ManualSource(
                probability=probability,
                weight=1.0,
                label=f"{league.lower()}-consensus:{event.away_team} at {event.home_team}",
            ),
            notes=f"auto-{league.lower()} event={event.event_id} confidence={confidence:.2f}",
            league=league,
        ),
    )


def build_auto_league_watchlist(client, league: str, odds_events: list[OddsEvent]) -> list[WatchEntry]:
    if not odds_events:
        log.info(f"  No {league} sportsbook events available for auto-scan.")
        return []

    series_tickers = discover_series_tickers(client, league=league)
    log.info(f"  Kalshi {league} series search set: {', '.join(series_tickers) if series_tickers else 'none'}")
    kalshi_events = fetch_events_for_series(client, series_tickers=series_tickers)
    log.info(f"  Auto-scan pulled {len(kalshi_events)} Kalshi events for {league}.")
    candidates: list[tuple[float, WatchEntry]] = []

    for kalshi_event in kalshi_events:
        matched_event = match_kalshi_event_to_odds_event(kalshi_event, odds_events)
        if not matched_event:
            continue

        event = matched_event.event
        markets = list(getattr(kalshi_event, "markets", []) or [])
        for market in markets:
            if not market_closes_soon(market):
                continue

            side_choice = choose_market_side(market, event)
            if not side_choice:
                continue

            side, probability = side_choice
            built = build_watch_entry_from_market(
                market=market,
                event=event,
                side=side,
                probability=probability,
                confidence=matched_event.confidence,
                league=league,
            )
            if built:
                candidates.append(built)

    candidates.sort(key=lambda item: item[0], reverse=True)
    if not candidates:
        log.info(f"  No {league} Kalshi candidates matched the current sportsbook slate.")
    return [entry for _, entry in candidates[:MAX_AUTO_CANDIDATES]]


def build_auto_nba_watchlist(client) -> list[WatchEntry]:
    odds_events = fetch_nba_odds_events()
    return build_auto_league_watchlist(client, league="NBA", odds_events=odds_events)


# -- Edge model ----------------------------------------------------------------
def aggregate_fair_probability(entry: WatchEntry) -> tuple[Optional[float], list[SourceSignal]]:
    signals: list[SourceSignal] = []

    if entry.polymarket:
        signal = fetch_polymarket_signal(entry.polymarket)
        if signal:
            signals.append(signal)

    if entry.vegas:
        signal = fetch_vegas_signal(entry.vegas)
        if signal:
            signals.append(signal)

    if entry.manual:
        signal = fetch_manual_signal(entry.manual)
        if signal:
            signals.append(signal)

    if not signals:
        return None, []

    total_weight = sum(signal.weight for signal in signals if signal.weight > 0)
    if total_weight <= 0:
        return None, signals

    fair_probability = sum(signal.probability * signal.weight for signal in signals) / total_weight
    return clamp_probability(fair_probability), signals


def compute_edge_decision(entry: WatchEntry, ask: int, bid: int, volume_24h: int) -> Optional[EdgeDecision]:
    if volume_24h < MIN_VOLUME:
        log.info(f"  Volume {volume_24h} below minimum {MIN_VOLUME} - skipping")
        return None

    fair_probability, signals = aggregate_fair_probability(entry)
    if fair_probability is None or not signals:
        log.info("  No usable external probability sources - skipping")
        return None

    source_prices = [signal.probability * 100.0 for signal in signals]
    disagreement = max(source_prices) - min(source_prices) if len(source_prices) > 1 else 0.0
    if disagreement > MAX_SOURCE_DISAGREEMENT_CENTS:
        log.info(
            f"  Source disagreement {disagreement:.2f}c exceeds cap "
            f"{MAX_SOURCE_DISAGREEMENT_CENTS:.2f}c - skipping"
        )
        return None

    fair_price_cents = fair_probability * 100.0 if entry.side == "yes" else (1.0 - fair_probability) * 100.0
    spread_cents = max(ask - bid, 0)
    raw_edge_cents = fair_price_cents - ask
    adjusted_edge_cents = raw_edge_cents - (spread_cents / 2.0)

    return EdgeDecision(
        fair_probability=fair_probability,
        fair_price_cents=fair_price_cents,
        raw_edge_cents=raw_edge_cents,
        adjusted_edge_cents=adjusted_edge_cents,
        spread_cents=spread_cents,
        disagreement_cents=disagreement,
        signals=signals,
    )


def min_edge_required(entry: WatchEntry) -> float:
    return entry.min_edge_cents if entry.min_edge_cents is not None else MIN_EDGE_CENTS


def active_positions_count(state: dict[str, Any]) -> int:
    return len(state.get("positions", {}))


def log_edge_decision(decision: EdgeDecision) -> None:
    sources = ", ".join(
        f"{signal.name}={signal.probability * 100:.1f}% (w={signal.weight:.2f})"
        for signal in decision.signals
    )
    log.info(
        "  Fair prob: %.2f%%  Fair px: %.2fc  Edge raw: %.2fc  Edge adj: %.2fc  Spread: %sc",
        decision.fair_probability * 100.0,
        decision.fair_price_cents,
        decision.raw_edge_cents,
        decision.adjusted_edge_cents,
        decision.spread_cents,
    )
    log.info(f"  Source mix: {sources}")


def log_daily_risk_snapshot(state: dict[str, Any]) -> None:
    stats = get_day_stats(state)
    exposure = current_total_exposure_cents(state)
    realized = int(stats.get("realized_pnl_cents", 0))
    trades = int(stats.get("trades", 0))
    log.info(
        "Daily snapshot: trades=%s/%s  realized_pnl=%sc  open_exposure=%sc/%sc",
        trades,
        MAX_DAILY_TRADES,
        realized,
        exposure,
        MAX_TOTAL_EXPOSURE_CENTS,
    )


def summarize_diff_lines(lines: list[str], limit: int = 5) -> str:
    if not lines:
        return "none"
    preview = lines[:limit]
    if len(lines) > limit:
        preview.append(f"... (+{len(lines) - limit} more)")
    return " | ".join(preview)


def watcher_should_poll(name: str) -> bool:
    now = time.time()
    last_polled = WATCHER_LAST_POLLED.get(name, 0.0)
    if now - last_polled < INJURY_WATCHER_INTERVAL_SECONDS:
        return False
    WATCHER_LAST_POLLED[name] = now
    return True


def poll_injury_watchers() -> None:
    if ENABLE_NBA_INJURY_WATCHER and watcher_should_poll("nba"):
        try:
            nba_diff = NBAInjuryWatcher(
                INJURY_WATCHER_STATE_FILE,
                timeout_seconds=max(REQUEST_TIMEOUT_SECONDS, 20),
            ).poll()
            if nba_diff and nba_diff.changed:
                log.info(f"NBA injury watcher updated from {nba_diff.fetched_from}")
                log.info(f"  Added: {summarize_diff_lines(nba_diff.added)}")
                if nba_diff.removed:
                    log.info(f"  Removed: {summarize_diff_lines(nba_diff.removed)}")
        except Exception as exc:
            log.warning(f"NBA injury watcher failed: {exc}")

    if ENABLE_NHL_INJURY_WATCHER and watcher_should_poll("nhl"):
        try:
            nhl_diff = NHLStatusWatcher(INJURY_WATCHER_STATE_FILE, timeout_seconds=REQUEST_TIMEOUT_SECONDS).poll()
            if nhl_diff and nhl_diff.changed:
                log.info(f"NHL status watcher updated from {nhl_diff.fetched_from}")
                log.info(f"  Added: {summarize_diff_lines(nhl_diff.added)}")
                if nhl_diff.removed:
                    log.info(f"  Removed: {summarize_diff_lines(nhl_diff.removed)}")
        except Exception as exc:
            log.warning(f"NHL status watcher failed: {exc}")


# -- Main loop -----------------------------------------------------------------
def run() -> None:
    log.info("=" * 60)
    log.info(f"Kalshi Konnektor starting - DRY_RUN={DRY_RUN}")
    log.info(f"Monitoring {len(WATCHLIST)} markets, polling every {POLL_SECONDS}s")
    log.info(
        "Min edge: %.2fc  |  Min volume: %s  |  Max open positions: %s",
        MIN_EDGE_CENTS,
        MIN_VOLUME,
        MAX_ACTIVE_POSITIONS,
    )
    log.info(
        "Auto mode: nba_pregame=%s  lookahead=%sh  auto_candidates=%s  auto_contracts=%s",
        AUTO_NBA_PREGAME,
        NBA_LOOKAHEAD_HOURS,
        MAX_AUTO_CANDIDATES,
        AUTO_CONTRACTS,
    )
    log.info(
        "Risk caps: max position=%sc  max exposure=%sc  max daily loss=%sc  new entries disabled=%s",
        MAX_POSITION_COST_CENTS,
        MAX_TOTAL_EXPOSURE_CENTS,
        MAX_DAILY_REALIZED_LOSS_CENTS,
        DISABLE_NEW_ENTRIES,
    )
    log.info("=" * 60)

    client = build_client()
    state = load_state()

    while True:
        state = load_state()
        log_daily_risk_snapshot(state)
        poll_injury_watchers()
        entries = build_auto_nba_watchlist(client) if AUTO_NBA_PREGAME else WATCHLIST
        log.info(f"Active candidate set: {len(entries)} entries")

        for entry in entries:
            ticker = entry.ticker
            side = entry.side.lower()
            log.info(f"Checking {ticker} ({side.upper()})...")

            market = get_market(client, ticker)
            if not market:
                continue

            ask = getattr(market, f"{side}_ask", None)
            bid = getattr(market, f"{side}_bid", None)
            volume_24h = getattr(market, "volume_24h", 0) or 0

            if ask is None or bid is None:
                log.warning(f"  Missing price data for {ticker}")
                continue

            log.info(f"  Ask: {ask}c  Bid: {bid}c  Volume 24h: {volume_24h}")

            position = get_open_position(state, ticker)
            decision = compute_edge_decision(entry, ask, bid, volume_24h)
            if decision:
                log_edge_decision(decision)

            if position:
                if entry.take_profit is not None and bid >= entry.take_profit:
                    log.info(
                        f"  TAKE PROFIT triggered @ {bid}c "
                        f"(entry {position.entry_price}c, target {entry.take_profit}c)"
                    )
                    if place_order(client, ticker, "sell", side, bid, position.contracts):
                        record_trade_count(state)
                        record_realized_pnl(state, (bid - position.entry_price) * position.contracts)
                        clear_open_position(state, ticker)
                    continue

                if bid <= entry.stop_loss:
                    log.warning(
                        f"  STOP LOSS triggered @ {bid}c "
                        f"(entry {position.entry_price}c, floor {entry.stop_loss}c)"
                    )
                    if place_order(client, ticker, "sell", side, bid, position.contracts):
                        record_trade_count(state)
                        record_realized_pnl(state, (bid - position.entry_price) * position.contracts)
                        clear_open_position(state, ticker)
                    continue

                if decision and EXIT_ON_FAIR_VALUE and bid >= (decision.fair_price_cents - FAIR_EXIT_BUFFER_CENTS):
                    log.info(
                        f"  FAIR VALUE EXIT triggered @ {bid}c "
                        f"(fair {decision.fair_price_cents:.2f}c, buffer {FAIR_EXIT_BUFFER_CENTS:.2f}c)"
                    )
                    if place_order(client, ticker, "sell", side, bid, position.contracts):
                        record_trade_count(state)
                        record_realized_pnl(state, (bid - position.entry_price) * position.contracts)
                        clear_open_position(state, ticker)
                    continue

                log.info(
                    f"  Holding. Entry: {position.entry_price}c  "
                    f"TP: {entry.take_profit}c  SL: {entry.stop_loss}c"
                )
                continue

            if in_cooldown(state, ticker):
                log.info(f"  Cooling down after last exit ({COOLDOWN_MINUTES} min window).")
                continue

            if active_positions_count(state) >= MAX_ACTIVE_POSITIONS:
                log.info("  Position cap reached - skipping new entry.")
                continue

            if ask > entry.max_price:
                log.info(f"  Price {ask}c above target {entry.max_price}c - waiting.")
                continue

            if not decision:
                continue

            threshold = min_edge_required(entry)
            if decision.adjusted_edge_cents < threshold:
                log.info(
                    f"  Adjusted edge {decision.adjusted_edge_cents:.2f}c "
                    f"below threshold {threshold:.2f}c - skipping"
                )
                continue

            allowed, reason = can_open_new_position(state, entry, ask)
            if not allowed:
                log.info(f"  Risk gate blocked entry: {reason}.")
                continue

            log.info(f"  ENTRY conditions met with adjusted edge {decision.adjusted_edge_cents:.2f}c")
            if place_order(client, ticker, "buy", side, ask, entry.contracts):
                record_trade_count(state)
                set_open_position(
                    state,
                    Position(
                        ticker=ticker,
                        side=side,
                        entry_price=ask,
                        contracts=entry.contracts,
                        stop_loss=entry.stop_loss,
                        take_profit=entry.take_profit,
                        opened_at=int(time.time()),
                    ),
                )

        log.info(f"Sleeping {POLL_SECONDS}s...\n")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    while True:
        try:
            run()
        except KeyboardInterrupt:
            log.info("Stopped by user.")
            break
        except Exception as exc:
            log.error(f"Unexpected crash: {exc} - restarting in 30s...")
            time.sleep(30)

"""
free_odds.py — Zero-cost parallel odds sources for Kalshi Konnektor2
=====================================================================

Provides live probability signals from three free/guest-access sources that
run in parallel threads and feed directly into aggregate_fair_probability().

Sources
-------
1. Pinnacle (guest key — sharpest book in the world, no-vig = true fair value)
2. Action Network (public consensus API — aggregated sharp-money line)
3. DraftKings (public JSON endpoint — largest US book, useful soft-line ref)

All three fire simultaneously.  Any source that fails just drops out — the
remaining signals still aggregate.  Zero paid keys required.

Usage
-----
    from free_odds import FreeOddsSource, fetch_free_signals

    source = FreeOddsSource(
        sport_key="basketball_nba",
        home_team="Boston Celtics",
        away_team="Los Angeles Lakers",
        outcome="home",   # "home" or "away"
    )
    signals = fetch_free_signals(source)
    # returns list[FreeSignal], empty if all sources fail
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass, field
from typing import Any, Optional

import requests
from dotenv import load_dotenv

load_dotenv()

# ── Constants ──────────────────────────────────────────────────────────────────

PINNACLE_BASE   = "https://guest.api.arcadia.pinnacle.com/0.1"
PINNACLE_KEY    = os.getenv("PINNACLE_GUEST_KEY", "CmX2KcMrXuFmNg6YFbmTxE0y9CQnkinu")
PINNACLE_LEAGUES: dict[str, int] = {
    "basketball_nba": 487,
    "icehockey_nhl":  1456,
    "baseball_mlb":   246,
}

ACTION_NETWORK_SPORT: dict[str, str] = {
    "basketball_nba": "nba",
    "icehockey_nhl":  "nhl",
    "baseball_mlb":   "mlb",
}

# DraftKings sport → eventGroupId
DK_EVENT_GROUPS: dict[str, int] = {
    "basketball_nba": 42648,
    "icehockey_nhl":  42133,
    "baseball_mlb":   84240,
}

TIMEOUT     = 10     # seconds per request
THREAD_TTL  = 9.0    # max seconds to wait for each parallel thread


# ── Return type (no dependency on kalshi_bot.py) ───────────────────────────────

@dataclass
class FreeSignal:
    source:      str    # "pinnacle" | "action_network" | "draftkings"
    probability: float  # no-vig implied probability for the requested outcome
    weight:      float  # set by FreeOddsSource per-source config
    detail:      str    # human-readable context string


@dataclass
class FreeOddsSource:
    sport_key:  str
    home_team:  str
    away_team:  str
    outcome:    str       # "home" or "away"

    use_pinnacle:       bool  = True
    use_action_network: bool  = True
    use_draftkings:     bool  = True

    # Individual source weights — used when signals are collected
    pinnacle_weight:        float = 0.50   # Pinnacle = sharpest, highest trust
    action_network_weight:  float = 0.30   # Consensus sharp-money line
    draftkings_weight:      float = 0.20   # Largest US book, lower trust


# ── Internal helpers ───────────────────────────────────────────────────────────

def _get(url: str, params: dict | None = None,
         headers: dict | None = None) -> Any:
    r = requests.get(url, params=params, headers=headers, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json()


def _american_to_prob(price: Any) -> Optional[float]:
    """Raw American odds → raw implied probability (NOT no-vig)."""
    try:
        odds = float(price)
    except (TypeError, ValueError):
        return None
    if odds == 0:
        return None
    return 100.0 / (odds + 100.0) if odds > 0 else abs(odds) / (abs(odds) + 100.0)


def _novig(home_raw: float, away_raw: float) -> tuple[float, float]:
    """Remove bookmaker vig → true probabilities that sum to 1.0."""
    total = home_raw + away_raw
    if total <= 0:
        return home_raw, away_raw
    return home_raw / total, away_raw / total


def _tokens(name: str) -> set[str]:
    """Split a team name into meaningful tokens (3+ chars, lowercased)."""
    return {w for w in name.lower().replace(".", "").split() if len(w) >= 3}


def _matches(a: str, b: str) -> bool:
    """True if team name strings share at least one significant token."""
    return bool(_tokens(a) & _tokens(b))


# ── Source 1: Pinnacle ─────────────────────────────────────────────────────────

def _fetch_pinnacle(sport_key: str, home_team: str,
                    away_team: str, outcome: str) -> Optional[FreeSignal]:
    """
    Pull Pinnacle's moneyline for the given matchup via the public guest API.
    Pinnacle is the reference sharp book — their no-vig line is closest to
    true fair value.  Guest key has been stable for years.
    """
    league_id = PINNACLE_LEAGUES.get(sport_key)
    if not league_id:
        return None

    headers = {"User-Agent": "Mozilla/5.0", "X-Api-Key": PINNACLE_KEY}

    matchups_raw = _get(
        f"{PINNACLE_BASE}/leagues/{league_id}/matchups",
        params={"withSpecials": "false"},
        headers=headers,
    )

    matchup_map: dict[int, dict] = {}
    for m in matchups_raw:
        mid = m.get("id")
        participants = m.get("participants", [])
        h = next((p for p in participants if p.get("alignment") == "home"), None)
        a = next((p for p in participants if p.get("alignment") == "away"), None)
        if mid and h and a:
            matchup_map[mid] = {
                "home": str(h.get("name", "")).strip(),
                "away": str(a.get("name", "")).strip(),
            }

    markets_raw = _get(
        f"{PINNACLE_BASE}/leagues/{league_id}/markets/straight",
        headers=headers,
    )

    for market in markets_raw:
        if not str(market.get("key", "")).startswith("s;0;m"):
            continue
        mid = market.get("matchupId")
        game = matchup_map.get(mid)
        if not game:
            continue
        if not (_matches(game["home"], home_team) and _matches(game["away"], away_team)):
            continue

        prices = market.get("prices", [])
        hp = _american_to_prob(
            next((p.get("price") for p in prices if p.get("designation") == "home"), None)
        )
        ap = _american_to_prob(
            next((p.get("price") for p in prices if p.get("designation") == "away"), None)
        )
        if hp is None or ap is None:
            continue

        h_nov, a_nov = _novig(hp, ap)
        prob = h_nov if outcome.lower() == "home" else a_nov
        return FreeSignal(
            source="pinnacle",
            probability=prob,
            weight=0.0,   # caller sets this
            detail=f"Pinnacle: {game['home']} p={h_nov:.3f} / {game['away']} p={a_nov:.3f}",
        )

    return None


# ── Source 2: Action Network ───────────────────────────────────────────────────

def _fetch_action_network(sport_key: str, home_team: str,
                          away_team: str, outcome: str) -> Optional[FreeSignal]:
    """
    Action Network public API — returns aggregated consensus moneyline from
    the sharp market.  No API key required.
    """
    sport = ACTION_NETWORK_SPORT.get(sport_key)
    if not sport:
        return None

    data = _get("https://api.actionnetwork.com/web/v1/games", params={"sport": sport})
    games = data.get("games", [])

    for g in games:
        ht = (g.get("home_team") or {})
        at = (g.get("away_team") or {})
        ht_name = ht.get("full_name") or ht.get("display_name") or ht.get("abbr") or ""
        at_name = at.get("full_name") or at.get("display_name") or at.get("abbr") or ""

        if not (_matches(ht_name, home_team) and _matches(at_name, away_team)):
            continue

        lines = g.get("lines", [])
        if not lines:
            continue

        # Prefer consensus book (id 0 or labelled "consensus"), else first entry
        con = next(
            (l for l in lines
             if str(l.get("book_id", "")).strip() in ("0", "consensus")
             or str(l.get("affil_id", "")).strip() in ("0", "consensus")),
            None,
        )
        line = con or lines[0]

        ml_home = (line.get("ml_home") or line.get("moneyline_home") or
                   line.get("home_ml") or line.get("homeMoneyline"))
        ml_away = (line.get("ml_away") or line.get("moneyline_away") or
                   line.get("away_ml") or line.get("awayMoneyline"))

        hp = _american_to_prob(ml_home)
        ap = _american_to_prob(ml_away)
        if hp is None or ap is None:
            continue

        h_nov, a_nov = _novig(hp, ap)
        prob = h_nov if outcome.lower() == "home" else a_nov
        return FreeSignal(
            source="action_network",
            probability=prob,
            weight=0.0,
            detail=f"ActionNet: {ht_name} ml={ml_home} / {at_name} ml={ml_away}",
        )

    return None


# ── Source 3: DraftKings ───────────────────────────────────────────────────────

def _fetch_draftkings(sport_key: str, home_team: str,
                      away_team: str, outcome: str) -> Optional[FreeSignal]:
    """
    DraftKings public sportsbook API — no auth required.  Soft-line book but
    high volume and useful as a cross-reference.
    """
    eg_id = DK_EVENT_GROUPS.get(sport_key)
    if not eg_id:
        return None

    data = _get(
        f"https://sportsbook-nash.draftkings.com/sites/US-SB/api/v5/eventgroups/{eg_id}",
        params={"format": "json"},
    )

    eg = data.get("eventGroup", {})

    # Try top-level events first, fall back to offerCategories
    events: list[dict] = eg.get("events") or []
    if not events:
        for cat in eg.get("offerCategories", []):
            for sub in cat.get("offerSubcategoryDescriptors", []):
                sub_data = sub.get("offerSubcategory", {})
                for offer_group in sub_data.get("offers", []):
                    for offer in (offer_group if isinstance(offer_group, list) else [offer_group]):
                        label = str(offer.get("label", "")).lower()
                        if "moneyline" not in label:
                            continue
                        # Try to get team name from outcomes
                        outcomes = offer.get("outcomes", [])
                        if len(outcomes) < 2:
                            continue
                        p1 = str(outcomes[0].get("participant", ""))
                        p2 = str(outcomes[1].get("participant", ""))
                        # Check if one is home and one is away
                        if _matches(p1, home_team) and _matches(p2, away_team):
                            hp = _american_to_prob(outcomes[0].get("oddsAmerican"))
                            ap = _american_to_prob(outcomes[1].get("oddsAmerican"))
                        elif _matches(p2, home_team) and _matches(p1, away_team):
                            hp = _american_to_prob(outcomes[1].get("oddsAmerican"))
                            ap = _american_to_prob(outcomes[0].get("oddsAmerican"))
                        else:
                            continue
                        if hp is None or ap is None:
                            continue
                        h_nov, a_nov = _novig(hp, ap)
                        prob = h_nov if outcome.lower() == "home" else a_nov
                        return FreeSignal(
                            source="draftkings",
                            probability=prob,
                            weight=0.0,
                            detail=f"DraftKings: {p1} / {p2}",
                        )

    for event in events:
        name = str(event.get("name", "") or event.get("eventName", "") or "")

        # Parse "Away @ Home" or "Home vs Away"
        sep = None
        if " @ " in name:
            sep = " @ "
        elif " vs. " in name.lower():
            sep = " vs. " if " vs. " in name else " VS. "
        elif " vs " in name.lower():
            sep = " vs "

        if not sep:
            continue

        parts = name.split(sep, 1)
        if len(parts) != 2:
            continue

        if sep.strip() == "@":
            dk_away, dk_home = parts[0].strip(), parts[1].strip()
        else:
            dk_home, dk_away = parts[0].strip(), parts[1].strip()

        if not (_matches(dk_home, home_team) and _matches(dk_away, away_team)):
            continue

        offers = event.get("offers", [])
        for offer in offers:
            label = str(offer.get("label", "")).lower()
            if "moneyline" not in label and "money line" not in label:
                continue
            outcomes = offer.get("outcomes", [])
            home_odds = next(
                (o.get("oddsAmerican") for o in outcomes if _matches(str(o.get("participant", "")), home_team)),
                None,
            )
            away_odds = next(
                (o.get("oddsAmerican") for o in outcomes if _matches(str(o.get("participant", "")), away_team)),
                None,
            )
            hp = _american_to_prob(home_odds)
            ap = _american_to_prob(away_odds)
            if hp is None or ap is None:
                continue
            h_nov, a_nov = _novig(hp, ap)
            prob = h_nov if outcome.lower() == "home" else a_nov
            return FreeSignal(
                source="draftkings",
                probability=prob,
                weight=0.0,
                detail=f"DraftKings: {dk_home} ml={home_odds} / {dk_away} ml={away_odds}",
            )

    return None


# ── Main public function ───────────────────────────────────────────────────────

def fetch_free_signals(source: FreeOddsSource) -> list[FreeSignal]:
    """
    Run all enabled free-source fetchers in parallel and collect results.

    Returns a list of FreeSignal objects with weights already set.
    May return an empty list if all sources fail or return no match —
    callers should handle that gracefully.

    Thread-safe.  Each fetcher has a hard timeout of THREAD_TTL seconds.
    """
    tasks: list[tuple[str, Any, float]] = []
    if source.use_pinnacle:
        tasks.append(("pinnacle", lambda: _fetch_pinnacle(
            source.sport_key, source.home_team, source.away_team, source.outcome),
            source.pinnacle_weight))
    if source.use_action_network:
        tasks.append(("action_network", lambda: _fetch_action_network(
            source.sport_key, source.home_team, source.away_team, source.outcome),
            source.action_network_weight))
    if source.use_draftkings:
        tasks.append(("draftkings", lambda: _fetch_draftkings(
            source.sport_key, source.home_team, source.away_team, source.outcome),
            source.draftkings_weight))

    collected: list[Optional[FreeSignal]] = [None] * len(tasks)
    lock = threading.Lock()

    def _run(idx: int, fn: Any, weight: float) -> None:
        try:
            sig = fn()
            if sig is not None:
                sig.weight = weight
                with lock:
                    collected[idx] = sig
        except Exception:
            pass   # each source fails silently — others still contribute

    threads = [
        threading.Thread(target=_run, args=(i, t[1], t[2]), daemon=True)
        for i, t in enumerate(tasks)
    ]
    for th in threads:
        th.start()
    for th in threads:
        th.join(timeout=THREAD_TTL)

    return [s for s in collected if s is not None]

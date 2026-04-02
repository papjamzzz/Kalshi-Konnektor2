"""
odds_keys.py — Multi-key rotation for The Odds API
----------------------------------------------------
Supports up to 10 keys loaded from env vars:
  SPORTSODDSAPI_KEY_1 … SPORTSODDSAPI_KEY_10

Backward compatible: if none of those are set, falls back to ODDS_API_KEY.

On 429/402: parks that key for Retry-After seconds, falls through to next.
On 401/403: marks key exhausted for the session.
On 5xx: retries with backoff up to MAX_RETRY times.

Usage (drop-in replacement for direct ODDS_API_KEY calls):
  from odds_keys import get_odds_client
  client = get_odds_client()
  data = client.get_odds(sport_key, regions="us")   # returns raw list or []
"""

from __future__ import annotations

import os
import time
import threading
import requests
from dotenv import load_dotenv

load_dotenv()

ODDS_API_BASE = "https://api.the-odds-api.com/v4/sports"
MAX_KEYS      = 10
TIMEOUT       = 12
MAX_RETRY     = 3


# -- Key slot -----------------------------------------------------------------

class _KeySlot:
    def __init__(self, index: int, key: str):
        self.index        = index
        self.key          = key
        self.exhausted    = False
        self.parked_until = 0.0

    def is_available(self) -> bool:
        return not self.exhausted and time.time() >= self.parked_until

    def park(self, seconds: float) -> None:
        self.parked_until = time.time() + max(seconds, 1.0)
        print(f"  ⏳ [OddsAPI] key_{self.index} parked for {seconds:.0f}s")

    def exhaust(self) -> None:
        self.exhausted = True
        print(f"  ✗  [OddsAPI] key_{self.index} exhausted (bad credentials)")

    @property
    def status(self) -> str:
        if self.exhausted:
            return "exhausted"
        wait = self.parked_until - time.time()
        if wait > 0:
            return f"parked_{int(wait)}s"
        return "ready"


# -- Client -------------------------------------------------------------------

class OddsApiClient:
    def __init__(self):
        self._lock  = threading.Lock()
        self._slots: list[_KeySlot] = []
        self._load_keys()

    def _load_keys(self) -> None:
        slots = []
        # Numbered keys take priority
        for i in range(1, MAX_KEYS + 1):
            key = os.getenv(f"SPORTSODDSAPI_KEY_{i}", "").strip()
            if key:
                slots.append(_KeySlot(i, key))

        # Backward compat: ODDS_API_KEY as slot 0 if no numbered keys found
        if not slots:
            legacy = os.getenv("ODDS_API_KEY", "").strip()
            if legacy:
                slots.append(_KeySlot(0, legacy))
                print("  ✓ [OddsAPI] 1 key loaded (ODDS_API_KEY)")

        if slots and slots[0].index != 0:
            print(f"  ✓ [OddsAPI] {len(slots)} key(s) loaded")

        # Preserve exhausted/parked state across reloads
        old = {s.index: s for s in self._slots}
        for s in slots:
            if s.index in old:
                s.exhausted    = old[s.index].exhausted
                s.parked_until = old[s.index].parked_until

        self._slots = slots

    def reload_keys(self) -> None:
        """Hot-reload keys from env — safe to call without restart."""
        with self._lock:
            load_dotenv(override=True)
            self._load_keys()

    @property
    def has_keys(self) -> bool:
        return bool(self._slots)

    def key_status(self) -> list[dict]:
        return [{"index": s.index, "status": s.status} for s in self._slots]

    def get_odds(self, sport_key: str, regions: str = "us",
                 markets: str = "h2h", odds_format: str = "american") -> list:
        """
        Fetch moneyline events for a sport. Returns raw list from The Odds API
        or [] if all keys fail. Rotates keys on rate-limit automatically.
        """
        path   = f"{ODDS_API_BASE}/{sport_key}/odds"
        params = {"regions": regions, "markets": markets, "oddsFormat": odds_format}
        result = self._request(path, params)
        return result if isinstance(result, list) else []

    def get_event_odds(self, sport_key: str, event_id: str,
                       regions: str = "us", markets: str = "h2h",
                       odds_format: str = "american",
                       bookmakers: str | None = None) -> dict | None:
        """
        Fetch odds for a single event by ID. Returns the event dict or None.
        """
        path   = f"{ODDS_API_BASE}/{sport_key}/events/{event_id}/odds"
        params: dict = {"regions": regions, "markets": markets, "oddsFormat": odds_format}
        if bookmakers:
            params["bookmakers"] = bookmakers
        result = self._request(path, params)
        return result if isinstance(result, dict) else None

    def get_all_events(self, sport_key: str, regions: str = "us",
                       markets: str = "h2h",
                       odds_format: str = "american",
                       bookmakers: str | None = None) -> list:
        """
        Fetch all events with odds — used by watchlist VegasOddsSource matching.
        """
        path   = f"{ODDS_API_BASE}/{sport_key}/odds"
        params: dict = {"regions": regions, "markets": markets, "oddsFormat": odds_format}
        if bookmakers:
            params["bookmakers"] = bookmakers
        result = self._request(path, params)
        return result if isinstance(result, list) else []

    def _request(self, url: str, params: dict) -> list | dict | None:
        """Try each available key slot in order. Returns parsed JSON or None."""
        if not self._slots:
            return None

        for slot in self._slots:
            with self._lock:
                if not slot.is_available():
                    continue

            for attempt in range(MAX_RETRY):
                try:
                    r = requests.get(
                        url,
                        params={**params, "apiKey": slot.key},
                        timeout=TIMEOUT,
                    )

                    if r.status_code == 200:
                        return r.json()

                    if r.status_code in (429, 402):
                        retry_after = float(r.headers.get("Retry-After", 60))
                        slot.park(retry_after)
                        break   # move to next slot immediately

                    if r.status_code in (401, 403):
                        slot.exhaust()
                        break

                    if r.status_code >= 500:
                        if attempt < MAX_RETRY - 1:
                            time.sleep(2 ** attempt)
                            continue
                        print(f"  ✗ [OddsAPI] key_{slot.index} got {r.status_code} after retries")
                        break

                    print(f"  ✗ [OddsAPI] key_{slot.index} got {r.status_code}: {r.text[:80]}")
                    break

                except requests.exceptions.Timeout:
                    if attempt < MAX_RETRY - 1:
                        time.sleep(1)
                    continue
                except requests.exceptions.RequestException as exc:
                    print(f"  ✗ [OddsAPI] key_{slot.index} error: {exc}")
                    break

        return None   # all slots exhausted or parked


# -- Singleton ----------------------------------------------------------------

_client: OddsApiClient | None = None

def get_odds_client() -> OddsApiClient:
    global _client
    if _client is None:
        _client = OddsApiClient()
    return _client

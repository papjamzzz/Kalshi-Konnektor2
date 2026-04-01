from __future__ import annotations

import hashlib
import io
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from pypdf import PdfReader


STATUS_KEYWORDS = (
    "available",
    "probable",
    "questionable",
    "doubtful",
    "out",
    "inactive",
    "day-to-day",
    "day to day",
    "game-time decision",
    "game time decision",
    "will play",
    "won't play",
    "won t play",
    "injury",
    "illness",
    "lower-body",
    "upper-body",
    "rest",
)


def normalize_line(value: str) -> str:
    cleaned = value.replace("\u00a0", " ")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def hash_lines(lines: list[str]) -> str:
    payload = "\n".join(lines).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass
class WatcherSnapshot:
    source: str
    fetched_from: str
    fingerprint: str
    lines: list[str]


@dataclass
class WatcherDiff:
    changed: bool
    source: str
    fetched_from: str
    added: list[str]
    removed: list[str]


class BaseInjuryWatcher:
    def __init__(self, state_path: str | Path, timeout_seconds: int = 10):
        self.state_path = Path(state_path)
        self.timeout_seconds = timeout_seconds

    def get_json_state(self) -> dict:
        if not self.state_path.exists():
            return {}
        try:
            return json.loads(self.state_path.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def save_json_state(self, payload: dict) -> None:
        self.state_path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    def load_snapshot(self, key: str) -> Optional[WatcherSnapshot]:
        state = self.get_json_state()
        data = state.get(key)
        if not data:
            return None
        return WatcherSnapshot(**data)

    def save_snapshot(self, key: str, snapshot: WatcherSnapshot) -> None:
        state = self.get_json_state()
        state[key] = asdict(snapshot)
        self.save_json_state(state)

    def build_diff(self, previous: Optional[WatcherSnapshot], current: WatcherSnapshot) -> WatcherDiff:
        if previous is None:
            return WatcherDiff(
                changed=True,
                source=current.source,
                fetched_from=current.fetched_from,
                added=current.lines,
                removed=[],
            )

        previous_set = set(previous.lines)
        current_set = set(current.lines)
        added = sorted(current_set - previous_set)
        removed = sorted(previous_set - current_set)
        return WatcherDiff(
            changed=previous.fingerprint != current.fingerprint,
            source=current.source,
            fetched_from=current.fetched_from,
            added=added,
            removed=removed,
        )

    def fetch_text(self, url: str) -> str:
        response = requests.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.text

    def fetch_bytes(self, url: str) -> bytes:
        response = requests.get(url, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response.content


class NBAInjuryWatcher(BaseInjuryWatcher):
    PAGE_URL = "https://official.nba.com/nba-injury-report-2025-26-season/"
    SNAPSHOT_KEY = "nba_injury_report"

    def latest_report_url(self) -> Optional[str]:
        try:
            html = self.fetch_text(self.PAGE_URL)
        except Exception:
            html = self.fetch_text(self.PAGE_URL.replace("https://", "http://"))
        soup = BeautifulSoup(html, "html.parser")
        links: list[str] = []

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            label = normalize_line(anchor.get_text(" ", strip=True)).lower()
            if "report" in label and "et" in label:
                links.append(urljoin(self.PAGE_URL, href))

        return links[-1] if links else None

    def extract_relevant_lines(self, pdf_bytes: bytes) -> list[str]:
        reader = PdfReader(io.BytesIO(pdf_bytes))
        lines: list[str] = []

        for page in reader.pages:
            text = page.extract_text() or ""
            for raw_line in text.splitlines():
                line = normalize_line(raw_line)
                if not line:
                    continue
                lower = line.lower()
                if any(keyword in lower for keyword in STATUS_KEYWORDS):
                    lines.append(line)

        deduped = []
        seen = set()
        for line in lines:
            if line not in seen:
                deduped.append(line)
                seen.add(line)
        return deduped

    def snapshot(self) -> Optional[WatcherSnapshot]:
        report_url = self.latest_report_url()
        if not report_url:
            return None
        try:
            pdf_bytes = self.fetch_bytes(report_url)
        except Exception:
            pdf_bytes = self.fetch_bytes(report_url)
        lines = self.extract_relevant_lines(pdf_bytes)
        return WatcherSnapshot(
            source="NBA Official Injury Report",
            fetched_from=report_url,
            fingerprint=hash_lines(lines),
            lines=lines,
        )

    def poll(self) -> Optional[WatcherDiff]:
        current = self.snapshot()
        if current is None:
            return None
        previous = self.load_snapshot(self.SNAPSHOT_KEY)
        diff = self.build_diff(previous, current)
        self.save_snapshot(self.SNAPSHOT_KEY, current)
        return diff


class NHLStatusWatcher(BaseInjuryWatcher):
    INDEX_URL = "https://www.nhl.com/news"
    SNAPSHOT_KEY = "nhl_status_report"

    def latest_status_report_url(self) -> Optional[str]:
        html = self.fetch_text(self.INDEX_URL)
        soup = BeautifulSoup(html, "html.parser")

        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if "nhl-status-report" in href:
                return urljoin(self.INDEX_URL, href)
        return None

    def extract_relevant_lines(self, html: str) -> list[str]:
        soup = BeautifulSoup(html, "html.parser")
        text_blocks: list[str] = []

        for element in soup.find_all(["h1", "h2", "p", "li"]):
            line = normalize_line(element.get_text(" ", strip=True))
            if not line:
                continue
            lower = line.lower()
            if any(keyword in lower for keyword in STATUS_KEYWORDS):
                text_blocks.append(line)

        deduped = []
        seen = set()
        for line in text_blocks:
            if line not in seen:
                deduped.append(line)
                seen.add(line)
        return deduped

    def snapshot(self) -> Optional[WatcherSnapshot]:
        article_url = self.latest_status_report_url()
        if not article_url:
            return None
        html = self.fetch_text(article_url)
        lines = self.extract_relevant_lines(html)
        return WatcherSnapshot(
            source="NHL Status Report",
            fetched_from=article_url,
            fingerprint=hash_lines(lines),
            lines=lines,
        )

    def poll(self) -> Optional[WatcherDiff]:
        current = self.snapshot()
        if current is None:
            return None
        previous = self.load_snapshot(self.SNAPSHOT_KEY)
        diff = self.build_diff(previous, current)
        self.save_snapshot(self.SNAPSHOT_KEY, current)
        return diff


class MLBProbableStarterWatcher(BaseInjuryWatcher):
    SCOREBOARD_URL = "https://site.api.espn.com/apis/site/v2/sports/baseball/mlb/scoreboard"
    SNAPSHOT_KEY = "mlb_probable_starters"

    def extract_relevant_lines(self, payload: dict) -> list[str]:
        lines: list[str] = []
        for event in payload.get("events", []):
            competitions = event.get("competitions", [])
            if not competitions:
                continue
            competition = competitions[0]
            home_team = ""
            away_team = ""
            home_pitcher = "TBD"
            away_pitcher = "TBD"

            for competitor in competition.get("competitors", []):
                team_name = normalize_line(str(competitor.get("team", {}).get("displayName", "") or ""))
                probable_name = "TBD"
                for probable in competitor.get("probables", []):
                    if probable.get("name") == "probableStartingPitcher":
                        probable_name = normalize_line(
                            str(probable.get("athlete", {}).get("displayName", "") or probable.get("displayName", "") or "TBD")
                        )
                        break

                if competitor.get("homeAway") == "home":
                    home_team = team_name
                    home_pitcher = probable_name
                elif competitor.get("homeAway") == "away":
                    away_team = team_name
                    away_pitcher = probable_name

            if not home_team or not away_team:
                continue
            lines.append(f"{away_team} at {home_team} | away SP: {away_pitcher} | home SP: {home_pitcher}")

        deduped = []
        seen = set()
        for line in lines:
            if line not in seen:
                deduped.append(line)
                seen.add(line)
        return deduped

    def snapshot(self) -> Optional[WatcherSnapshot]:
        payload = requests.get(self.SCOREBOARD_URL, timeout=self.timeout_seconds)
        payload.raise_for_status()
        data = payload.json()
        lines = self.extract_relevant_lines(data)
        return WatcherSnapshot(
            source="MLB Probable Starters",
            fetched_from=self.SCOREBOARD_URL,
            fingerprint=hash_lines(lines),
            lines=lines,
        )

    def poll(self) -> Optional[WatcherDiff]:
        current = self.snapshot()
        if current is None:
            return None
        previous = self.load_snapshot(self.SNAPSHOT_KEY)
        diff = self.build_diff(previous, current)
        self.save_snapshot(self.SNAPSHOT_KEY, current)
        return diff

# Kalshi Konnektor Roadmap

## Current sequence

1. Get `NHL` pregame auto-selection working end to end.
2. Return to `NBA` and fix market discovery plus official injury-report reliability.
3. Add `MLB` if it gives more daily action and cleaner pregame edges.
4. Layer in third-wave data that can improve speed and confidence.

## Why NHL first

- Kalshi is currently exposing live `NHL` game events in the `KXNHLGAME` series.
- The `NHL` watcher is already returning usable status-report diffs.
- That makes NHL the fastest path to a real dry-run candidate generator.

## Liquidity rule

A league is not truly live-tradable for this bot unless it has:

- discoverable Kalshi events
- quoted Kalshi markets or recent market volume
- matching sportsbook odds

Listed events without quotes are not enough.

## NBA plan after NHL

- Improve official NBA injury-report ingestion and retries.
- Keep NBA limited to regular-season and playoff games.
- Stay pregame only.
- Use official report timing plus sportsbook confirmation plus Kalshi lag.

## MLB idea queue

If NHL is working and NBA still needs discovery work, MLB is the next strong candidate because:

- there are many games
- there are day games and night games
- pitcher changes and lineup news can matter a lot
- rotation structure can create predictable information windows

## Third-wave data ideas

Only after the core league scanners work:

- trusted fantasy / betting news feeds
- injury/news sites like Rotowire or other licensed sources
- back-to-back scheduling flags
- travel/rest spots
- goalie confirmations for NHL
- probable starters / lineup changes for MLB

Important:

- Check licensing and terms before integrating third-party news/data sites.
- The official/primary sources stay the base layer.
- Extra feeds are only worth adding if they improve speed or confidence without making the bot fragile.

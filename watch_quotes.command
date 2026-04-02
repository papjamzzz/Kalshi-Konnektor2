#!/bin/bash
# Watches bot.log and fires a Mac notification when Kalshi starts quoting markets.

LOG="/Users/miahsm1/Documents/New project/Kalshi-Konnektor2/bot.log"
ALERTED=false

echo "Watching for quoted markets... (Ctrl+C to stop)"
echo ""

tail -F "$LOG" 2>/dev/null | while read -r line; do
    echo "$line"

    # Match any line where quoted_markets is > 0
    if echo "$line" | grep -qE "quoted_markets=[1-9][0-9]*"; then
        if [ "$ALERTED" = false ]; then
            ALERTED=true
            # Extract the key info for the notification
            DETAIL=$(echo "$line" | grep -oE "(MLB|NHL|NBA).*quoted_markets=[0-9]+" | head -1)
            osascript -e "display notification \"$DETAIL\" with title \"Kalshi Markets Quoted\" sound name \"Glass\""
            echo ""
            echo ">>> QUOTED MARKET DETECTED <<<"
            echo ">>> $line"
            echo ""
        fi
    fi
done

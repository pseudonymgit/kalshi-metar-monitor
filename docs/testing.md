# Testing

Base URL example:
- https://kalshi-metar-monitor.onrender.com

## Health
curl -sS https://kalshi-metar-monitor.onrender.com/

## Window fetch (ingests + returns latest)
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/window?icao=KDEN&minutes=3&source=nws"

## Latest (single)
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/latest?icao=KDEN&source=nws"

## Multi
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/multi?icaos=KDEN,KLAX,KMDW&source=nws"

## Watchlist
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/watchlist"
curl -sS -X POST "https://kalshi-metar-monitor.onrender.com/metar/watchlist" \
  -H "Content-Type: application/json" \
  -d '{"icaos":["KDEN","KLAX","KMDW"]}'

## Metrics
curl -sS "https://kalshi-metar-monitor.onrender.com/metar/metrics"

## Start polling
curl -sS -X POST "https://kalshi-metar-monitor.onrender.com/metar/start"

## Force one poll
curl -sS -X POST "https://kalshi-metar-monitor.onrender.com/metar/force-poll"

## Test alert
curl -sS -X POST "https://kalshi-metar-monitor.onrender.com/metar/test-alert"

## Debug
curl -sS "https://kalshi-metar-monitor.onrender.com/debug/version"
curl -sS "https://kalshi-metar-monitor.onrender.com/debug/state"

"""
24/7 stock scanner.
Pulls quotes + price history for a watchlist from Finnhub, scores each stock
on trend / momentum / RSI / volume, and pushes the top picks to Telegram.

Runs once per invocation — the GitHub Actions workflow calls this on a
schedule so it behaves like a 24/7 background job without needing a server.
"""

import os
import time
import requests

FINNHUB_KEY = os.environ["FINNHUB_API_KEY"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# Edit this list to whatever you want scanned. Keep it to US tickers for now —
# the Finnhub free tier covers US markets best.
WATCHLIST = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AVGO", "JPM", "V", "UNH", "XOM", "WMT", "LLY", "COST",
]

BASE_URL = "https://finnhub.io/api/v1"


def fetch_quote(symbol):
    r = requests.get(f"{BASE_URL}/quote", params={"symbol": symbol, "token": FINNHUB_KEY}, timeout=10)
    r.raise_for_status()
    return r.json()


def fetch_candles(symbol, days=120):
    now = int(time.time())
    start = now - 60 * 60 * 24 * days
    r = requests.get(
        f"{BASE_URL}/stock/candle",
        params={"symbol": symbol, "resolution": "D", "from": start, "to": now, "token": FINNHUB_KEY},
        timeout=10,
    )
    r.raise_for_status()
    return r.json()


def sma(values, period):
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return None
    gains = losses = 0
    for i in range(len(closes) - period, len(closes)):
        diff = closes[i] - closes[i - 1]
        if diff >= 0:
            gains += diff
        else:
            losses -= diff
    if losses == 0:
        return 100
    rs = (gains / period) / (losses / period)
    return 100 - (100 / (1 + rs))


def analyze(symbol):
    quote = fetch_quote(symbol)
    if quote.get("c", 0) == 0:
        return None

    candles = fetch_candles(symbol)
    score = 0
    reasons = []
    sma20 = sma50 = rsi14 = momentum = vol_trend = None

    if candles.get("s") == "ok" and len(candles.get("c", [])) > 20:
        closes = candles["c"]
        volumes = candles["v"]
        sma20 = sma(closes, 20)
        sma50 = sma(closes, 50)
        rsi14 = rsi(closes, 14)
        past = closes[-21] if len(closes) > 21 else closes[0]
        momentum = ((closes[-1] - past) / past) * 100
        vol_recent = sum(volumes[-10:]) / 10
        vol_prior = sum(volumes[-30:-10]) / 20 if len(volumes) >= 30 else None
        vol_trend = ((vol_recent - vol_prior) / vol_prior) * 100 if vol_prior else None

        if sma20 and sma50:
            if quote["c"] > sma20 > sma50:
                score += 2
                reasons.append("Above both 20/50-day averages, with the shorter above the longer — clean uptrend.")
            elif quote["c"] < sma20 < sma50:
                score -= 2
                reasons.append("Below both 20/50-day averages, shorter lagging longer — clean downtrend.")

        if rsi14 is not None:
            if rsi14 > 70:
                score -= 1
                reasons.append(f"RSI {rsi14:.0f} — overbought, pullback risk.")
            elif rsi14 < 30:
                score += 1
                reasons.append(f"RSI {rsi14:.0f} — oversold, selling may be exhausted.")

        if momentum is not None:
            if momentum > 5:
                score += 1
                reasons.append(f"Up {momentum:.1f}% over the past month.")
            elif momentum < -5:
                score -= 1
                reasons.append(f"Down {abs(momentum):.1f}% over the past month.")

        if vol_trend is not None and abs(vol_trend) > 20:
            direction = "up" if vol_trend > 0 else "down"
            reasons.append(f"Volume {direction} {abs(vol_trend):.0f}% vs prior period.")

    return {
        "symbol": symbol,
        "price": quote["c"],
        "change_pct": ((quote["c"] - quote["pc"]) / quote["pc"]) * 100 if quote.get("pc") else 0,
        "score": score,
        "reasons": reasons,
    }


def send_telegram(text):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("Telegram not configured — printing results instead.\n")
        print(text)
        return
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, data={"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)


def main():
    results = []
    for symbol in WATCHLIST:
        try:
            r = analyze(symbol)
            if r:
                results.append(r)
        except Exception as e:
            print(f"Skipping {symbol}: {e}")
        time.sleep(1)  # stay well under the free-tier rate limit

    if not results:
        send_telegram("Scan ran but returned no data. Check the API key and watchlist.")
        return

    results.sort(key=lambda r: r["score"], reverse=True)
    top = results[:3]

    lines = ["<b>Top picks this scan</b>"]
    for r in top:
        arrow = "+" if r["change_pct"] >= 0 else ""
        lines.append(f"\n<b>{r['symbol']}</b> — ${r['price']:.2f} ({arrow}{r['change_pct']:.2f}%)")
        for reason in r["reasons"]:
            lines.append(f"• {reason}")
        if not r["reasons"]:
            lines.append("• No strong signal either way right now.")

    send_telegram("\n".join(lines))


if __name__ == "__main__":
    main()

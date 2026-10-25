import os
import time
import json
import requests
from datetime import datetime, timezone

# AB TRADING CYCLE SCANNER
# Bybit Futures - 4H Bearish OB + FVG

BASE_URL = "https://api.bybit.com"

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

SCAN_INTERVAL = 300
STATE_FILE = "alerted_setups.json"

session = requests.Session()


def api_get(endpoint, params):
    response = session.get(
        BASE_URL + endpoint,
        params=params,
        timeout=20
    )
    response.raise_for_status()
    data = response.json()

    if data.get("retCode") != 0:
        raise Exception(data.get("retMsg", "Bybit API error"))

    return data["result"]


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram credentials are missing.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={"chat_id": CHAT_ID, "text": message},
        timeout=20
    )
    response.raise_for_status()


def get_symbols():
    symbols = []
    cursor = None

    while True:
        params = {
            "category": "linear",
            "limit": 1000
        }

        if cursor:
            params["cursor"] = cursor

        result = api_get("/v5/market/instruments-info", params)

        for item in result["list"]:
            if (
                item["quoteCoin"] == "USDT"
                and item["contractType"] == "LinearPerpetual"
                and item["status"] == "Trading"
            ):
                symbols.append(item["symbol"])

        cursor = result.get("nextPageCursor")

        if not cursor:
            break

    return symbols


def get_candles(symbol):
    result = api_get(
        "/v5/market/kline",
        {
            "category": "linear",
            "symbol": symbol,
            "interval": "240",
            "limit": 100
        }
    )

    candles = sorted(
        result["list"],
        key=lambda x: int(x[0])
    )

    # Exclude the currently forming 4H candle
    now_ms = int(time.time() * 1000)

    candles = [
        c for c in candles
        if int(c[0]) + 4 * 60 * 60 * 1000 <= now_ms
    ]

    return [
        {
            "time": int(c[0]),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4])
        }
        for c in candles
    ]


def get_price(symbol):
    result = api_get(
        "/v5/market/tickers",
        {
            "category": "linear",
            "symbol": symbol
        }
    )

    return float(result["list"][0]["lastPrice"])


def average(values):
    return sum(values) / len(values)


def find_bearish_setup(candles):
    if len(candles) < 40:
        return None

    # Most recently completed 4H candle
    i = len(candles) - 1
    candle = candles[i]

    # Calculate 14-candle average true range
    true_ranges = []

    for j in range(i - 14, i):
        previous_close = candles[j - 1]["close"]

        tr = max(
            candles[j]["high"] - candles[j]["low"],
            abs(candles[j]["high"] - previous_close),
            abs(candles[j]["low"] - previous_close)
        )

        true_ranges.append(tr)

    atr = average(true_ranges)

    if atr <= 0:
        return None

    body = candle["open"] - candle["close"]

    # Bearish displacement
    if candle["close"] >= candle["open"]:
        return None

    if body < 1.2 * atr:
        return None

    # Find a previous meaningful swing low
    swing_low = None

    for j in range(i - 4, i - 25, -1):
        if j < 3:
            continue

        if all(
            candles[j]["low"] < candles[k]["low"]
            for k in range(j - 3, j)
        ) and all(
            candles[j]["low"] < candles[k]["low"]
            for k in range(j + 1, j + 4)
        ):
            swing_low = candles[j]["low"]
            break

    # Require a body close below the swing low
    if swing_low is None or candle["close"] >= swing_low:
        return None

    # Find the last bullish candle before displacement
    ob_index = None

    for j in range(i - 1, max(i - 7, 0), -1):
        if candles[j]["close"] > candles[j]["open"]:
            ob_index = j
            break

    if ob_index is None:
        return None

    ob = candles[ob_index]

    # Find a bearish FVG in the same recent impulse
    fvg = None

    for j in range(max(2, i - 5), i + 1):
        first = candles[j - 2]
        third = candles[j]

        if first["low"] > third["high"]:
            fvg = {
                "high": first["low"],
                "low": third["high"]
            }
            break

    if fvg is None:
        return None

    # Check whether later candles have touched the OB
    mitigated = False

    for j in range(ob_index + 1, len(candles)):
        if candles[j]["high"] >= ob["low"]:
            mitigated = True
            break

    return {
        "time": candle["time"],
        "ob_high": ob["high"],
        "ob_low": ob["low"],
        "fvg_high": fvg["high"],
        "fvg_low": fvg["low"],
        "swing_low": swing_low,
        "body": body,
        "atr": atr,
        "mitigated": mitigated
    }


def load_alerted():
    try:
        with open(STATE_FILE, "r") as file:
            return set(json.load(file))
    except Exception:
        return set()


def save_alerted(alerted):
    with open(STATE_FILE, "w") as file:
        json.dump(list(alerted), file)


def scan():
    alerted = load_alerted()

    print("Getting Bybit Futures symbols...")
    symbols = get_symbols()

    print(f"Scanning {len(symbols)} USDT perpetual pairs.")

    for symbol in symbols:
        try:
            candles = get_candles(symbol)
            setup = find_bearish_setup(candles)

            if not setup:
                continue

            setup_id = f"{symbol}_{setup['time']}"

            if setup_id in alerted:
                continue

            price = get_price(symbol)

            status = (
                "MITIGATED"
                if setup["mitigated"]
                else "FRESH"
            )

            candle_time = datetime.fromtimestamp(
                setup["time"] / 1000,
                tz=timezone.utc
            ).strftime("%Y-%m-%d %H:%M UTC")

            message = f"""
🔔 AB TRADING CYCLE SCANNER

Pair: {symbol}
Market Price: {price}
Timeframe: 4H
Direction: BEARISH

📦 Bearish Order Block:
High: {setup['ob_high']}
Low: {setup['ob_low']}

⚡ Bearish FVG:
High: {setup['fvg_high']}
Low: {setup['fvg_low']}

📉 Displacement:
Bearish body: {setup['body']:.8g}
ATR: {setup['atr']:.8g}
Closed below swing low: {setup['swing_low']}

OB Status: {status}
Formation: {candle_time}

⚠️ ALERT ONLY — NOT A TRADE SIGNAL.
Manually verify the setup before considering any trade.
"""

            send_telegram(message)

            alerted.add(setup_id)
            save_alerted(alerted)

            print(f"Alert sent: {symbol}")

        except Exception as error:
            print(f"{symbol}: {error}")

        time.sleep(0.15)


def main():
    send_telegram(
        "🚀 AB Trading Cycle Scanner started!\n"
        "Monitoring Bybit Futures 4H bearish OB setups."
    )

    while True:
        try:
            scan()
        except Exception as error:
            print(f"Scanner error: {error}")

        print("Scan complete. Waiting 5 minutes...")
        time.sleep(SCAN_INTERVAL)


if __name__ == "__main__":
    main()

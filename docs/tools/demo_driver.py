"""Scripted driver for the README screenshots and GIF.

Feeds a local Go engine (simulated venue) a seeded random walk and a handful of
decisions, so the SwiftUI dashboard has something real to show. Nothing here
talks to a broker or uses an account.

    cd engine && go run ./cmd/engine -cash 25000     # terminal 1
    python3 docs/tools/demo_driver.py                 # terminal 2
    cd dashboard && ./run.sh                          # terminal 3

The prices are made up; the risk verdicts and fills are the engine's own.
"""
from __future__ import annotations

import json
import random
import sys
import time
import urllib.request

URL = "http://127.0.0.1:8080"


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(URL + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        return json.loads(urllib.request.urlopen(req, timeout=2).read())
    except Exception as e:  # a 4xx verdict is still a verdict; keep going
        return {"error": str(e)}


def main(seconds: float = 40.0, seed: int = 5) -> None:
    rng = random.Random(seed)
    px = {"BTC-USD": 64_000.0, "ETH-USD": 3_100.0, "SOL-USD": 145.0}

    def tick(drift: float) -> None:
        for s in px:
            px[s] *= 1 + rng.gauss(drift, 0.0009)
            post("/tick", {"symbol": s, "price": round(px[s], 2)})

    for _ in range(5):
        tick(0.0)
    plan = {8: ("BTC-USD", "BUY", 0.85), 20: ("ETH-USD", "BUY", 0.80),
            35: ("SOL-USD", "SELL", 0.75), 50: ("ETH-USD", "BUY", 0.30)}
    steps = int(seconds / 0.25)
    for i in range(steps):
        if i in plan:
            sym, side, conf = plan[i]
            print(sym, side, post("/decision", {"symbol": sym, "side": side,
                                                "confidence": conf, "size_hint": 1,
                                                "rationale": "demo"}))
        tick(0.00015 if i < steps * 0.6 else -0.0001)
        time.sleep(0.25)


if __name__ == "__main__":
    main(float(sys.argv[1]) if len(sys.argv) > 1 else 40.0)

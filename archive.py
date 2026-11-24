#!/usr/bin/env python3
"""
Weekly price archive: adds the latest 1-hour candles to data/history/<PAIR>_1h.csv.

The live bot reads this saved history together with the newest download, so the
price history it works from grows by about one week every week (instead of always
being just the latest ~4 months). Runs every Saturday (market closed).
"""
import os
import sys
import time
from datetime import datetime, timedelta, timezone

import signals as S


def run(fetch=None, now=None):
    fetch = fetch or (lambda pair, key: S.fetch_candles(pair, key, S.CANDLES))
    now = now or datetime.now(timezone.utc)
    key = os.getenv("TWELVEDATA_API_KEY", "")
    for n, pair in enumerate(S.PAIRS):
        if n:
            time.sleep(10)          # free plan: max 8 requests per minute
        old = S.load_history(pair)
        try:
            new = fetch(pair, key)
        except Exception as e:
            print(f"[{pair}] could not get prices, history unchanged: {e}")
            continue
        new = [c for c in new if c["time"] + timedelta(hours=1) <= now]    # finished candles only
        merged = S.merge_candles(old, new)
        S.save_history(pair, merged)
        first = merged[0]["time"].strftime("%d %b %Y") if merged else "-"
        print(f"[{pair}] history: {len(old)} -> {len(merged)} candles (from {first})")


if __name__ == "__main__":
    if not os.getenv("TWELVEDATA_API_KEY"):
        sys.exit("Missing TWELVEDATA_API_KEY secret.")
    run()

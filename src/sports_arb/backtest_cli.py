"""`sports-arb-backtest`: replay recordings, sweep latency/skew, print the profitability gate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from sports_arb.live_config import LiveRiskConfig
from sports_arb.replay import DataQuality, load_snapshots, run_replay, summarize, verdict
from sports_arb.strategy import StrategyConfig


def main() -> None:
    p = argparse.ArgumentParser(prog="sports-arb-backtest")
    p.add_argument("recordings", type=Path, nargs="+", help="JSONL files in time order")
    p.add_argument("--latencies", type=float, nargs="+", default=[0, 1, 2, 5])
    p.add_argument("--skews", type=float, nargs="+", default=[1000, 500, 250])
    p.add_argument("--target-latency", type=float, default=2.0)
    a = p.parse_args()
    risk, strategy = LiveRiskConfig(), StrategyConfig.load()
    risk.validate()
    universes, snaps, _res, quality = [], [], {}, DataQuality()
    for path in a.recordings:
        u, s, r, q = load_snapshots(path)
        universes += u
        snaps += s
        quality.snapshots += q.snapshots
        quality.non_monotone += q.non_monotone
        quality.invalid_books += q.invalid_books
    universes.sort(key=lambda x: x[0])
    results = [
        run_replay(universes, snaps, risk, strategy, latency_s=lat, quality=quality)
        for lat in a.latencies
    ]
    skew_runs = [
        run_replay(universes, snaps, risk, strategy, latency_s=a.target_latency, max_pair_skew_ms=s)
        for s in a.skews
    ]
    by_lat = {r.latency_s: r for r in results}
    print(
        json.dumps(
            {
                "data_quality": {s: getattr(quality, s) for s in quality.__slots__},
                "latency_sweep": summarize(results),
                "skew_sweep_at_target_latency": summarize(skew_runs),
                "GATE": verdict(by_lat, a.target_latency),
            },
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()

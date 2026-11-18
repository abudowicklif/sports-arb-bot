import json
from dataclasses import asdict
from decimal import Decimal as D

from helpers import market

from sports_arb.live_config import LiveRiskConfig
from sports_arb.replay import (
    MIN_TRADES_FOR_VERDICT,
    DataQuality,
    load_snapshots,
    phase,
    run_replay,
    verdict,
)
from sports_arb.scanner import _jsonable
from sports_arb.strategy import StrategyConfig


def raw(asset, ask, bid, ts, size="100"):
    return {
        "asset_id": asset,
        "market": "condition-1",
        "asks": [{"price": ask, "size": size}],
        "bids": [{"price": bid, "size": size}],
        "timestamp": str(int(ts * 1000)),
        "min_order_size": "1",
        "tick_size": "0.01",
    }


def write(path, m, steps):
    rows = [{"k": "universe", "ts": 1000.0, "markets": [_jsonable(asdict(m))]}]
    for ts, a0, a1, b0, b1 in steps:
        rows.append(
            {
                "k": "books",
                "ts": ts,
                "recv": ts,
                "raw": [raw("101", a0, b0, ts), raw("202", a1, b1, ts)],
            }
        )
    path.write_text("\n".join(json.dumps(r) for r in rows))


def run(path, latency, skew=None):
    u, s, _r, q = load_snapshots(path)
    return run_replay(
        u,
        s,
        LiveRiskConfig(),
        StrategyConfig(),
        latency_s=latency,
        max_pair_skew_ms=skew,
        quality=q,
    ), q


def test_consistent_books_produce_no_candidates_and_gate_is_honest(tmp_path):
    p = tmp_path / "r.jsonl"
    # uncrossed: bids 0.40/0.40, asks 0.62/0.62 -> asks sum 1.24 > 1
    write(p, market(), [(1000.0 + i, "0.62", "0.62", "0.40", "0.40") for i in range(50)])
    r, _ = run(p, 0)
    assert r.trades == []
    assert verdict({2.0: r}).startswith("INSUFFICIENT_DATA")  # never a manufactured PASS


def test_replay_uses_snapshot_clock_not_wall_clock(tmp_path):
    p = tmp_path / "r.jsonl"
    write(
        p,
        market(),
        [(1000.0, "0.44", "0.44", "0.30", "0.30"), (1001.0, "0.44", "0.44", "0.30", "0.30")],
    )
    r, _ = run(p, 0)
    assert r.candidate_status["qualifying"] >= 1  # would be 'stale_book' on the wall clock


def test_latency_sweep_phantom_edge_vanishes_with_latency(tmp_path):
    p = tmp_path / "r.jsonl"
    # market() has seconds_delay=1; edge lives for ~1.5s then disappears
    steps = [
        (1000.0, "0.44", "0.44", "0.30", "0.30"),
        (1001.0, "0.44", "0.44", "0.30", "0.30"),
        (1002.0, "0.44", "0.44", "0.30", "0.30"),
        (1003.0, "0.60", "0.60", "0.30", "0.30"),
        (1010.0, "0.60", "0.60", "0.30", "0.30"),
    ]
    write(p, market(), steps)
    fast, _ = run(p, 0)
    slow, _ = run(p, 5)
    assert fast.trades[0].status == "HEDGED" and fast.trades[0].pnl > 0
    assert slow.trades[0].status == "NO_FILL" and slow.net_pnl == 0


def test_one_leg_surviving_is_unwound_at_a_loss(tmp_path):
    p = tmp_path / "r.jsonl"
    write(
        p,
        market(),
        [(1000.0, "0.44", "0.44", "0.30", "0.30"), (1002.0, "0.44", "0.60", "0.30", "0.30")],
    )
    r, _ = run(p, 1)
    assert r.trades[0].status == "LEG_FAILED_UNWOUND" and r.trades[0].pnl < 0


def test_one_open_pair_per_market_no_depth_reuse(tmp_path):
    p = tmp_path / "r.jsonl"
    write(p, market(), [(1000.0 + i, "0.44", "0.44", "0.30", "0.30") for i in range(10)])
    r, _ = run(p, 0)
    assert len([t for t in r.trades if t.status == "HEDGED"]) == 1


def test_crossed_books_are_counted_as_data_quality_flags(tmp_path):
    p = tmp_path / "r.jsonl"
    write(p, market(), [(1000.0, "0.44", "0.44", "0.60", "0.55")])
    _, q = run(p, 0)
    assert isinstance(q, DataQuality) and q.crossed_pairs == 1


def test_non_monotone_timestamps_are_dropped_and_counted(tmp_path):
    p = tmp_path / "r.jsonl"
    write(
        p,
        market(),
        [(1002.0, "0.62", "0.62", "0.4", "0.4"), (1001.0, "0.62", "0.62", "0.4", "0.4")],
    )
    _, _, _, q = load_snapshots(p)
    assert q.non_monotone == 1 and q.snapshots == 1


def test_verdict_fail_and_provisional_paths():
    from sports_arb.replay import Result, Trade

    def trade(pnl):
        return Trade(
            1.0, "m", "polymarket", "moneyline", "pre", "HEDGED", D(pnl), D("5"), D("5"), 2.0
        )

    bad = Result(2.0, 500.0, trades=[trade("-0.01")] * MIN_TRADES_FOR_VERDICT)
    assert verdict({2.0: bad}).startswith("FAIL")
    lucky = Result(2.0, 500.0, trades=[trade("100")] + [trade("-0.01")] * 40)
    assert verdict({2.0: lucky}).startswith("FAIL: profit depends")
    good = Result(2.0, 500.0, trades=[trade("0.05")] * 40)
    assert verdict({2.0: good}).startswith("PASS_PROVISIONAL")


def test_phase_uses_real_start_time_only():
    m = market()
    assert phase(m, 1.0) == "unknown"

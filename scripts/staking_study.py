"""Monte Carlo of staking methods on SYNTHETIC binary bets (algorithm study, not market evidence).

Each bet buys at all-in cost k per $1 payoff and wins with true probability q.
Methods are forced 'approved' AND every bet is forced 'validated' here ONLY to measure the staking
math in isolation (so flat/Martingale/Paroli keep betting even at zero or negative edge; Kelly
refuses because its lower-bound edge is not positive). Live use is gated by the registry.
Run:  python scripts/staking_study.py
"""

from __future__ import annotations

import random
import statistics
import sys
from decimal import Decimal as D
from pathlib import Path

from sports_arb.quant.registry import AUTH, ValidationRegistry
from sports_arb.quant.staking import (
    METHODS,
    StakeContext,
    StakingConfig,
    StakingEngine,
)

K = D("0.50")


def run_path(method, q, bets, rng, reg):
    cfg = StakingConfig(
        method=method,
        base_stake=D("1"),
        max_stake=D("20"),
        max_fraction=0.25,
        max_steps=4,
        daily_loss_limit=D("1000"),
        cumulative_loss_limit=D("60"),
    )
    eng = StakingEngine(cfg, reg)
    bank, peak, max_dd, placed = D("100"), D("100"), D("0"), 0
    for _ in range(bets):
        if bank < D("1"):
            break
        d = eng.next_stake(
            StakeContext(
                bankroll=bank,
                price=K,
                win_prob_lower=q - 0.03,
                edge_validated=True,
                central_cap=bank,
                sample_size=500,
            )
        )
        if d.stake == 0:
            if eng.halted:
                break
            continue
        won = rng.random() < q
        pnl = d.stake * (1 - K) / K if won else -d.stake
        bank += pnl
        eng.record(won, pnl)
        placed += 1
        peak = max(peak, bank)
        max_dd = max(max_dd, (peak - bank) / peak)
    return float(bank), float(max_dd), bank < D("1"), eng.halted, placed


def main(paths=800, bets=300):
    tmp = Path("/tmp/staking_registry.json")
    tmp.unlink(missing_ok=True)
    reg = ValidationRegistry(tmp)
    for m in METHODS:
        reg.record_evidence(f"staking:{m}", {"study": True})
        reg.approve(f"staking:{m}", AUTH)
    print(f"{paths} paths x {bets} bets, start $100, cost/share {K}\n")
    for label, q in (
        ("POSITIVE edge q=0.56", 0.56),
        ("ZERO edge q=0.50", 0.50),
        ("NEGATIVE edge q=0.46", 0.46),
    ):
        print(label)
        print(
            f"{'method':24}{'mean':>9}{'median':>9}{'p5':>9}{'ruin%':>8}{'p95 DD%':>9}{'halt%':>7}"
        )
        for m in METHODS:
            rng = random.Random(42)
            res = [run_path(m, q, bets, rng, reg) for _ in range(paths)]
            finals = sorted(r[0] for r in res)
            dds = sorted(r[1] for r in res)
            print(
                f"{m:24}{statistics.mean(finals):9.2f}{finals[len(finals) // 2]:9.2f}"
                f"{finals[int(0.05 * len(finals))]:9.2f}{100 * sum(r[2] for r in res) / paths:8.1f}"
                f"{100 * dds[int(0.95 * len(dds))]:9.1f}{100 * sum(r[3] for r in res) / paths:7.1f}"
            )
        print()


if __name__ == "__main__":
    sys.exit(main())

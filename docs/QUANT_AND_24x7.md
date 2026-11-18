# Statistical arbitrage + 24/7 operation: what exists and what does not

Evidence: model tests use synthetic data to check the *algorithms* (e.g. a planted bias is found;
a perfectly calibrated market is NOT flagged). They are not evidence of any real-market edge.
See [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md) for the latest recorded test run. No
current live connection or licensed sports-data provider is established by these tests.

## Built and tested
| Area | Module | Notes |
|---|---|---|
| Scoring, calibration, Bayesian updating | `quant/stats.py` | Brier, log loss, reliability/ECE, Beta posterior, Wilson, paired bootstrap |
| Probability models | `quant/models.py` | Market baseline; logistic recalibrator shrunk toward the market; logit ensemble; volatility regime detector |
| Walk-forward validation | `quant/models.py` | Expanding window, train strictly before test, approval only if the 5th percentile of the bootstrap log-loss gain over the market baseline is > 0 and out-of-sample n >= 300 |
| Sizing | `quant/sizing.py` | Quarter-Kelly on the LOWER credible bound, 5% bankroll cap; no edge at the bound = no trade |
| Drift + suspension | `quant/drift.py` | Rolling gain over market on resolved markets; suspends; resume needs `AUTHORIZE MODEL RESUME` |
| Validation registry | `quant/registry.py` | Every strategy/model is `UNVALIDATED` by default; live use needs recorded evidence and `AUTHORIZE LIVE STRATEGY`; new evidence resets approval |
| Decision engine | `quant/decision.py` | Rejects by default; requires approved strategy, good data/execution/liquidity, positive LOWER-BOUND EV, exposure limits per event/game/venue/strategy/total, concurrency; ranks by EV per capital; one event cannot be held twice across strategies |
| Cross-venue equivalence | `quant/equivalence.py` | Only human-reviewed mappings with `settlement_equivalent = true`; titles/prices never match markets |
| Sports-data quality gate | `quant/sportsdata.py` | Freshness, source lag, >= 2 independent sources, conflict detection; contract only (no provider) |
| Strategy capital allocation | `quant/allocation.py`, `accounting.py` | Weight only approved strategies with >= 30 realized trades and a positive lower bound; remainder stays reserve |
| 24/7 supervisor | `service.py` | Restart with backoff, heartbeat staleness, unexpected exit = failure, status file with `trading_allowed`, kill switch override, alert log, external `check` watchdog |
| Rate limiting | `ratelimit.py`, `http.py` | Token bucket; optional per client |
| Deployment | `deploy/` | systemd unit, Dockerfile with healthcheck |

## Not built / blocked
- **Real data (blocks all model validation).** No recorded Polymarket/Kalshi data exists yet. Start `sports-arb-service run` and let it record; models cannot be approved before several hundred RESOLVED markets exist.
- **Sports-data provider.** None integrated: needs you to choose and license one (credentials, terms, latency). Until then in-play statistical trading must stay off.
- **Resolution outcomes feed.** Needed to evaluate calibration and drift on real markets; poller not written (venue API unverified from the sandbox).
- **Machine learning beyond logistic recalibration, time-series and correlation models.** Not implemented: nothing justifies the complexity without data. `walk_forward` is the harness any candidate must pass first.
- **Statistical-arbitrage execution.** Directional (unhedged) positions need their own order path, unwind/hold semantics, settlement-risk handling and per-strategy PnL feed. The decision engine is not yet connected to `LiveTrader`/`KalshiTrader`; the service places no orders.
- **Cross-platform comparison.** Needs a Kalshi data path (public recorder still blocked) plus mappings in a reviewed `config/market_map.toml` (none provided).
- **WebSocket market-data streams with REST fallback.** Only REST polling exists for market data; Polymarket market WS not implemented.
- **Alerts to a pager/chat and dashboards.** Alerts go to log + `alerts.jsonl`; status is `service_status.json`. No webhook or HTML dashboard.
- **Pure arbitrage remains unproven** (see IMPLEMENTATION_STATUS.md). On a consistent book its gross edge is not positive.

## Honest expectation
Prediction-market prices are often hard to beat. The harness is built to say "no" and the registry
keeps every strategy off until the evidence says otherwise. 24/7 operation means recording,
monitoring and waiting; it does not mean trading.

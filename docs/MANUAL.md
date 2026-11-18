# sports-arb-bot: System Manual and Readiness Report

Status: **NOT READY** for real-money trading. Every claim is either verified by an executed test or run, or labelled as not verified.


## 1. Executive summary

Status: NOT READY for real-money trading. The project is a Python system for two-outcome sports-market arbitrage on Polymarket and Kalshi. It has been audited, repaired and extended with a recorder, replay engine, capital ledger, decision engine, staking engine, 24/7 supervisor and dashboard. These components have local tests (114 passing in the 2026-11-12 rerun) on synthetic or fixture data only.

No genuine trading edge has been found or proven. No real Polymarket or Kalshi data, licensed sports feed, or funded-account test has been available, so strategy profitability is unknown. A structural finding matters most: on a self-consistent binary order book, buying both outcomes at the asks costs more than $1, so pure same-market arbitrage can appear only on crossed or non-simultaneous snapshots. The system is built to measure this honestly and to refuse to trade until evidence says otherwise.

- Verified: install on Python 3.13+, 114 tests in the 2026-11-12 rerun, replay/fill simulation, capital accounting, risk gating, supervisor behaviour. Ruff was not installed for that rerun; an earlier lint run is recorded below.
- Not verified: any live API call, any real market data, sports-data providers, order placement, balances, permissions.
- Blocked on you: real data collection time, a licensed sports-data provider, account credentials with limited permissions, and a funded small-scale test only after every earlier stage passes.


## 2. System architecture

Data flows from venue clients into recordings, then through the same detector, limits and fill logic used for live execution. Strategy approval is a human decision recorded in a registry; the central decision engine rejects by default.

```
venues (Polymarket CLOB/Gamma, Kalshi)  ->  recorder (JSONL, raw + timestamps)
        |                                          |
        v                                          v
  live scanner/executors  <--- same config --->  replay + fill simulator
        |                                          |
        v                                          v
  decision engine (approved strategies only) <- validation registry (human approval)
        |
        v
  staking -> risk caps -> executors -> live.db -> capital.db
        |
  supervisor (restart, heartbeat) -> status JSON, alerts -> dashboard
```

Key modules: detector, scanner, gamma/clob/kalshi_gateway, live_execution, kalshi_execution, live_ledger, strategy (shared config and entry gating), record, replay, accounting, quant/* (stats, models, sizing, staking, decision, registry, equivalence, sportsdata, matchstate, edge, drift, allocation), service, ratelimit, dashboard.


## 3. Installation and setup

```
python3.13 -m venv .venv && . .venv/bin/activate
pip install -r requirements.lock
pip install -e '.[dev]' --no-deps
pytest -q          # expect: 114 passed
ruff check src tests scripts
```

What it does: reproducible install from the lockfile. Why: unpinned transitive packages can change behaviour. Troubleshooting: on Python 3.11/3.12 the install is refused by design because the pinned Kalshi SDK needs 3.13.

Data location: all state (ledger, capital ledger, kill switch, recordings, status, alerts) lives in one directory, set with SPORTS_ARB_DATA (default: the project data folder). Tests use an isolated temp directory.


## 4. Python and dependencies

requires-python is >=3.13. Direct pins: aiohttp, py-clob-client-v2 1.2.1, kalshi-python-async 3.35.0. requirements.lock pins transitive packages. A CI workflow (.github/workflows/ci.yml) installs from the lockfile and runs lint and tests; it has not been executed on GitHub from this environment.

Upgrade procedure: change a pin, regenerate the lock (uv pip compile pyproject.toml -o requirements.lock), run the full suite, and re-run the replay regression tests before use.


## 5. Exchange API integrations

Polymarket: Gamma (discovery), CLOB (books, orders via py-clob-client-v2), data API (positions), user WebSocket. Kalshi: official async SDK for events, markets, order books and orders, plus an authenticated WebSocket stream. Execution uses fill-or-kill legs with protected limits and never retries an order submission whose outcome is unknown.

Status: code paths are covered by fixture and scripted-venue tests. NOT externally verified: connectivity, authentication, real order behaviour, fee and tick rules as currently enforced, geographic and market eligibility. Document and confirm these in Stage 1 before anything else.

- Create API credentials with the least privilege the venue offers; disable withdrawal permission where supported.
- Keep secrets in environment variables loaded from a protected file (see .env.live.example). Never paste secrets into chat, logs or code.
- Live arming is off by default: three independent confirmations are required (two environment variables per venue plus the --confirm-live flag).


## 6. Sports-data integrations

Status: BLOCKED. No licensed provider is integrated. What exists is the contract and the safety rules: SportsSignal (source, event time, receive time), SignalGate (freshness, source lag, at least two independent sources, conflict detection) and MatchTracker (six verified match states with legal-transition checks).

Required from you: choose and license a live-scores provider that covers your target sports, obtain credentials and latency figures, and write a thin adapter that emits SportsSignal records plus a status_map from the provider's status strings to MatchState. Until then in-play statistical trading must stay off.


## 7. Live-market discovery

Polymarket and Kalshi discovery pull active sports markets. The recorder re-runs discovery every five minutes and stores the whole universe each time, including fee schedule, tick size, minimum size, delay and start time as of capture, so replay uses the universe that actually existed. The six match states are UPCOMING, STARTED, IN_PROGRESS, SUSPENDED, COMPLETED awaiting settlement, and RESOLVED. A match counts as live-action only when two fresh sources agree it is IN_PROGRESS.

Not built: Kalshi public (unauthenticated) recording, a resolution-outcome poller, and market-data WebSockets with REST fallback (REST polling only). Kalshi markets currently carry no real start time.


## 8. Pure arbitrage

Implemented: the same-market detector (buy both outcomes when asks plus fees are below $1), with depth walking, fees, slippage reserve, staleness and skew checks, and capped-edge gating at protected limits. A regression test pins the structural finding: across 2,000 random uncrossed Kalshi books the gross edge is always negative.

Not implemented: a cross-platform pure-arbitrage detector. It would require reviewed event/outcome/settlement mappings (config/market_map.toml format is supported by quant/equivalence.py) and a Kalshi data path. The decision engine accepts pure_arb candidates but nothing yet generates cross-venue candidates.


## 9. Statistical arbitrage

Implemented building blocks: market baseline, logistic recalibrator shrunk toward the market, logit ensemble, volatility regime detector, walk-forward validation against the market baseline, Brier/log-loss/reliability scoring, Beta-Bayesian updating, drift monitor with automatic suspension.

Validated behaviour (synthetic): a planted bias is found and approved; a perfectly calibrated market is not approved (four seeds); thin data is refused; the walk-forward never trains on future data. No real-market result exists. Machine-learning models beyond recalibration, time-series and correlation models are not built because nothing justifies them without data; any candidate must pass walk_forward first.


## 10. Hybrid strategy

A hybrid candidate is accepted by the decision engine only if the hybrid strategy itself is APPROVED_FOR_LIVE, which requires its own recorded evidence. Double counting is prevented structurally: exposure is tracked per event, per game, per sport, per venue and per strategy, so the same real-world outcome cannot be held twice across engines. No hybrid logic beyond that gating exists yet.


## 11. Probability modelling

How it works: the market price is the null model. A model is useful only if the 5th percentile of the paired-bootstrap log-loss improvement over the market, measured strictly out of sample across at least 300 points, is positive. Position sizes use the lower credible bound of the win probability, never the point estimate, so a high predicted probability alone never authorizes a trade.

Configure: min_train, test_size and min_oos arguments of walk_forward. Test: tests/test_quant_core.py (13 tests). Troubleshoot: 'insufficient out-of-sample data' means record and resolve more markets.


## 12. Martingale and alternative staking systems

Implemented methods: flat, Kelly, adaptive (Kelly scaled by sample size), classic, capped and fractional Martingale, and Paroli (press winners). Rules enforced in code: no stake without a validated positive lower-bound edge; progression methods need registry approval (staking:<method>); the central risk cap always wins; progression and loss limits halt staking until a human reset; loss limits use net PnL.

Classic Martingale raises stakes after losses, which conflicts with the rule never to increase stakes merely because previous trades lost, so it is research-only and unapproved by default. Increasing after wins is implemented separately as Paroli.

Monte Carlo study (synthetic binary bets at $0.50 cost, 800 paths x 300 bets, start $100, every method forced to keep betting so the staking math is isolated):

```
800 paths x 300 bets, start $100, cost/share 0.50

POSITIVE edge q=0.56
method                       mean   median       p5   ruin%  p95 DD%  halt%
flat                       135.41   136.00   108.00     0.0     17.5    0.0
kelly                      170.09   165.91   109.01     0.0     26.5    0.0
adaptive                   170.09   165.91   109.01     0.0     26.5    0.0
martingale                 125.56   109.00    72.00     0.0     30.1   95.0
capped_martingale          125.56   109.00    72.00     0.0     30.1   95.0
fractional_martingale      116.81   107.88    87.81     0.0     13.6   95.0
paroli                     202.54   200.00    87.00     0.0     53.5    1.8

ZERO edge q=0.50
method                       mean   median       p5   ruin%  p95 DD%  halt%
flat                        99.29   100.00    72.00     0.0     37.3    0.0
kelly                      100.00   100.00   100.00     0.0      0.0    0.0
adaptive                   100.00   100.00   100.00     0.0      0.0    0.0
martingale                 101.10    90.00    70.00     0.0     30.7   99.0
capped_martingale          101.10    90.00    70.00     0.0     30.7   99.0
fractional_martingale      100.56    95.81    86.81     0.0     14.9   99.0
paroli                      99.72    91.00    40.00     0.0     66.9   27.2

NEGATIVE edge q=0.46
method                       mean   median       p5   ruin%  p95 DD%  halt%
flat                        75.35    76.00    48.00     0.0     56.0    3.4
kelly                      100.00   100.00   100.00     0.0      0.0    0.0
adaptive                   100.00   100.00   100.00     0.0      0.0    0.0
martingale                  90.39    84.00    70.00     0.0     31.0   99.9
capped_martingale           90.39    84.00    70.00     0.0     31.0   99.9
fractional_martingale       94.22    91.81    85.50     0.0     16.5   99.9
paroli                      59.53    40.00    40.00     0.0     69.0   63.2
```

- At zero edge no method makes money (differences are within sampling noise).
- At negative edge, Martingale variants look better than flat only because they halt early (about 99% of paths stop); stopping, not staking, limits the loss.
- At positive edge fractional Kelly beat flat and Martingale with lower drawdown than Martingale; Paroli had the highest mean but the deepest drawdowns (p95 above 50%).
- Only Kelly refuses to bet when the lower-bound edge is not positive. No staking system changes expected value; none is enabled by default.


## 13. Edge discovery

Three pre-registered hypotheses exist as data (slow reaction after a score, overreaction after a red card, favourite-longshot bias), each with a statement, rationale, required data and predefined validation requirements. event_study computes net-of-cost gain per event after your execution delay with a bootstrap interval and is significant only if at least 200 events exist and the 5th percentile is positive. Verified on synthetic data: planted drift detected, noise not detected.

Not done: testing any hypothesis on real data. That needs recorded books around timestamped game events from a licensed feed.


## 14. Historical data recording

```
sports-arb-record --out data/recordings --interval 2 --max-markets 250
sports-arb-service run          # recorder under the supervisor
```

Stores raw /books responses with request-sent and response-received times, plus universe snapshots, one JSONL file per UTC day. Not run against the live API from this environment. Storage per day has not been measured yet; check file sizes after the first day, then compress and rotate. Resolution outcomes are not recorded yet.


## 15. Backtesting and paper trading

```
sports-arb-backtest data/recordings/polymarket-2026-11-12.jsonl --latencies 0 1 2 5 --skews 1000 500 250
```

The replay never reads the wall clock, fills against the first snapshot at or after decision time plus latency plus the venue delay, treats legs independently, simulates unwinds, allows one open pair per market, mirrors the live daily limits and uses the same protected-limit gating as live execution. Output separates pre-game, live and unknown phases and is labelled SIMULATION_NOT_REALIZED_RESULTS.

The profitability gate returns INSUFFICIENT_DATA, FAIL or PASS_PROVISIONAL; it never returns 'proven'. Paper trading (Stage 3) is the same replay run on freshly recorded live data; no paper run has been performed.


## 16. Live execution

Polymarket and Kalshi executors place both legs as fill-or-kill orders with protected limits, check balances, open orders and stream health first, record every submission and fill in a SQLite ledger, and halt on unknown states. Daily notional, in-flight cost and daily realized-loss limits are enforced from the ledger. Shared strategy numbers and entry gating live in one module used by both executors and the replay.

Shipped defaults are conservative (maximum pair cost $10, daily notional $20, maximum 5 shares, daily realized loss limit $1). Live execution has never been run against real venues from this environment.


## 17. Failed-order and unwind procedures

Verified by nine scripted-venue scenarios: both legs fill (HEDGED_OPEN); neither fills (NO_FILL, reservation released); one leg fills then unwinds flat with a loss; a failed unwind writes the kill switch (EXPOSED_HALTED); an unknown submission is never retried and requests market-level cancellation; an unhealthy stream blocks before any order; the daily notional and loss limits block further rounds; the ledger survives a restart.

Recovery: after any halt, inspect the ledger and venue positions, resolve exposure manually, remove the kill file only after reconciliation. Not built: automatic reconciliation against venue balances and positions beyond the pre-trade checks.


## 18. Capital management

Implemented: operating capital (deposits plus reinvested profit, impaired by deficits), per-trade ceiling as a fraction of operating capital, daily and weekly realized-loss limits, drawdown suspension, and decision-engine exposure limits per event, game, sport, venue, strategy, total and concurrent positions. Not wired yet: the capital account does not automatically feed the live executors' per-trade cap, so those still use their own hard ceilings.


## 19. Cumulative profit and reinvestment

```
sports-arb-capital deposit 100 seed
sports-arb-capital recognize <round-id> <net-pnl>
sports-arb-capital allocate --liabilities 0
sports-arb-capital set-policy --reinvest 50 --withdraw 20 --reserve 30 --max-drawdown 10 --max-trade-fraction 0.25 --authorize 'AUTHORIZE POLICY CHANGE'
```

Rules: only realized net PnL is recognized, once per reference (duplicates ignored); losses reduce undistributed profit before anything is distributed; liabilities are held back; allocation is blocked while suspended; policy changes need the authorization phrase and are audited; rounding never leaks cents (reserve takes the remainder). Defaults: 60/25/15, adjustable. Known gap: live HEDGED_OPEN rounds do not yet record expected PnL, and merge/redeem is not implemented, so winning Polymarket rounds are not automatically recognized.


## 20. Risk controls

| Control | Where | Status |
|---|---|---|
| Live disabled by default; three confirmations | live_config, .env.live.example | Verified |
| Kill switch file | live_config, executors, supervisor | Verified (path now anchored) |
| Daily notional / in-flight / realized-loss limits | live_ledger | Verified |
| Daily and weekly loss limits (capital account) | accounting | Verified; not wired to executors |
| Drawdown suspension | accounting | Verified |
| Exposure per event/game/sport/venue/strategy | decision engine | Verified; engine not wired to live executors |
| Model drift suspension | quant/drift | Verified |
| Strategy approval gate | registry | Verified |
| Duplicate-order prevention | executors (unknown submission never retried) | Partially verified |
| Exchange-to-ledger reconciliation | pre-trade balance/orders only | NOT complete |


## 21. Security

- Credentials come from environment variables; none are in source. Never share them in chat or logs.
- The env template no longer arms live trading; arming lines are commented out.
- Rate limiter available for API clients (token bucket).
- Use least-privilege keys; disable withdrawals where the venue allows; verify eligibility and terms for your jurisdiction before trading (not verified here).
- Residual: no secrets-manager integration; no log-redaction test; no audit of dependency vulnerabilities.


## 22. Dashboard and monitoring

```
sports-arb-dashboard --out data/dashboard.html   # static page, auto-refresh 10 s
sports-arb-service check                           # exit 0 only if healthy
```

Shows health, capital and allocation, realized performance by strategy, validation status, execution rounds and recent alerts, all from recorded state. Panels that need missing components show NOT CONNECTED rather than invented data: sports-data connectivity, live sports monitored, opportunities detected or rejected, paper trading, unrealized PnL and open positions. Alerts go to the log and data/alerts.jsonl; no pager or chat delivery is configured.


## 23. 24/7 deployment

```
# systemd (see deploy/sports-arb.service)
sudo systemctl enable --now sports-arb
# docker (see deploy/Dockerfile; includes a healthcheck)
docker build -f deploy/Dockerfile -t sports-arb . && docker run -v sports-arb-data:/data sports-arb
```

The supervisor restarts crashed components with exponential backoff, treats an unexpected exit or a silent heartbeat as a failure, and writes a status file whose trading_allowed flag is false whenever a critical component is unhealthy or the kill switch exists. The service records and monitors only; it places no orders. Continuous operation never implies continuous trading.


## 24. Troubleshooting and recovery

| Symptom | Likely cause | Action |
|---|---|---|
| Install fails on 3.12 | Kalshi SDK needs Python 3.13 | Use python3.13 |
| Backtest shows INSUFFICIENT_DATA | Fewer than 30 simulated entries | Record more data; this may be the true answer |
| Replay shows stale_book | Snapshot clock not passed | Use sports-arb-backtest, not the wall clock |
| Service status HALTED | Critical component down | Read data/alerts.jsonl and service_status.json |
| Trading refused: KILL file | Kill switch exists | Reconcile positions, then remove the file deliberately |
| Staking returns 0 | No validated edge or method unapproved | Check decision reason and registry status |
| Different ledger/kill file than expected | SPORTS_ARB_DATA differs | Print the path; set the variable consistently |


## 25. Test results and performance evidence

Recorded 2026-11-12: pytest = 114 passed on Python 3.14.6; `pip check` clean; both live CLI help commands run on Windows. Ruff was not installed for this rerun. Earlier Ruff/Python 3.13.14 results are historical; see [VALIDATION.md](VALIDATION.md). Counts by file:

```
tests/test_accounting.py: 10
tests/test_book.py: 2
tests/test_decision_and_gates.py: 9
tests/test_detector.py: 9
tests/test_execution_scenarios.py: 9
tests/test_gamma.py: 1
tests/test_kalshi.py: 8
tests/test_ledger_migration_and_structure.py: 2
tests/test_live_config.py: 3
tests/test_live_cli_lock.py: 1
tests/test_live_execution_math.py: 3
tests/test_live_ledger.py: 2
tests/test_live_models.py: 2
tests/test_models.py: 6
tests/test_multi.py: 2
tests/test_quant_core.py: 13
tests/test_replay.py: 9
tests/test_service.py: 7
tests/test_staking_matchstate_edge_dashboard.py: 13
tests/test_stream.py: 3
```

Performance evidence: none from real markets. Synthetic results (replay fixtures, staking Monte Carlo, planted-bias models) validate code behaviour only. Components with no test: live WebSocket reconnection against a real server, HTTP retry behaviour, database corruption handling, Kalshi trader scenarios, the recorder against live APIs.


## 26. Known limitations and residual risks

| Risk | Status | Mitigation / needed |
|---|---|---|
| Pure arbitrage may have no real edge | OPEN | Gate on recorded real data |
| No real historical data | BLOCKED | Run recorder for days to weeks |
| No sports-data provider | BLOCKED | License and integrate a provider |
| Kalshi public recorder and resolution poller | BLOCKED | Verify endpoints with network access |
| Winning Polymarket rounds not recognized; no merge/redeem | OPEN | Implement after wallet type confirmed |
| Order precision and minimum notional unchecked | OPEN | Venue-specific quantization and checks |
| Decision engine not wired to executors | OPEN | Integrate after validation |
| Statistical-arb (unhedged) execution path absent | OPEN | Design with unwind/settlement handling |
| Exchange-to-ledger reconciliation incomplete | OPEN | Add periodic balance/position reconciliation |
| Database corruption, network-outage chaos tests absent | OPEN | Add fault-injection tests |
| Dependency vulnerability scan not run | OPEN | Add to CI |
| Market/jurisdiction eligibility unverified | BLOCKED | Confirm terms with each venue |

This register is not exhaustive and no claim is made that every risk is eliminated.


## 27. Controlled live-account activation instructions

Current status: NOT READY. Do not activate live trading and do not deposit funds. Proceed stage by stage; each stage must pass before the next.

- Stage 1 (technical): install from the lockfile, run the full suite, then verify API connectivity, discovery and book parsing with read-only access.
- Stage 2 (read-only live): run sports-arb-service for days; confirm the recorder and supervisor stay healthy; review data quality counters.
- Stage 3 (paper): run the backtest and gate on recorded data at latencies 0, 1, 2 and 5 s; record evidence in the validation registry only if the gate and walk-forward support it.
- Stage 4 (controlled live): only after explicit approval. Use the shipped hard ceilings, one venue, minimum size, and an amount you can afford to lose entirely. Approve a strategy with the registry authorization phrase. Confirm the kill switch works first.
- Stage 5 (evaluate): compare real fills with simulated fills; do not scale up after a short profitable run.

Recommended initial test capital: withheld. It cannot be justified until Stages 1 to 3 produce evidence. Automatic stop conditions that exist today: kill switch, unhealthy stream, daily notional limit, daily realized-loss limit, failed unwind, stale or crossed books, and supervisor-reported failure.


## Appendix A. Final deployment-readiness report

| Item | Content |
|---|---|
| Missing at start | No backtest, recorder or data source; install broken on 3.12; Kalshi stream dropped events; arming enabled by template; path-dependent kill switch/ledger; no strategy-validity test; PnL unrecorded for winning rounds. |
| Implemented | Recorder, replay, fill simulator, gate, capital ledger and reinvestment, decision engine, validation registry, equivalence registry, sports-data gate, match tracker, probability and sizing tools, staking engine, edge tools, supervisor, rate limiter, dashboard, deployment files, lockfile, CI, license. |
| Repaired | Python requirement, Kalshi stream queue and handshake sequence tracking, env template, anchored paths, NO_FILL reservation release, Kalshi start-time mislabel and classification fallback, shared strategy config, ledger schema (venue, fee amount) with migration. |
| Tests passed | 114 of 114 in the 2026-11-12 rerun. |
| Tests failed | None in the 2026-11-12 run. Defects found by earlier tests were fixed during development (loss limits on gross instead of net PnL; sport exposure not recorded; match status comparison before mapping). |
| Not externally verified | Every venue API, order placement, balances, permissions, real data, sports feeds, GitHub CI run. |
| Remaining risks | See section 26. |
| External services and costs | A licensed sports-data provider (cost varies by vendor), hosting for 24/7 operation, storage for recordings. Venue trading and withdrawal fees apply and are not verified here. |
| Account permissions | Trade and read-only keys with withdrawals disabled where supported; eligibility to trade in your jurisdiction. |
| Recommended test capital | Withheld until stages 1 to 3 pass. |
| Overall | NOT READY. BLOCKED on data and sports feed. WARNING: pure arbitrage may have no edge. |

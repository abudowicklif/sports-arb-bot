# Implementation status (audit checklist)

Latest local rerun (2026-11-12): 114 tests passed on Python 3.14.6, the modified live CLI and
coordinator both start on Windows, and `pip check` is clean. Ruff was not installed in the
configured environment and was not rerun. No authenticated account or live-order checks were
performed. Earlier fixture-only checks and their limitations are documented below. Everything
marked "verified" concerns local behavior; synthetic backtest numbers say nothing about
profitability.

| # | Item | Status |
|---|------|--------|
| 1 | Python version | FIXED AND VERIFIED. `requires-python >=3.13`; 3.12 install now fails with a clear message. |
| 2 | Recorder / public Kalshi | Polymarket recorder (`sports-arb-record`): implemented, **not run against the live API**. Kalshi public recorder + resolution poller: BLOCKED (cannot reach venue to confirm unauthenticated endpoints/shape). |
| 3 | Wall clock | FIXED AND VERIFIED (`replay.py` never reads the clock; test shows default clock would give `stale_book`). `Opportunity.snapshot_ts` added. |
| 4 | Profitability gate | Gate FIXED AND VERIFIED on synthetic data (returns `INSUFFICIENT_DATA`/`FAIL`/`PASS_PROVISIONAL`, never "proven"). Structural finding verified for Kalshi books (test). Actual profitability: BLOCKED, needs recorded real data. |
| 5 | Replay + fill sim | Engine FIXED AND VERIFIED on synthetic data (latency, independent legs, unwind, one open pair per market, risk gate). Validation on real data: BLOCKED. |
| 6 | Phantom arb / skew | Skew sweep implemented. Kalshi skew not testable (one REST response, no venue timestamp): NOT APPLICABLE, documented. |
| 7 | Survivorship | Universe recorded per tick and replayed as-of: implemented, synthetic-tested. Resolution outcomes: BLOCKED (poller not written, API unverified). |
| 8 | Historical fees | Fee schedule/tick/delay stored per universe row and used by replay. Only synthetic-tested. |
| 9 | Latency / sports delay | FIXED AND VERIFIED (sweep 0/1/2/5 s + venue `seconds_delay`). |
| 10 | Consistency | FIXED AND VERIFIED for Polymarket and Kalshi code paths: shared `StrategyConfig`, `detector_config`, `cap_plan` (live + replay). Kalshi trader has no scenario tests yet. |
| 11 | PnL / merge / lock-up | Capital ledger FIXED AND VERIFIED (`accounting.py`). **Not done:** live `HEDGED_OPEN` rounds still record NULL PnL; merge/redeem not implemented (BLOCKED: needs wallet type + funded verification); capital account is not yet fed automatically from live rounds. |
| 12 | Phases | `pre/live/unknown` implemented. Kalshi has no start-time source: BLOCKED. Mislabelled expiration-as-start fixed. |
| 13 | Order precision | **NOT DONE** (float casts, share quantization, min notional). |
| 14 | Kalshi WS | FIXED AND VERIFIED (3 regression tests). |
| 15 | Env defaults | FIXED AND VERIFIED (arming lines commented out in the template). |
| 16 | Paths | FIXED AND VERIFIED (`paths.py`, `SPORTS_ARB_DATA`; tests isolated via `conftest.py`). |
| 17 | Minor | Fixed: NO_FILL releases daily reservation (tested), unknown Kalshi classification, start-time mislabel. Not done: Windows `fcntl`, `received_at` timing, fee-rate-bps formula check (needs venue docs). |
| 18 | Tests | 9 Polymarket execution scenarios + stream + accounting + replay + migration. Missing: Kalshi trader scenarios, DB-failure injection, HTTP retry. |
| 19 | Packaging | `requirements.lock`, CI workflow (not run), `LICENSE` (holder name is a placeholder), `config/strategy.toml`. |
| 20 | Data quality / ledger | Crossed/non-monotone/invalid counters; ledger schema v2 (venue, fee_amount) with tested migration. No HTML dashboard; `fills.round_id` not added. |

## Production readiness: NOT READY
Critical blockers: no real recorded data, profitability unproven, recorder untested against
live APIs, no merge/redeem, live PnL for winning rounds unrecorded, order precision unverified,
no funded verification. Do not fund live trading.

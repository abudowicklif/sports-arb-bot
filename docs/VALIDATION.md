# Validation record

## Current local rerun (2026-11-12)

- Python 3.14.6: `pytest -q` completed successfully; 114 tests collected and passed.
- `pip check`: no broken requirements found.
- `sports_arb.live_cli --help` and `sports_arb.multi_cli --help` both ran on
  Windows after correcting the process-lock import.
- Ruff was not installed in the selected environment, so lint was not rerun.
- No authenticated account, current market API, live order, or real fill was
  tested. This is not a deposit-readiness or live-trading certification.

The following is a historical record from **2026-11-10**. It distinguishes
local/public checks from funded-account verification and is not a live-fill or
profitability claim.

## Completed checks

From the repository root:

```bash
PYTHONPATH=src .venv/bin/python -m pytest
# 38 passed

.venv/bin/ruff check src tests
# All checks passed!

.venv/bin/python -m pip check
# No broken requirements found.
```

A separate clean virtual environment was created outside the project. Editable
installation with development dependencies, dependency consistency, CLI import,
and Kalshi WebSocket timeout construction succeeded:

```text
kalshi-python-async: 3.35.0
sports-arb-multi --help: OK
KalshiWebSocketStream import: OK
aiohttp.ClientWSTimeout(ws_close=5): OK
pip check: No broken requirements found.
```

The deterministic tests cover existing Polymarket models/scanner/live logic and
the new Kalshi/coordinator behavior, including:

- bid-only Kalshi books to complementary YES/NO ladders;
- V2 YES/NO buy/sell direction and price conversion;
- reduce-only unwind request flags and whole-contract rejection of fractions;
- order status, matched size, actual fill action, and actual fee normalization;
- six-decimal upward fee rounding and whole-contract detector sizing;
- atomic shared daily/in-flight risk reservation rejection;
- flat Kalshi PnL as post-round venue value minus pre-round baseline;
- rejection of group/other-readable Kalshi private-key files;
- actual concurrent startup of both venue workers;
- kill-file creation before credential validation.

Tests use fixed local responses and do not submit orders.

## Public production market-data smoke check

The official Kalshi async SDK successfully queried public production sports
events, loaded series/event fee information, and fetched an active order book.
No valid account credential was used and no authenticated order request was made.

Observed discovery included:

```text
KXWCCAREERGOALS-KMBAPPE-30  props  fee rate 0.07
KXWCCAREERGOALS-KMBAPPE-35  props  fee rate 0.07
KXWCCAREERGOALS-KMBAPPE-40  props  fee rate 0.07
```

For `KXWCCAREERGOALS-KMBAPPE-30`, normalized output included:

```text
YES: best bid 0.7900 / 1001 contracts; best ask 0.8600 / 151 contracts
NO:  best bid 0.1400 / 151 contracts;  best ask 0.2100 / 1001 contracts
tick: 0.0100
```

The complements are internally consistent: YES ask `1 - 0.14 = 0.86` and NO ask
`1 - 0.79 = 0.21`. These observations are historical smoke-test data, not a
current quote or trading recommendation.

Earlier Polymarket validation used the official V2 SDK and throwaway private key
`0x...01` to create a valid signed order against active Gamma market `6911434`.
The signed order was **not posted**.

## Not verified

No funded Polymarket or Kalshi credentials were available in the workspace.
Therefore the following remain unverified:

- authenticated Kalshi balance/order/position responses and WebSocket session;
- real order acceptance on either venue;
- real full, partial, or unequal fills;
- real cancellation races and sports-delay behavior;
- real unwind executions and residual-exposure recovery;
- live flat-position reconciliation and realized cash PnL;
- production latency, fill quality, capacity, and profitability.

No real order IDs, fills, or PnL are claimed. Complete the minimum-practical-size
checklist in [LIVE_TRADING.md](LIVE_TRADING.md) with actual venue records before
describing either adapter as live-fill verified. Never place a real order merely
to make this validation record look complete; a qualifying arbitrage may not
exist at the minimum size.

# Polymarket + Kalshi Sports Arbitrage System

Real-order adapters for **independent same-market, two-outcome sports arbitrage
on Polymarket and Kalshi**, with a concurrent coordinator and shared risk ledger.
The repository is **not ready for deposits or live trading**: funded-account
execution, real historical validation, a licensed in-play sports-data provider,
and several execution/recovery cases remain unverified or incomplete.

Three executables are installed:

| Command | Purpose |
|---|---|
| `sports-arb` | Read-only Polymarket research scanner; never submits orders |
| `sports-arb-live` | Polymarket-only authenticated production execution |
| `sports-arb-multi` | **Concurrent Polymarket + Kalshi production execution, auth checks, reconciliation, and emergency stop** |

**[Complete live setup and operating instructions](docs/LIVE_TRADING.md)** ·
**[Exact validation record](docs/VALIDATION.md)**

## Verification status — not deposit-ready

- The latest audit and its evidence are recorded in
  [docs/IMPLEMENTATION_STATUS.md](docs/IMPLEMENTATION_STATUS.md) and
  [docs/VALIDATION.md](docs/VALIDATION.md). They cover synthetic/fixture behavior,
  not real-money execution.
- Polymarket EIP-712 order signing was validated against an active market with a
  throwaway signer. No order was posted.
- Kalshi public production discovery, event/series fee retrieval, and order-book
  conversion were smoke-tested against active sports markets.
- Kalshi signed REST construction uses the official async SDK and RSA/Ed25519
  authentication. Its authenticated account and WebSocket paths still require a
  funded account for end-to-end verification.
- **No real Polymarket or Kalshi order, fill, or profit is claimed from this
  workspace.** No funded credentials were available. The system must not be
  described as live-fill verified until real minimum-size orders and venue
  records confirm both adapters.
- Do not fund an account or enable live order placement based on this code or
  its synthetic tests. The remaining blockers must first be resolved and
  verified; even then, technical readiness would not establish profitability.

The production paths contain no paper-fill or simulated-execution branch.
Conversely, passing unit tests does not prove that a venue will accept, fill,
cancel, or correctly unwind a real order.

## Production capabilities

### Polymarket

- Official Gamma discovery, CLOB V2 REST books, and `py-clob-client-v2==1.2.1`.
- Real signed FOK entry orders and FAK unwinds with protected limits.
- Authenticated user WebSocket, five-second order heartbeats, and REST recovery.
- Dynamic per-market fees, V1/V2 outcome IDs, delayed-sports monitoring,
  cancellation, Data API positions, and estimated-fee cash accounting.

### Kalshi

- Official `kalshi-python-async==3.35.0`, production V2 REST, RSA-PSS/SHA-256 or
  Ed25519 authentication, and authenticated production WebSockets.
- Event overrides and series fee schedules; unsupported flat schedules are
  skipped rather than treated as free or priced with a generic formula.
- Bid-only book conversion into YES and NO ladders, six-decimal upward fee
  rounding, balance-rounding reserve, whole-contract sizing, and uniform-tick
  validation.
- Real V2 FOK batches and IOC reduce-only unwinds. Batch legs are independently
  reconciled because a batch is **not an atomic hedge**.
- Actual fill actions and `fee_cost`, order/fill pagination, balances,
  cancellation, and net-position reconciliation.
- Realized PnL only from the post-round minus pre-round venue-reported
  `realized_pnl_dollars`, and only when flat with complete fill evidence.

### Shared coordination and capital protection

- Both venue workers run concurrently in one locked process.
- One SQLite reservation gate atomically enforces shared daily committed cost,
  in-flight cost, and daily realized-loss limits before either venue submits.
- Independent arming variables for each venue plus the CLI phrase `REAL FUNDS`.
- Very low defaults, hard-coded safety ceilings, fresh-book revalidation,
  executable-depth checks, and protected price limits.
- Unequal fills trigger capped real unwinds. Residual exposure, missing evidence,
  stream failure, or execution errors activate `data/KILL_SWITCH`.
- A worker failure cancels its sibling and requests real account-wide
  cancellation on both venues.
- Append-only orders, fills, states, WebSocket events, errors, and round results.

The coordinator does **not** guess cross-venue contract equivalence. Polymarket
and Kalshi each trade their own complementary YES/NO market. Similarly titled
contracts on different venues are not assumed to have identical settlement rules.

## Installation

Requires Python 3.13+ (enforced by `pyproject.toml`):

```bash
cd sports-arb-bot
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check src tests
```

Read-only Polymarket scan, with no credentials:

```bash
sports-arb --max-markets 50 --top 10
```

Concurrent authenticated account check, with no orders:

```bash
# After creating and loading the protected environment described in the docs.
sports-arb-multi auth-check
```

One concurrent real-trading round per venue, only if each finds a qualifying
opportunity:

```bash
sports-arb-multi trade \
  --confirm-live 'REAL FUNDS' \
  --max-shares 5 \
  --max-pair-cost 10 \
  --max-daily-notional 20 \
  --max-in-flight-cost 20 \
  --max-daily-loss 1 \
  --max-unwind-loss 1
```

No qualifying candidate means no order. Do not interpret a quiet scan as a
malfunction or relax cost controls merely to force a fill.

Emergency stop and reconciliation:

```bash
sports-arb-multi kill
sports-arb-multi reconcile
```

## Market coverage and economics

Default live discovery includes moneylines, spreads, totals, futures, and props.
Only active binary markets passing the venue-specific lifecycle, fee, tick, and
book checks are eligible.

For complementary outcome ask prices `p0` and `p1`, the initial theoretical
relationship is:

```text
gross edge per pair = 1 - p0 - p1
net estimate = assumed payout
               - both depth-walked notionals
               - venue-specific fees and rounding reserves
               - slippage/settlement reserves
```

That estimate is not a realized return. Non-atomic legs can fill unequally,
displayed liquidity can disappear, unwinds can lose money, and capital can remain
locked until resolution or an explicit merge/redeem operation. Kalshi nets YES
and NO exposure in one market position; Polymarket uses separate outcome assets.
Their accounting paths deliberately differ.

## Repository layout

```text
src/sports_arb/
  gamma.py / clob.py             Polymarket public market data
  live_gateway.py               Polymarket signing and account gateway
  live_execution.py             Polymarket real execution state machine
  user_stream.py                Polymarket authenticated WebSocket
  kalshi_config.py              Kalshi credentials and arming
  kalshi_gateway.py             Kalshi SDK, V2 orders, books, fills, positions
  kalshi_stream.py              Kalshi authenticated WebSocket monitoring
  kalshi_execution.py           Kalshi real execution state machine
  multi_cli.py                  Concurrent coordinator and emergency controls
  live_ledger.py                Atomic shared risk reservations and evidence
  live_config.py                Shared risk limits and Polymarket arming
  models.py / book.py           Decimal markets, fees, and books
  detector.py / scanner.py      Fee/depth-aware evaluation and research scan
tests/                         Deterministic tests; no order submission
```

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for data flow and failure
semantics, and [docs/LIVE_TRADING.md](docs/LIVE_TRADING.md) for exact credential,
startup, verification, and emergency procedures.

## Official references

- Polymarket: [CLOB V2 migration](https://docs.polymarket.com/v2-migration),
  [order placement](https://docs.polymarket.com/trading/place-orders),
  [fees](https://docs.polymarket.com/trading/fees), and
  [user WebSocket](https://docs.polymarket.com/developers/CLOB/websocket/user-channel).
- Kalshi: [production environments](https://docs.kalshi.com/getting_started/api_environments),
  [V2 order creation](https://docs.kalshi.com/api-reference/orders/create-order-v2),
  [order direction](https://docs.kalshi.com/getting_started/order_direction),
  [order books](https://docs.kalshi.com/api-reference/market/get-market-orderbook),
  [fee rounding](https://docs.kalshi.com/getting_started/fee_rounding), and
  [WebSockets](https://docs.kalshi.com/getting_started/quick_start_websockets).

API behavior, eligibility, jurisdiction, terms, taxes, and fee schedules can
change. Revalidate them before live use. Nothing here is a profitability claim.

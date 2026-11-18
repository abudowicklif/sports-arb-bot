# Implemented architecture

This document describes the code actually present in the archive. It is not a
roadmap that substitutes for live execution. Funded-account verification remains
outstanding on both venues; see [LIVE_TRADING.md](LIVE_TRADING.md).

## 1. Design principles

1. **Fail closed.** Missing fees, invalid books, uncertain submission outcomes,
   stream gaps, incomplete fills, and reconciliation breaks stop the affected
   execution path.
2. **Decimal economics.** Prices, quantities, fees, limits, and PnL do not pass
   through binary floats for edge decisions.
3. **No implicit zero costs.** Venue fee metadata is required. Unsupported fee
   or tick structures are skipped rather than optimistically interpreted.
4. **Non-atomic means non-atomic.** A transport batch does not prove a hedge.
   Every leg has its own order ID, terminal state, fill evidence, and unwind
   handling.
5. **Venue-native accounting.** Polymarket outcome assets and Kalshi net YES/NO
   positions require different position and realized-PnL logic.
6. **No fabricated evidence.** Scanner indications, order states, fills,
   venue-reported positions, and estimated PnL are distinct evidence classes.
7. **A halted system is valid.** The kill file and manual reconciliation are
   preferred to continuing with unknown exposure.

## 2. Runtime topology

```text
                         sports-arb-multi
                    (one exclusive process lock)
                                  |
                +-----------------+-----------------+
                |                                   |
       Polymarket live worker               Kalshi live worker
                |                                   |
     Gamma + CLOB REST discovery          Kalshi Events/Market REST
     CLOB fresh pair snapshots            Kalshi fresh order books
     market-specific Gamma fees           event override/series fees
                |                                   |
        SameMarketDetector                   SameMarketDetector
        (separate outcome assets)            (whole contracts, fee rounding)
                |                                   |
     Polymarket signed FOK batch            Kalshi V2 FOK batch
     user WS + heartbeat + REST             auth WS + REST order recovery
     real FAK unequal-fill unwind           real IOC reduce-only unwind
                |                                   |
     Data API + CLOB fills/orders           Kalshi fills/net positions
                |                                   |
                +-----------------+-----------------+
                                  |
                    atomic shared risk reservation
                    + persistent append-only ledger
                                  |
                  data/live.db + data/KILL_SWITCH
```

The two workers are concurrent but do not trade against each other or assume
that similarly named contracts are equivalent. Each looks only at complementary
outcomes within one venue-native binary market.

`sports-arb-live` can still run the Polymarket adapter alone. `sports-arb` is a
separate read-only Polymarket research scanner and has no credentials or order
path.

## 3. Canonical market and book model

`models.py` defines `SportsMarket` and `FeeSchedule`; `book.py` defines validated
`BookLevel`, `OrderBook`, and the last-known-good snapshot cache.

For Polymarket:

- Gamma `version` selects V1 `clobTokenIds` or V2 `positionIds`.
- `feesEnabled` and `feeSchedule` are mandatory in the expected form.
- CLOB books are normalized to descending bids and ascending asks.
- Source timestamps, receive timestamps, tick size, minimum size, condition ID,
  and sports delay survive normalization.

For Kalshi:

- Only active, non-provisional binary markets with default settlement bounds are
  eligible.
- Event `fee_type_override`/`fee_multiplier_override` takes precedence over the
  parent series. Quadratic taker schedules use the multiplier-derived rate;
  unsupported flat schedules are explicitly skipped.
- Price ranges must have one positive uniform step. The discovered step is used
  for protected limit construction.
- REST `orderbook_fp.yes_dollars` and `no_dollars` contain bids only:

```text
YES ask(price, size) = (1 - NO bid price, NO bid size)
NO ask(price, size)  = (1 - YES bid price, YES bid size)
YES bids and NO bids retain their native outcome prices
```

Normalized Kalshi asset IDs are `TICKER::YES` and `TICKER::NO`; the ticker is the
condition/market key used by the execution ledger.

## 4. Detection and protected limits

`detector.py` is shared arithmetic, not a shared execution assumption. It:

- rejects missing, crossed-context, stale, or skewed snapshots;
- requires both ask ladders and enough paired depth;
- evaluates minimum, maximum, depth-boundary, and capital-boundary quantities;
- walks every consumed ask level with `Decimal`;
- applies market-specific fee curves, fee rounding, slippage, and optional
  settlement/order reserves;
- can restrict quantities to whole Kalshi contracts;
- returns below-threshold opportunities for diagnostics without qualifying them.

For Kalshi, each model fee is rounded upward to `$0.000001`, and the detector adds
a `$0.01` per-order balance-alignment reserve. This is intentionally conservative;
actual fill `fee_cost` replaces estimates for accounting.

Before either live submission, the selected pair is fetched again. Each buy limit
is the worst consumed ask plus one tick, rounded to a valid outcome tick. The
engine rechecks pair cost and edge at those protected limits rather than relying
on the earlier scanner indication.

## 5. Atomic shared risk gate

`live_ledger.py` uses `BEGIN IMMEDIATE` around each round reservation. In the
same transaction it checks:

1. current UTC-day committed cost plus the new protected cap;
2. unfinished (`SUBMITTING`) round cost plus the new cap;
3. current recorded daily realized PnL against the loss halt.

Only then does it insert the round reservation. Polymarket and Kalshi therefore
cannot independently approve against a stale shared total. The normal coordinator
also holds an exclusive file lock, while SQLite provides transactional protection
for the shared ledger itself.

Existing open orders block each venue worker. Kalshi additionally requires zero
nonzero contract positions before starting clean rounds. Available collateral or
balance must cover the protected cap.

Risk controls are necessary but not sufficient: they do not eliminate venue
outages, sudden gaps, resolution loss, or an unwind that cannot execute within
its price cap.

## 6. Polymarket execution state machine

`live_gateway.py` wraps `py-clob-client-v2==1.2.1` for real signing, FOK/FAK
submission, order queries, trades, collateral, cancellation, and heartbeats.
`live_execution.py` implements:

```text
DISCOVER -> FRESH_VALIDATE -> RISK_RESERVE
         -> USER_WS + HEARTBEAT -> SIGNED_FOK_BATCH
         -> INDEPENDENT_ORDER_STATES -> TRADE_EVIDENCE
         -> equal fills: HEDGED_OPEN / NO_FILL
         -> unequal fills: capped real FAK SELL unwinds
         -> FLAT_UNWOUND / EXPOSED_HALTED / RECONCILIATION_REQUIRED
```

Important details:

- Batch responses are normalized independently.
- Network errors after possible venue acceptance are `SUBMISSION_UNKNOWN`;
  signed batches are not blindly retried.
- Sports delay extends the order-monitoring deadline. Cancellation rejection
  does not cause a false terminal assumption.
- `MATCHED` with zero matched size is treated as potentially transient and
  queried again.
- Complete trade rows must cover matched order sizes before flat cash PnL is
  calculated.
- Data API positions are fetched for reconciliation. `HEDGED_OPEN` is not
  realized PnL, and merge/redeem is not automatic.
- Where Polymarket lacks explicit fill fee amounts, cash PnL subtracts estimated
  fees and is labeled accordingly.

## 7. Kalshi execution state machine

`kalshi_gateway.py` uses the official async SDK for production REST and V2 order
models. SDK 3.35.0's broken convenience `set_kalshi_auth()` import is bypassed
only by directly assigning the SDK's own `KalshiAuth`; the SDK continues to sign
and serialize requests.

Normalized action/direction mapping is:

| Intent | V2 `side` | V2 YES-denominated `price` |
|---|---|---|
| Buy YES at `p` | `bid` | `p` |
| Buy NO at `p` | `ask` | `1 - p` |
| Sell YES at `p` | `ask` | `p` |
| Sell NO at `p` | `bid` | `1 - p` |

`bid`/`ask` describes directional exposure. It does not prove cash action, so
`normalize_kalshi_fill()` requires each fill's actual `action`, `outcome_side`,
quantity, outcome-denominated price, and `fee_cost`.

The state machine is:

```text
DISCOVER -> FRESH_VALIDATE -> PRE_ROUND_REALIZED_BASELINE
         -> RISK_RESERVE -> AUTHENTICATED_WS
         -> V2_FOK_BATCH -> INDEPENDENT_ORDER_STATES -> FILL_EVIDENCE
         -> unequal fills: capped IOC REDUCE-ONLY unwinds
         -> VENUE_NET_POSITION_CHECK
         -> NO_FILL / FLAT_MATCHED / FLAT_UNWOUND
            / EXPOSED_HALTED / RECONCILIATION_REQUIRED
```

Expected net contracts are calculated from YES fills minus NO fills, adjusted by
unwinds. The result must equal the venue's `position_fp`. A nonzero mismatch or
missing fill evidence activates the kill switch.

Kalshi `realized_pnl_dollars` can be cumulative for a market. The engine stores
its pre-round baseline and uses only the post-round delta, and only when net
contracts are zero and fill evidence is complete. It never books an assumed $1
redemption for a net-flat Kalshi round.

## 8. WebSockets and recovery

Polymarket's authenticated user stream supplies account order/trade events while
REST remains the terminal-state authority. The order heartbeat is serialized
through the SDK gateway every five seconds during execution.

Kalshi's authenticated production WebSocket subscribes separately to:

- `orderbook_delta` for the selected ticker, with explicit unified YES pricing;
- `fill`;
- `user_orders`.

Subscription acknowledgement is required before order submission. Sequence
numbers are tracked per subscription; a gap, error frame, or disconnect marks
the stream unhealthy and halts monitoring. Events are persisted in the ledger.
Fresh execution decisions continue to use REST books, so the monitoring stream
is not mistaken for a fully recovered delta cache.

Neither adapter treats a WebSocket event alone as proof of final account state.

## 9. Evidence, PnL, and alerts

SQLite tables:

- `rounds`: protected committed cap, status, cash flow, fees, realized result,
  baseline/details JSON;
- `orders`: venue order ID, outcome, requested action/price/size, terminal state,
  matched size, raw redacted payload;
- `fills`: venue fill/trade ID, actual action, quantity, price, status, fee data;
- `events`: submissions, errors, WebSocket events, cancellation, and lifecycle
  transitions.

The schema retains historical column names such as `estimated_realized_pnl` for
compatibility. Kalshi details explicitly identify the value as a venue-reported
baseline delta with actual fees; Polymarket details distinguish estimated fees.
The CLI labels them differently as well.

Console logs and `data/KILL_SWITCH` are built-in alerts. No external pager or
metrics service is falsely claimed. External operational alerting should watch
process exit, kill-file creation, database errors, and residual exposure.

## 10. Coordinator failure behavior

`multi_cli.py` creates one Polymarket and one Kalshi worker. In one-shot mode it
waits for both. In continuous mode each loops until an error or interruption.

On worker failure or cancellation, the coordinator:

1. cancels both worker tasks and waits for their cleanup;
2. creates/overwrites `data/KILL_SWITCH`;
3. requests real account-wide cancellation on both venues, best effort;
4. preserves the original failure for the operator.

A manual `kill` writes the kill file before credential validation. If credentials
are unavailable, local re-arming is still blocked, but venue cancellation may
require the venue dashboards.

## 11. Deliberate boundaries

- No related-market payoff graph or unverified cross-venue title matching.
- No automatic Polymarket merge/redeem transaction.
- No guarantee that an arbitrage exists at the venue minimum size.
- No low-latency colocated infrastructure or claim of competitive machine speed.
- No live performance, capacity, profitability, or “master” claim without real
  fills and measured distributions.
- No external sportsbook adapter, portfolio mark-to-market engine, tax engine,
  or external paging service.

These boundaries do not replace the real order paths that are implemented. They
identify risks and functions that must not be silently inferred from them.

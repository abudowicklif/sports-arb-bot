# Real Polymarket + Kalshi live trading

> **Current status: NOT READY FOR DEPOSIT OR LIVE EXECUTION.** The procedures
> below describe guarded code paths; they are not evidence of funded-account
> connectivity or live fills. The repository audit still lists critical blockers
> including the unverified live order lifecycle, missing licensed sports-data
> provider and real-data validation. Do not fund an account or remove any live
> arming safeguards until those blockers are closed and independently verified.

`sports-arb-multi` runs **real production strategies on Polymarket and Kalshi at
the same time**. Each venue independently looks for complementary YES/NO orders
on one of its own markets. The coordinator does not infer cross-venue settlement
equivalence from similar titles.

There is no paper-fill, backtest, or simulated-execution branch in either live
adapter. If the command reaches order submission, real funds are at risk.

## Verification status

Checked in this workspace:

- Deterministic fixtures and unit tests; see
  [VALIDATION.md](VALIDATION.md) for the dated test record.
- Historical public-market smoke checks and local SDK/model checks, as detailed
  in [VALIDATION.md](VALIDATION.md). They do not prove current connectivity,
  account access, order acceptance, fills, or continuous monitoring.

Not checked because no funded account credentials were available:

- An authenticated Kalshi account response or WebSocket session.
- A real Polymarket or Kalshi order acceptance, fill, cancel/fill race, partial
  fill, unwind, or flat-position reconciliation.
- Realized live PnL, production latency, capacity, or profitability.

**Do not call the system live-fill verified until the checklist at the end of
this document produces genuine venue order/trade IDs and matching account
records on both venues.** Tests cannot substitute for that evidence.

## 1. Prerequisites

1. Eligible Polymarket and Kalshi accounts in your jurisdiction. Do not bypass
   geographic, account, or venue restrictions.
2. Small, dedicated balances that you can afford to lose. Start with the minimum
   practical funds; do not use shared market-making API keys.
3. Polymarket pUSD, the required route approvals, signer key, correct signature
   type, and funder/wallet address.
4. A Kalshi production API key ID and its unencrypted RSA or Ed25519 private key
   PEM. The key ID is not the private key.
5. Python 3.13+ and a private terminal/host with reliable time synchronization
   and networking.

Review the current official documents before funding:

- Polymarket [wallets/auth](https://docs.polymarket.com/trading/wallets-auth),
  [deposit wallets](https://docs.polymarket.com/trading/deposit-wallets), and
  [order lifecycle](https://docs.polymarket.com/concepts/order-lifecycle).
- Kalshi [API environments](https://docs.kalshi.com/getting_started/api_environments),
  [authenticated requests](https://docs.kalshi.com/getting_started/quick_start_authenticated_requests),
  and [V2 orders](https://docs.kalshi.com/getting_started/quick_start_create_order).

The only production endpoints configured are:

```text
Polymarket CLOB: https://clob.polymarket.com (Polygon chain ID 137)
Kalshi REST:     https://external-api.kalshi.com/trade-api/v2
Kalshi WS:       wss://external-api-ws.kalshi.com/trade-api/ws/v2
```

## 2. Install and validate

From the extracted source directory:

```bash
cd sports-arb-bot
python -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
pytest
ruff check src tests
python -m pip check
```

Pinned venue SDKs:

```text
py-clob-client-v2==1.2.1
kalshi-python-async==3.35.0
```

Kalshi SDK 3.35.0 has a `set_kalshi_auth()` import defect that raises
`NameError`. `kalshi_gateway.py` works around only that defect by assigning the
SDK's own validated `KalshiAuth` object to the client authentication field. The
official SDK still performs REST serialization and request signing; no custom
order or authentication replacement is used.

## 3. Configure protected credentials

```bash
cp .env.live.example .env.live
chmod 600 .env.live
chmod 600 /absolute/path/to/kalshi-private-key.pem
```

Edit `.env.live` locally. Never paste real credentials into chat, source control,
logs, screenshots, or tickets. `.env.live` and runtime databases are gitignored.

### Polymarket values

```text
POLYMARKET_PRIVATE_KEY=0x...
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
POLYMARKET_SIGNATURE_TYPE=...
POLYMARKET_FUNDER_ADDRESS=0x...
```

Signature types:

| Value | Wallet type |
|---:|---|
| 0 | EOA; funder may be omitted |
| 1 | Polymarket proxy; actual funder required |
| 2 | Gnosis Safe; actual funder required |
| 3 | POLY_1271 deposit wallet; actual funder required |

Do not guess the type or use the signer address as the funder for proxy/Safe/
deposit accounts.

If L2 credentials are not already available, set the signer value, load the
file, and derive them in a private terminal:

```bash
set -a
source .env.live
set +a
sports-arb-live derive-credentials
```

Copy the three printed values into `.env.live`. The command deliberately prints
secrets only for this requested local setup; protect the terminal output.

### Kalshi values

```text
KALSHI_API_KEY_ID=your-production-api-key-id
KALSHI_PRIVATE_KEY_PATH=/absolute/path/to/kalshi-private-key.pem
```

The PEM file must be mode `600`; group/other-readable keys are rejected. RSA and
Ed25519 are detected and validated by the official SDK. Keep the PEM out of the
command line and environment value itself—only its protected path is loaded.

### Manual live arming

Both independent pairs are required for concurrent trading, in addition to the
CLI confirmation phrase:

```text
POLYMARKET_LIVE_TRADING=YES
POLYMARKET_LIVE_CONFIRM='I UNDERSTAND REAL FUNDS ARE AT RISK'
KALSHI_LIVE_TRADING=YES
KALSHI_LIVE_CONFIRM='I UNDERSTAND REAL FUNDS ARE AT RISK'
```

Omit these four variables until you intentionally want real order submission.
They are not needed for `auth-check` or `reconcile`.

Load the completed file for every new shell:

```bash
set -a
source .env.live
set +a
```

## 4. Authenticated check — no orders

```bash
sports-arb-multi auth-check
```

Confirm all of the following before continuing:

- expected Polymarket wallet and signature type;
- expected Polymarket collateral and allowance entries;
- Kalshi production exchange response and expected available balance;
- zero unexplained open orders on both venues;
- no authentication or permission errors.

`auth-check` performs authenticated reads only. It never creates an order.
Balance/allowance success does not guarantee that a future order will be
accepted or filled.

To inspect accounts separately, `sports-arb-live auth-check --refresh-balance`
is also available for Polymarket.

## 5. Run one concurrent real round

Start with small caps:

```bash
sports-arb-multi trade \
  --confirm-live 'REAL FUNDS' \
  --max-shares 5 \
  --max-pair-cost 10 \
  --max-daily-notional 20 \
  --max-in-flight-cost 20 \
  --max-daily-loss 1 \
  --max-unwind-loss 1 \
  --min-net-edge-bps 50 \
  --min-net-profit 0.01 \
  --order-timeout 12 \
  --unwind-attempts 3
```

This performs at most one candidate execution attempt per venue, concurrently.
A venue with no qualifying candidate submits nothing and returns no round result.
The command does not manufacture an order merely to test connectivity.

The defaults cover moneylines, spreads, totals, futures, and props. To restrict
research explicitly:

```bash
sports-arb-multi trade \
  --confirm-live 'REAL FUNDS' \
  --market-types moneyline spreads totals \
  --max-markets 10
```

Omitting the other risk arguments retains the small defaults shown above.
Hard-coded ceilings reject excessively large pair, daily, share, loss, and unwind
settings even if requested on the CLI.

### Shared risk meaning

| Argument | Default | Scope |
|---|---:|---|
| `--max-shares` | 5 | Maximum shares/contracts selected per venue pair |
| `--max-pair-cost` | 10 | Protected maximum cost for one venue pair |
| `--max-daily-notional` | 20 | Combined committed cost across both venues per UTC day |
| `--max-in-flight-cost` | 20 | Combined cost reserved by unfinished rounds |
| `--max-daily-loss` | 1 | Combined recorded realized-loss halt per UTC day |
| `--max-unwind-loss` | 1 | Per-round loss budget used to cap unwind prices |
| `--min-net-edge-bps` | 50 | Minimum modeled net edge after costs |
| `--min-net-profit` | 0.01 | Minimum modeled absolute net profit |

The shared ledger reservation is atomic. If Kalshi reserves cost first,
Polymarket cannot simultaneously approve against the old daily total, and vice
versa. Committed-cost reservations are intentionally conservative and are not
presented as actual losses or filled notionals.

## 6. What the production executors do

### Common sequence

1. Require both credential sets, both arming pairs, `REAL FUNDS`, an exclusive
   process lock, and no existing kill file.
2. Refuse a venue round when its account has existing open orders. Kalshi also
   refuses to start with any nonzero contract position.
3. Discover active sports markets and reject unknown/unsupported fees, invalid
   lifecycle states, stale books, insufficient depth, or inadequate net edge.
4. Re-fetch the selected books immediately before submission and recalculate the
   full depth-walked economics.
5. Set protected buy limits to the worst consumed ask plus one tick; reject if
   the edge or capital cap does not survive those limits and fee reserves.
6. Check actual available collateral/balance and atomically reserve shared risk.
7. Start the authenticated venue WebSocket and submit real non-atomic legs.
8. Reconcile each order independently. Unknown submission outcomes trigger
   cancellation requests and a halt, never a blind retry.
9. Unequal fills trigger capped real unwinds. Residual exposure or incomplete
   evidence activates the kill file.
10. Persist order IDs, states, fills, actual fees where supplied, WebSocket
    events, errors, positions, and round outcomes in `data/live.db`.

### Polymarket-specific behavior

- Official SDK EIP-712 signing and two genuine FOK orders in one batch.
- Authenticated user WebSocket and five-second order heartbeat during execution.
- Sports `secondsDelay` is included in order-monitoring timeouts; cancellation
  restrictions can still apply.
- Real sell FAK orders unwind unequal outcome-token fills.
- `HEDGED_OPEN` means equal separate outcome assets remain open. It is **not**
  realized PnL. Merge/redeem/resolution is not automatic.
- Where explicit trade fee amounts are unavailable, flat cash PnL is labeled
  estimated-fee PnL rather than venue-confirmed net profit.

### Kalshi-specific behavior

- Official V2 `fill_or_kill` batch for entry and `immediate_or_cancel` orders for
  reduce-only unwinds. Each batch result is checked independently.
- `bid` means long YES and `ask` means long NO. A normalized NO price is converted
  to the V2 YES-denominated price by `yes_price = 1 - no_price`.
- REST `yes_dollars` and `no_dollars` arrays are bids only. YES asks come from
  complemented NO bids; NO asks come from complemented YES bids.
- Actual cash `action` and actual `fee_cost` are read from every fill. The code
  does not infer whether a direction-closing fill was a buy or sell.
- Event fee overrides take precedence over series fees. Quadratic taker fees are
  rounded upward to six decimals, with an additional one-cent balance-rounding
  reserve per order. Unsupported flat fee schedules and non-uniform/missing tick
  structures are skipped.
- Kalshi maintains one net YES/NO market position. A matched complementary round
  is flat only when the venue position confirms zero net contracts.
- Realized PnL is the current venue `realized_pnl_dollars` minus its pre-round
  baseline, used only for a flat round with complete fill evidence. No assumed
  $1 redemption is booked.

Authenticated Kalshi WebSocket subscriptions cover `orderbook_delta`, `fill`,
and `user_orders`; sequence gaps or stream failure halt execution. Fresh
execution decisions still use REST snapshots so an incomplete delta cache is
never treated as authoritative.

## 7. Continuous operation — only after real verification

```bash
sports-arb-multi trade \
  --confirm-live 'REAL FUNDS' \
  --continuous \
  --poll-interval 2
```

Both workers continue independently. If either raises an execution error, the
coordinator activates the kill file, cancels the sibling task, and requests real
account-wide cancellation on both venues. Cancellation is best effort and does
not guarantee that a resting order will not fill first.

REST polling is not machine-speed market making. Competing bots can consume an
edge before either leg fills. Short windows, capital lockup, fragmented balances,
and failed hedges remain material risks.

## 8. Emergency stop and reconciliation

From the repository root with credentials loaded:

```bash
# Creates data/KILL_SWITCH first, then requests REAL cancel-all on both venues.
sports-arb-multi kill

# Fetch real open orders, Kalshi recent fills/balance, and venue positions.
sports-arb-multi reconcile
```

The kill file is created before credential validation, so a missing/misconfigured
credential cannot prevent the local stop flag from being written. In that
situation venue cancellation may be impossible through the CLI; use the venue
UIs immediately as well.

Also independently inspect both venue dashboards. `Ctrl-C` during execution
activates the kill file and requests cancellation, but you should still run
`kill` and `reconcile` after an interruption.

Do not remove the kill file merely to bypass a risk breach. Only after every
order, fill, fee, and position is explained:

```bash
rm data/KILL_SWITCH
```

Polymarket-only controls remain available:

```bash
sports-arb-live kill
sports-arb-live reconcile
sports-arb-live reconcile --condition-id 0xYOUR_CONDITION_ID
```

## 9. Result statuses

### Polymarket

| Status | Meaning |
|---|---|
| `NO_FILL` | Neither outcome leg acquired shares |
| `HEDGED_OPEN` | Equal nonzero outcome positions remain open; no realized payout claimed |
| `FLAT_UNWOUND` | Exposure was closed; estimated-fee cash PnL is recorded |
| `EXPOSED_HALTED` | Residual one-sided exposure remains; manual action required |
| `RECONCILIATION_REQUIRED` | Evidence is incomplete or inconsistent; no PnL claimed |

### Kalshi

| Status | Meaning |
|---|---|
| `NO_FILL` | No contracts filled and venue net position is zero |
| `FLAT_MATCHED` | Complementary fills and venue net position confirm flat |
| `FLAT_UNWOUND` | Unequal fills were unwound and venue net position confirms flat |
| `EXPOSED_HALTED` | Venue net directional exposure remains; manual action required |
| `RECONCILIATION_REQUIRED` | Position/fill evidence is incomplete or inconsistent; no PnL claimed |

The ledger is `data/live.db`. Secrets, passphrases, private keys, and account
owner fields are redacted from persisted payloads. Console logging and the
persistent kill file are the built-in alert channel; external paging must be
configured separately.

## 10. First genuine live verification checklist

Do this separately and carefully; never place an order solely to satisfy a test.

1. Complete `sports-arb-multi auth-check` and confirm both expected accounts.
2. Independently confirm current venue minimum sizes and fees. Kalshi whole
   contracts start at one; Polymarket market `orderMinSize` can be higher.
3. Use one non-continuous command with minimum practical shares/cost caps. For a
   one-contract Kalshi-eligible attempt, `--max-shares 1` prevents Polymarket
   markets with a larger minimum from trading. Do not lower fee/edge safeguards.
4. If no candidate survives, record that fact and stop. A minimum-size test order
   is not guaranteed to exist as an arbitrage opportunity.
5. If an order is submitted, save the returned real order IDs and round ID.
6. Run `sports-arb-multi reconcile` and compare every order, fill quantity,
   actual action, price, fee, and position with both venue dashboards.
7. Verify the pre-round and post-round Kalshi realized-PnL delta. For Polymarket,
   distinguish estimated-fee cash accounting from venue position PnL.
8. If residual exposure or `RECONCILIATION_REQUIRED` occurs, keep the kill file,
   stop all workers, and resolve it manually.
9. Repeat only with deliberately minimal risk until real acceptance, fills,
   cancellation, unequal-fill handling, and reconciliation are understood.
10. Consider `--continuous` only after both adapters have genuine matching venue
    evidence and measured latency/fill behavior. Do not infer mastery or
    profitability from one successful round.

Real order IDs, actual fills, reconciled exposure, and explainable cash PnL are
the completion standard—not scanner output, theoretical edge, or viral wallet
narratives.

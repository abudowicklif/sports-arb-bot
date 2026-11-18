"""Real Kalshi two-leg execution with independent fills and capped unwinds."""

from __future__ import annotations

import asyncio
import logging
import time
from decimal import Decimal

from sports_arb.book import OrderBookCache
from sports_arb.detector import Opportunity, SameMarketDetector
from sports_arb.kalshi_gateway import KalshiGateway
from sports_arb.kalshi_stream import KalshiWebSocketStream
from sports_arb.live_config import KILL_FILE, LiveRiskConfig
from sports_arb.live_execution import (
    LiveExecutionError,
    LiveRoundResult,
    _round_to_tick,
    _weighted_acquisition_price,
    fills_cover_states,
)
from sports_arb.live_ledger import LiveLedger
from sports_arb.live_models import OrderState, OrderSubmission, VenueFill
from sports_arb.models import ONE, ZERO, SportsMarket, decimal_value
from sports_arb.scanner import SportsScanner
from sports_arb.strategy import StrategyConfig, cap_plan, detector_config

LOGGER = logging.getLogger(__name__)


class KalshiTrader:
    def __init__(
        self,
        *,
        gateway: KalshiGateway,
        stream: KalshiWebSocketStream,
        ledger: LiveLedger,
        risk: LiveRiskConfig,
    ) -> None:
        risk.validate()
        self.gateway = gateway
        self.stream = stream
        self.ledger = ledger
        self.risk = risk
        self.strategy = StrategyConfig.load()
        self.detector = SameMarketDetector(detector_config(risk, self.strategy, venue="kalshi"))
        self.scanner = SportsScanner(gateway, gateway, self.detector)

    async def trade_best(
        self, market_types: tuple[str, ...], max_markets: int
    ) -> LiveRoundResult | None:
        open_orders = await self.gateway.open_orders()
        if open_orders:
            raise LiveExecutionError(
                f"Kalshi has {len(open_orders)} existing resting orders; reconcile first"
            )
        positions = await self.gateway.positions()
        nonzero = [
            item
            for item in positions
            if decimal_value(item.get("position_fp") or ZERO, "position_fp") != ZERO
        ]
        if nonzero:
            raise LiveExecutionError(
                "Kalshi has existing contract positions; reconcile them before a clean round"
            )
        markets = await self.scanner.discover(market_types, max_markets)
        report = await self.scanner.scan_once(markets)
        candidates = [
            item.opportunity for item in report.qualifying if item.opportunity is not None
        ]
        if not candidates:
            LOGGER.info(
                "No Kalshi candidate survived costs: markets=%d statuses=%s",
                len(markets),
                report.status_counts,
            )
            return None
        opportunity = max(candidates, key=lambda item: item.net_profit)
        market = next(item for item in markets if item.market_id == opportunity.market_id)
        return await self.execute(market, opportunity)

    async def execute(self, market: SportsMarket, prior: Opportunity) -> LiveRoundResult:
        books = await self.gateway.get_books(list(market.asset_ids))
        cache = OrderBookCache()
        for book in books.values():
            cache.put(book)
        evaluation = self.detector.evaluate(market, cache)
        if evaluation.status != "qualifying" or evaluation.opportunity is None:
            raise LiveExecutionError(
                f"Kalshi opportunity disappeared on fresh validation: {evaluation.status}"
            )
        baseline_positions = await self.gateway.positions(market.market_id)
        baseline_realized = (
            decimal_value(
                baseline_positions[0].get("realized_pnl_dollars") or ZERO,
                "baseline realized_pnl_dollars",
            )
            if baseline_positions
            else ZERO
        )
        opp = evaluation.opportunity
        book_0 = cache.get(market.asset_ids[0])
        book_1 = cache.get(market.asset_ids[1])
        if book_0 is None or book_1 is None:
            raise LiveExecutionError("Kalshi fresh validation lost an order book")
        limit_0, limit_1, cap_cost, cap_edge_bps = cap_plan(
            market, opp.shares, book_0, book_1, extra_cost=self.strategy.kalshi_cap_extra
        )
        if cap_cost > self.risk.max_pair_cost:
            raise LiveExecutionError("Kalshi capped pair cost exceeds live capital limit")
        if cap_edge_bps < self.risk.min_net_edge_bps:
            raise LiveExecutionError("Kalshi edge does not survive protected limits")
        if await self.gateway.balance() < cap_cost:
            raise LiveExecutionError("insufficient available Kalshi balance")
        round_id = self.ledger.create_round(
            market.market_id,
            market.condition_id,
            opp.shares,
            cap_cost,
            venue="kalshi",
            max_daily_notional=self.risk.max_daily_notional,
            max_in_flight_cost=self.risk.max_in_flight_cost,
            max_daily_realized_loss=self.risk.max_daily_realized_loss,
        )
        self.ledger.event(
            round_id,
            "KALSHI_LIVE_INTENT",
            {
                "ticker": market.market_id,
                "contracts": str(opp.shares),
                "outcome_limits": [str(limit_0), str(limit_1)],
                "cap_cost_including_rounding_reserve": str(cap_cost),
                "prior_book_age_ms": prior.book_age_ms,
                "fresh_book_age_ms": opp.book_age_ms,
            },
        )
        states: tuple[OrderState, OrderState] | None = None
        unwind_states: list[OrderState] = []
        try:
            await self.stream.start([market.market_id])
            if not self.stream.healthy:
                raise LiveExecutionError("Kalshi authenticated WebSocket is not healthy")
            LOGGER.warning(
                "SUBMITTING REAL KALSHI FOK PAIR ticker=%s contracts=%s max_cost=%s",
                market.market_id,
                opp.shares,
                cap_cost,
            )
            try:
                raw_submissions = await self.gateway.post_pair_fok(
                    market, prices=(limit_0, limit_1), count=opp.shares
                )
            except Exception as exc:
                self.ledger.event(round_id, "KALSHI_SUBMISSION_UNKNOWN", str(exc))
                await self._best_effort_cancel(round_id, market.market_id)
                raise LiveExecutionError(
                    "Kalshi batch outcome is unknown; cancellation was requested; reconcile"
                ) from exc
            submissions = tuple(OrderSubmission.from_raw(raw) for raw in raw_submissions)
            for index, submission in enumerate(submissions):
                self.ledger.record_submission(
                    round_id,
                    submission,
                    asset_id=market.asset_ids[index],
                    side="BUY",
                    price=(limit_0, limit_1)[index],
                    size=opp.shares,
                )
            states_list = await asyncio.gather(
                *(self._monitor(round_id, item, opp.shares) for item in submissions)
            )
            states = (states_list[0], states_list[1])
            fills = await self._fills(states)
            self.ledger.record_fills(fills)
            if states[0].size_matched != states[1].size_matched:
                unwind_states = await self._unwind(round_id, market, states, fills)
                fills = await self._fills([*states, *unwind_states])
                self.ledger.record_fills(fills)
            return await self._finish(
                round_id,
                market,
                states,
                unwind_states,
                fills,
                baseline_realized=baseline_realized,
            )
        except asyncio.CancelledError:
            self.ledger.event(round_id, "KALSHI_EXECUTION_CANCELLED", "process cancellation")
            self._activate_kill("process cancelled during Kalshi execution")
            await asyncio.shield(self._best_effort_cancel(round_id, market.market_id))
            raise
        except Exception as exc:
            self.ledger.event(round_id, "KALSHI_EXECUTION_FAILURE", str(exc))
            self._activate_kill(f"Kalshi execution failure: {exc}")
            await self._best_effort_cancel(round_id, market.market_id)
            raise
        finally:
            for event in self.stream.drain():
                self.ledger.event(round_id, "KALSHI_WS", event)
            await self.stream.stop()

    async def _monitor(
        self, round_id: str, submission: OrderSubmission, expected_size: Decimal
    ) -> OrderState:
        if not submission.accepted:
            return OrderState.rejected(submission)
        immediate = OrderState.from_unmatched_submission(submission)
        if immediate is not None:
            return immediate
        assert submission.order_id is not None
        deadline = time.monotonic() + self.risk.order_timeout_seconds
        cancellation_requested = False
        last_error: str | None = None
        while True:
            for event in self.stream.drain():
                self.ledger.event(round_id, "KALSHI_WS", event)
            if not self.stream.healthy:
                raise LiveExecutionError("Kalshi WebSocket failed during order monitoring")
            try:
                state = OrderState.from_raw(
                    await self.gateway.get_order(submission.order_id), submission.order_id
                )
                self.ledger.record_state(round_id, state)
                if state.terminal and not (
                    state.status == "EXECUTED"
                    and expected_size > ZERO
                    and state.size_matched == ZERO
                ):
                    return state
            except Exception as exc:
                last_error = str(exc)
                self.ledger.event(round_id, "KALSHI_ORDER_QUERY_ERROR", last_error)
            if time.monotonic() >= deadline:
                if not cancellation_requested:
                    cancellation_requested = True
                    deadline = time.monotonic() + 5
                    try:
                        await self.gateway.cancel_order(submission.order_id)
                    except Exception as exc:
                        self.ledger.event(round_id, "KALSHI_CANCEL_ERROR", str(exc))
                else:
                    raise LiveExecutionError(
                        f"Kalshi order {submission.order_id} did not become terminal; "
                        f"last error={last_error}"
                    )
            await asyncio.sleep(0.25)

    async def _fills(self, states: list[OrderState] | tuple[OrderState, ...]) -> list[VenueFill]:
        ids = {state.order_id for state in states if state.order_id}
        if not ids:
            return []
        fills: list[VenueFill] = []
        for _ in range(10):
            groups = await asyncio.gather(*(self.gateway.fills_for_order(item) for item in ids))
            fills = [fill for group in groups for fill in group]
            if fills_cover_states(states, fills):
                return fills
            await asyncio.sleep(0.5)
        self.ledger.event(None, "KALSHI_FILL_INDEX_INCOMPLETE", {"order_ids": sorted(ids)})
        return fills

    async def _unwind(
        self,
        round_id: str,
        market: SportsMarket,
        states: tuple[OrderState, OrderState],
        initial_fills: list[VenueFill],
    ) -> list[OrderState]:
        difference = states[0].size_matched - states[1].size_matched
        index = 0 if difference > ZERO else 1
        asset_id = market.asset_ids[index]
        exposure = abs(difference)
        remaining = exposure
        acquisition = _weighted_acquisition_price(initial_fills, states[index], asset_id)
        minimum = (
            acquisition
            + market.fee_schedule.fee(ONE, acquisition)
            - (self.risk.max_unwind_loss / exposure)
        )
        result: list[OrderState] = []
        for attempt in range(1, self.risk.unwind_attempts + 1):
            books = await self.gateway.get_books([asset_id])
            book = books.get(asset_id)
            if book is None or book.best_bid is None:
                self.ledger.event(round_id, "KALSHI_UNWIND_NO_BID", {"attempt": attempt})
                await asyncio.sleep(0.5)
                continue
            tick = book.tick_size or Decimal("0.01")
            limit = _round_to_tick(max(minimum, tick), tick, buy=False)
            if limit > book.best_bid.price - 2 * tick:
                self.ledger.event(round_id, "KALSHI_UNWIND_PRICE_CAP_BLOCKED", {"attempt": attempt})
                await asyncio.sleep(0.5)
                continue
            raw = await self.gateway.post_ioc(
                market,
                asset_id=asset_id,
                action="SELL",
                price=limit,
                count=remaining,
            )
            submission = OrderSubmission.from_raw(raw)
            self.ledger.record_submission(
                round_id,
                submission,
                asset_id=asset_id,
                side="SELL",
                price=limit,
                size=remaining,
            )
            state = await self._monitor(round_id, submission, remaining)
            result.append(state)
            remaining -= state.size_matched
            if remaining <= ZERO:
                return result
        self._activate_kill("Kalshi unwind exhausted with residual exposure")
        return result

    async def _finish(
        self,
        round_id: str,
        market: SportsMarket,
        states: tuple[OrderState, OrderState],
        unwinds: list[OrderState],
        fills: list[VenueFill],
        *,
        baseline_realized: Decimal = ZERO,
    ) -> LiveRoundResult:
        positions = await self.gateway.positions(market.market_id)
        position = positions[0] if positions else {}
        net_contracts = decimal_value(position.get("position_fp") or ZERO, "position_fp")
        complete = fills_cover_states([*states, *unwinds], fills)
        expected_net = (states[0].size_matched - states[1].size_matched) - sum(
            (
                state.size_matched
                if states[0].size_matched > states[1].size_matched
                else -state.size_matched
                for state in unwinds
            ),
            ZERO,
        )
        if net_contracts != expected_net or not complete:
            status = "RECONCILIATION_REQUIRED"
            self._activate_kill("Kalshi position/fill evidence does not reconcile")
        elif net_contracts != ZERO:
            status = "EXPOSED_HALTED"
            self._activate_kill("Kalshi residual directional exposure")
        elif states[0].size_matched == states[1].size_matched == ZERO:
            status = "NO_FILL"
        else:
            status = "FLAT_UNWOUND" if unwinds else "FLAT_MATCHED"
        actual_fees = sum((fill.fee_amount or ZERO for fill in fills), ZERO)
        cash_flow = sum(
            (fill.notional if fill.side == "SELL" else -fill.notional for fill in fills),
            ZERO,
        )
        # Kalshi reports realized PnL for the net market position. Only use it
        # after the account is demonstrably flat and all fills are present.
        realized = (
            decimal_value(position.get("realized_pnl_dollars") or ZERO, "realized_pnl_dollars")
            - baseline_realized
            if net_contracts == ZERO and complete
            else None
        )
        details = {
            "venue": "kalshi",
            "states": [item.raw for item in states],
            "unwind_states": [item.raw for item in unwinds],
            "expected_net_contracts": str(expected_net),
            "venue_net_contracts": str(net_contracts),
            "complete_fill_evidence": complete,
            "venue_positions": positions,
            "baseline_realized_pnl_dollars": str(baseline_realized),
            "pnl_basis": (
                "post-round minus pre-round Kalshi market_positions."
                "realized_pnl_dollars; no assumed payout"
            ),
        }
        self.ledger.finish_round(
            round_id,
            status,
            cash_flow_before_fees=cash_flow,
            estimated_fees=actual_fees,
            estimated_realized_pnl=realized,
            details=details,
        )
        order_ids = tuple(item.order_id for item in [*states, *unwinds] if item.order_id)
        return LiveRoundResult(
            round_id=round_id,
            status=status,
            market_id=market.market_id,
            condition_id=market.condition_id,
            order_ids=order_ids,
            estimated_realized_pnl=realized,
            venue_positions=positions,
        )

    async def _best_effort_cancel(self, round_id: str, ticker: str) -> None:
        try:
            result = await self.gateway.cancel_market(ticker)
            self.ledger.event(round_id, "KALSHI_MARKET_CANCEL_RESULT", result)
        except Exception as exc:
            self.ledger.event(round_id, "KALSHI_MARKET_CANCEL_FAILED", str(exc))

    @staticmethod
    def _activate_kill(reason: str) -> None:
        KILL_FILE.parent.mkdir(parents=True, exist_ok=True)
        KILL_FILE.write_text(f"{time.time()}: {reason}\n", encoding="utf-8")

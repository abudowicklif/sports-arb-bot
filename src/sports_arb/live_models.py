"""Models for genuine production order execution.

These models represent venue responses. There is no simulated-fill constructor or
paper execution mode in this package.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sports_arb.models import ZERO, decimal_value

TERMINAL_ORDER_STATUSES = {
    "MATCHED",
    "UNMATCHED",
    "CANCELED",
    "CANCELLED",
    "INVALID",
    "REJECTED",
    "FAILED",
}


@dataclass(frozen=True, slots=True)
class OrderSubmission:
    order_id: str | None
    status: str
    accepted: bool
    error: str | None
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: dict[str, Any]) -> OrderSubmission:
        order_id = (
            str(raw.get("orderID") or raw.get("orderId") or raw.get("order_id") or "") or None
        )
        status = str(raw.get("status") or "UNKNOWN").upper()
        error = str(raw.get("errorMsg") or raw.get("error_msg") or raw.get("error") or "") or None
        accepted_flag = raw.get("success", raw.get("ok")) is True
        accepted = accepted_flag and order_id is not None and error is None
        return cls(order_id=order_id, status=status, accepted=accepted, error=error, raw=raw)


@dataclass(frozen=True, slots=True)
class OrderState:
    order_id: str
    status: str
    original_size: Decimal
    size_matched: Decimal
    price: Decimal
    raw: dict[str, Any]

    @classmethod
    def from_raw(cls, raw: dict[str, Any], fallback_id: str = "") -> OrderState:
        order_id = str(raw.get("id") or raw.get("orderID") or fallback_id)
        return cls(
            order_id=order_id,
            status=str(raw.get("status") or "UNKNOWN").upper(),
            original_size=decimal_value(raw.get("original_size", ZERO), "original_size"),
            size_matched=decimal_value(raw.get("size_matched", ZERO), "size_matched"),
            price=decimal_value(raw.get("price", ZERO), "price"),
            raw=raw,
        )

    @property
    def terminal(self) -> bool:
        return self.status in TERMINAL_ORDER_STATUSES

    @classmethod
    def rejected(cls, submission: OrderSubmission) -> OrderState:
        return cls(
            order_id=submission.order_id or "",
            status="REJECTED",
            original_size=ZERO,
            size_matched=ZERO,
            price=ZERO,
            raw=submission.raw,
        )

    @classmethod
    def from_unmatched_submission(cls, submission: OrderSubmission) -> OrderState | None:
        if submission.status not in TERMINAL_ORDER_STATUSES - {"MATCHED"}:
            return None
        return cls(
            order_id=submission.order_id or "",
            status=submission.status,
            original_size=ZERO,
            size_matched=ZERO,
            price=ZERO,
            raw=submission.raw,
        )


@dataclass(frozen=True, slots=True)
class VenueFill:
    trade_id: str
    order_id: str
    asset_id: str
    side: str
    size: Decimal
    price: Decimal
    status: str
    fee_rate_bps: Decimal | None
    raw: dict[str, Any]
    fee_amount: Decimal | None = None

    @property
    def notional(self) -> Decimal:
        return self.size * self.price

    def estimated_fee(self, fallback_rate: Decimal, exponent: int = 1) -> Decimal:
        """Estimate actual taker fee using the trade's reported rate when present.

        Polymarket trade history does not expose a universal explicit fee-amount
        field. PnL using this value is therefore labeled estimated-fee PnL, while
        trade notionals and Data API position PnL remain venue-reported values.
        """
        if self.fee_amount is not None:
            return self.fee_amount
        rate = (
            self.fee_rate_bps / Decimal("10000") if self.fee_rate_bps is not None else fallback_rate
        )
        return self.size * rate * (self.price * (Decimal("1") - self.price)) ** exponent


def fills_for_orders(trades: list[dict[str, Any]], order_ids: set[str]) -> list[VenueFill]:
    """Extract the authenticated account's taker/maker fills for known orders."""
    result: list[VenueFill] = []
    seen: set[tuple[str, str]] = set()
    for trade in trades:
        taker_id = str(trade.get("taker_order_id") or "")
        matched_order_id: str | None = None
        maker_order: dict[str, Any] | None = None
        if taker_id in order_ids:
            matched_order_id = taker_id
        else:
            for candidate in trade.get("maker_orders") or []:
                if str(candidate.get("order_id") or "") in order_ids:
                    matched_order_id = str(candidate["order_id"])
                    maker_order = candidate
                    break
        if matched_order_id is None:
            continue

        trade_id = str(trade.get("id") or "")
        dedup_key = (trade_id, matched_order_id)
        if not trade_id or dedup_key in seen:
            continue
        seen.add(dedup_key)
        source = maker_order or trade
        size_field = source.get("matched_amount") if maker_order else source.get("size")
        fee_raw = source.get("fee_rate_bps")
        result.append(
            VenueFill(
                trade_id=trade_id,
                order_id=matched_order_id,
                asset_id=str(source.get("asset_id") or trade.get("asset_id") or ""),
                side=str(source.get("side") or trade.get("side") or "").upper(),
                size=decimal_value(size_field or ZERO, "trade.size"),
                price=decimal_value(source.get("price", ZERO), "trade.price"),
                status=str(trade.get("status") or "UNKNOWN").upper(),
                fee_rate_bps=(
                    decimal_value(fee_raw, "fee_rate_bps") if fee_raw not in (None, "") else None
                ),
                raw=trade,
            )
        )
    return result

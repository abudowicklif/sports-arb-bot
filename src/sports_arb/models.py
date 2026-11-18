"""Normalized market and fee models.

All prices and sizes use Decimal. Converting exchange data to float before an
edge decision can turn a small loss into an apparent profit.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import ROUND_CEILING, Decimal, InvalidOperation
from typing import Any

from sports_arb.errors import MarketDataError

ZERO = Decimal("0")
ONE = Decimal("1")
BPS = Decimal("10000")


def decimal_value(value: Any, field: str) -> Decimal:
    """Parse an API number without passing through a binary float."""
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise MarketDataError(f"{field} is not a valid decimal: {value!r}") from exc
    if not result.is_finite():
        raise MarketDataError(f"{field} must be finite")
    return result


def json_array(value: Any, field: str) -> list[Any]:
    """Gamma encodes some arrays as JSON strings and others as actual arrays."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise MarketDataError(f"{field} is not valid JSON") from exc
    if not isinstance(value, list):
        raise MarketDataError(f"{field} must be an array or JSON-encoded array")
    return value


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Market-specific Polymarket fee curve.

    For the currently documented sports curve (exponent=1), the taker fee is:
        shares * rate * price * (1 - price)
    Maker rebates and account-specific taker rebates are intentionally excluded.
    """

    rate: Decimal
    exponent: int = 1
    taker_only: bool = True
    rebate_rate: Decimal = ZERO
    ceil_quantum: Decimal = ZERO

    @classmethod
    def from_gamma(cls, raw: dict[str, Any]) -> FeeSchedule:
        enabled = raw.get("feesEnabled")
        if enabled is False:
            return cls(rate=ZERO)

        # Unknown status is not treated as free. That would be an unsafe default.
        if enabled is not True:
            raise MarketDataError("feesEnabled is missing or not boolean")

        schedule = raw.get("feeSchedule")
        if not isinstance(schedule, dict):
            raise MarketDataError("fee-enabled market has no feeSchedule")

        rate = decimal_value(schedule.get("rate"), "feeSchedule.rate")
        if rate < ZERO:
            raise MarketDataError("fee rate cannot be negative")

        raw_exponent = schedule.get("exponent", 1)
        exponent_decimal = decimal_value(raw_exponent, "feeSchedule.exponent")
        if exponent_decimal != exponent_decimal.to_integral_value():
            raise MarketDataError("fractional fee exponents are not supported")
        exponent = int(exponent_decimal)
        if exponent < 0:
            raise MarketDataError("fee exponent cannot be negative")

        taker_only = schedule.get("takerOnly", True)
        if not isinstance(taker_only, bool):
            raise MarketDataError("feeSchedule.takerOnly must be boolean")
        if not taker_only:
            # This scanner only models immediate taker buys. Refuse a different
            # schedule rather than silently under-estimating its costs.
            raise MarketDataError("only taker-only fee schedules are supported")

        rebate_rate = decimal_value(schedule.get("rebateRate", ZERO), "feeSchedule.rebateRate")
        return cls(
            rate=rate,
            exponent=exponent,
            taker_only=taker_only,
            rebate_rate=rebate_rate,
        )

    def fee(self, shares: Decimal, price: Decimal) -> Decimal:
        """Unrounded estimated taker fee for one fill at a fixed price.

        Actual venue rounding is applied to real fills. Not rounding down here is
        deliberately conservative for a multi-level theoretical execution.
        """
        if shares < ZERO:
            raise ValueError("shares cannot be negative")
        if not ZERO < price <= ONE:
            raise ValueError("price must be in (0, 1]")
        if self.rate == ZERO or shares == ZERO:
            return ZERO
        price_component = price * (ONE - price)
        fee = shares * self.rate * price_component**self.exponent
        if self.ceil_quantum > ZERO:
            fee = fee.quantize(self.ceil_quantum, rounding=ROUND_CEILING)
        return fee


@dataclass(frozen=True, slots=True)
class SportsMarket:
    market_id: str
    condition_id: str
    slug: str
    question: str
    version: str
    outcome_labels: tuple[str, str]
    asset_ids: tuple[str, str]
    sports_market_type: str
    fee_schedule: FeeSchedule
    order_min_size: Decimal
    liquidity: Decimal
    volume_24h: Decimal
    game_start_time: str | None
    seconds_delay: int

    @classmethod
    def from_gamma(cls, raw: dict[str, Any]) -> SportsMarket:
        """Validate and normalize the fields needed by the scanner.

        Protocol selection is based on `version`, not on which ID field happens
        to be populated. This is required by the V2 migration documentation.
        """
        try:
            market_id = str(raw["id"])
            condition_id = str(raw["conditionId"])
            slug = str(raw.get("slug") or "")
            question = str(raw.get("question") or "")
            version = str(raw["version"])
            sports_market_type = str(raw["sportsMarketType"])
        except KeyError as exc:
            raise MarketDataError(f"missing market field: {exc.args[0]}") from exc

        labels_raw = json_array(raw.get("outcomes"), "outcomes")
        if len(labels_raw) != 2:
            raise MarketDataError("only two-outcome markets are supported")
        labels = (str(labels_raw[0]), str(labels_raw[1]))

        if version == "v1":
            ids_raw = json_array(raw.get("clobTokenIds"), "clobTokenIds")
        elif version == "v2":
            ids_raw = json_array(raw.get("positionIds"), "positionIds")
        else:
            raise MarketDataError(f"unsupported market version: {version!r}")

        if len(ids_raw) != 2 or any(not str(item).isdigit() for item in ids_raw):
            raise MarketDataError("market must have two decimal outcome asset IDs")
        asset_ids = (str(ids_raw[0]), str(ids_raw[1]))
        if asset_ids[0] == asset_ids[1]:
            raise MarketDataError("outcome asset IDs must be distinct")

        order_min_size = decimal_value(raw.get("orderMinSize", ZERO), "orderMinSize")
        liquidity = decimal_value(raw.get("liquidityNum", ZERO), "liquidityNum")
        volume_24h = decimal_value(raw.get("volume24hr", ZERO), "volume24hr")
        seconds_delay_raw = raw.get("secondsDelay", 0) or 0
        try:
            seconds_delay = int(seconds_delay_raw)
        except (TypeError, ValueError) as exc:
            raise MarketDataError("secondsDelay must be an integer") from exc

        return cls(
            market_id=market_id,
            condition_id=condition_id,
            slug=slug,
            question=question,
            version=version,
            outcome_labels=labels,
            asset_ids=asset_ids,
            sports_market_type=sports_market_type,
            fee_schedule=FeeSchedule.from_gamma(raw),
            order_min_size=order_min_size,
            liquidity=liquidity,
            volume_24h=volume_24h,
            game_start_time=raw.get("gameStartTime"),
            seconds_delay=seconds_delay,
        )

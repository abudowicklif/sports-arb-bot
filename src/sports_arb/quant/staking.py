"""Configurable staking engine (flat, Kelly, adaptive, Martingale variants, Paroli).

Hard rules, enforced in code:
* No staking method ever changes expected value. Every method requires a validated edge
  (positive lower-bound EV from the decision engine); otherwise the stake is 0.
* Progression methods (anything that varies the stake with results) additionally require
  ``staking:<method>`` to be APPROVED_FOR_LIVE in the validation registry, i.e. evidence on file.
  Classic Martingale raises stakes after losses, which conflicts with 'never increase stakes
  merely because previous trades lost', so it exists for simulation research and stays
  unapproved by default.
* The central risk cap always wins; stakes never exceed it, max_stake, or max_fraction.
* Progression limits, daily and cumulative loss limits stop staking (stake 0) until a human
  resets the sequence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal

from sports_arb.quant.registry import ValidationRegistry
from sports_arb.quant.sizing import kelly_stake

ZERO = Decimal("0")
METHODS = (
    "flat",
    "kelly",
    "adaptive",
    "martingale",
    "capped_martingale",
    "fractional_martingale",
    "paroli",
)
PROGRESSION = {"martingale", "capped_martingale", "fractional_martingale", "paroli", "adaptive"}
RESET_PHRASE = "AUTHORIZE STAKING RESET"


@dataclass(frozen=True, slots=True)
class StakingConfig:
    method: str = "flat"
    base_stake: Decimal = Decimal("1")
    multiplier: Decimal = Decimal("2")  # martingale / paroli growth factor
    fractional_step: Decimal = Decimal("0.5")  # fractional_martingale: 1 + f*(m-1) per step
    max_steps: int = 3  # progression limit (steps beyond the base)
    max_stake: Decimal = Decimal("5")
    max_fraction: float = 0.05  # of bankroll
    kelly_multiplier: float = 0.25
    paroli_wins: int = 3
    daily_loss_limit: Decimal = Decimal("5")
    cumulative_loss_limit: Decimal = Decimal("15")

    def __post_init__(self) -> None:
        if self.method not in METHODS:
            raise ValueError(f"unknown staking method {self.method!r}")
        if self.max_steps < 0 or self.base_stake <= ZERO or self.max_stake <= ZERO:
            raise ValueError("invalid staking limits")


@dataclass(frozen=True, slots=True)
class StakeContext:
    bankroll: Decimal
    price: Decimal  # all-in cost per $1 payoff (price + fee)
    win_prob_lower: float
    edge_validated: bool  # decision engine: approved strategy AND lower-bound EV > 0
    central_cap: Decimal  # risk manager ceiling for THIS trade
    sample_size: int = 0


@dataclass(frozen=True, slots=True)
class StakeDecision:
    stake: Decimal
    reason: str
    step: int = 0
    recovery_target: Decimal = ZERO


@dataclass(slots=True)
class StakingEngine:
    config: StakingConfig
    registry: ValidationRegistry | None = None
    step: int = 0
    win_run: int = 0
    sequence_loss: Decimal = ZERO
    daily_pnl: Decimal = ZERO
    cumulative_pnl: Decimal = ZERO
    halted: bool = False
    history: list[tuple[str, Decimal]] = field(default_factory=list)  # (W/L, pnl)

    def _progression_approved(self) -> bool:
        m = self.config.method
        if m not in PROGRESSION:
            return True
        return self.registry is not None and self.registry.approved(f"staking:{m}")

    def next_stake(self, ctx: StakeContext) -> StakeDecision:
        c = self.config
        if self.halted:
            return StakeDecision(ZERO, "halted_needs_reset", self.step)
        if not ctx.edge_validated:
            return StakeDecision(ZERO, "no_validated_positive_edge")
        if not self._progression_approved():
            return StakeDecision(ZERO, f"staking_{c.method}_not_approved")
        if -self.daily_pnl >= c.daily_loss_limit:
            return StakeDecision(ZERO, "daily_loss_limit")
        if -self.cumulative_pnl >= c.cumulative_loss_limit:
            self.halted = True
            return StakeDecision(ZERO, "cumulative_loss_limit", self.step)
        cap = min(ctx.central_cap, c.max_stake, ctx.bankroll * Decimal(str(c.max_fraction)))
        k = ctx.price
        if c.method == "flat":
            raw = c.base_stake
        elif c.method in {"kelly", "adaptive"}:
            scale = 1.0 if c.method == "kelly" else min(1.0, ctx.sample_size / 200)
            res = kelly_stake(
                win_prob_lower=ctx.win_prob_lower,
                price=k,
                fee_per_share=ZERO,
                bankroll=ctx.bankroll,
                kelly_multiplier=c.kelly_multiplier * scale,
                max_fraction=c.max_fraction,
            )
            if res.stake == ZERO:
                return StakeDecision(ZERO, res.reason)
            raw = res.stake
        elif c.method in {"martingale", "capped_martingale", "fractional_martingale"}:
            if self.step > c.max_steps:
                self.halted = True
                return StakeDecision(ZERO, "progression_limit", self.step)
            growth = (
                Decimal("1") + c.fractional_step * (c.multiplier - 1)
                if c.method == "fractional_martingale"
                else c.multiplier
            )
            raw = c.base_stake * growth**self.step
            if c.method == "capped_martingale":
                raw = min(raw, c.max_stake)
        else:  # paroli: press winners, never chase losses
            raw = c.base_stake * c.multiplier ** min(self.win_run, c.paroli_wins)
        stake = min(raw, cap).quantize(Decimal("0.000001"))
        if stake <= ZERO:
            return StakeDecision(ZERO, "below_minimum", self.step)
        if c.method in {"martingale", "capped_martingale", "fractional_martingale"} and stake < raw:
            # risk cap truncated the progression: it can no longer reach its recovery target
            self.halted = True
            return StakeDecision(ZERO, "risk_cap_truncates_progression", self.step)
        target = self.sequence_loss + c.base_stake
        return StakeDecision(stake, "ok", self.step, target)

    def record(self, won: bool, pnl: Decimal) -> None:
        self.history.append(("W" if won else "L", pnl))
        self.daily_pnl += pnl
        self.cumulative_pnl += pnl
        if won:
            self.win_run += 1
            self.step = 0
            self.sequence_loss = ZERO
        else:
            self.win_run = 0
            self.step += 1
            self.sequence_loss += -pnl

    def new_day(self) -> None:
        self.daily_pnl = ZERO

    def reset(self, phrase: str) -> None:
        if phrase != RESET_PHRASE:
            raise PermissionError("staking reset requires the authorization phrase")
        self.step, self.win_run, self.sequence_loss, self.halted = 0, 0, ZERO, False

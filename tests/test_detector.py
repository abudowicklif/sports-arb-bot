from decimal import Decimal

from helpers import book, cache_with, market

from sports_arb.detector import DetectorConfig, SameMarketDetector


def config(**overrides):
    values = {
        "min_shares": Decimal("1"),
        "max_shares": Decimal("100"),
        "max_capital": Decimal("1000"),
        "min_net_edge_bps": Decimal("0"),
        "min_net_profit": Decimal("0"),
        "slippage_bps": Decimal("0"),
        "max_book_age_ms": 5000,
        "max_pair_skew_ms": 1000,
    }
    values.update(overrides)
    return DetectorConfig(**values)


def test_complete_set_profit_uses_both_ask_ladders():
    m = market(fee_rate="0")
    cache = cache_with(
        book("101", [("0.48", "100")]),
        book("202", [("0.47", "100")]),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1001)
    assert result.status == "qualifying"
    opp = result.opportunity
    assert opp.shares == Decimal("100")
    assert opp.total_cost == Decimal("95.00")
    assert opp.net_profit == Decimal("5.00")


def test_sports_taker_fees_reduce_edge():
    m = market(fee_rate="0.05")
    cache = cache_with(
        book("101", [("0.48", "100")]),
        book("202", [("0.47", "100")]),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1001)
    opp = result.opportunity
    # 100*.05*(.48*.52 + .47*.53) = 2.4935
    assert opp.estimated_fees == Decimal("2.493500")
    assert opp.net_profit == Decimal("2.506500")


def test_gross_zero_edge_is_loss_after_fees():
    m = market(fee_rate="0.05")
    cache = cache_with(
        book("101", [("0.50", "100")]),
        book("202", [("0.50", "100")]),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1001)
    assert result.status == "below_threshold"
    # With a negative marginal edge the optimizer keeps only the minimum size.
    assert result.opportunity.shares == Decimal("1")
    assert result.opportunity.net_profit == Decimal("-0.025000")


def test_depth_walk_selects_profitable_size_not_maximum_size():
    m = market()
    cache = cache_with(
        book("101", [("0.45", "50"), ("0.55", "50")]),
        book("202", [("0.45", "100")]),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1001)
    assert result.opportunity.shares == Decimal("50")
    assert result.opportunity.net_profit == Decimal("5.00")


def test_capital_limit_adds_fractional_boundary_candidate():
    m = market()
    cache = cache_with(
        book("101", [("0.40", "1000")]),
        book("202", [("0.40", "1000")]),
    )
    detector = SameMarketDetector(config(max_capital=Decimal("50")))
    result = detector.evaluate(m, cache, now=1001)
    assert abs(result.opportunity.shares - Decimal("62.5")) <= Decimal("0.000001")
    assert result.opportunity.total_cost <= Decimal("50.0")


def test_slippage_reserve_can_reject_apparent_edge():
    m = market()
    cache = cache_with(
        book("101", [("0.49", "100")]),
        book("202", [("0.49", "100")]),
    )
    detector = SameMarketDetector(
        config(slippage_bps=Decimal("300"), min_net_profit=Decimal("0.01"))
    )
    result = detector.evaluate(m, cache, now=1001)
    assert result.status == "below_threshold"
    assert result.opportunity.net_profit < 0


def test_stale_books_are_not_evaluated():
    m = market()
    cache = cache_with(
        book("101", [("0.40", "100")], timestamp=900),
        book("202", [("0.40", "100")], timestamp=900),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1001)
    assert result.status == "stale_book"
    assert result.opportunity is None


def test_pair_timestamp_skew_is_rejected():
    m = market()
    cache = cache_with(
        book("101", [("0.40", "100")], timestamp=1000),
        book("202", [("0.40", "100")], timestamp=1002),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1002.1)
    assert result.status == "skewed_book"


def test_minimum_order_size_is_enforced():
    m = market(min_size="20")
    cache = cache_with(
        book("101", [("0.40", "10")]),
        book("202", [("0.40", "10")]),
    )
    result = SameMarketDetector(config()).evaluate(m, cache, now=1001)
    assert result.status == "insufficient_depth"

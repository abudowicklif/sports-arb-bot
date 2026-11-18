from sports_arb.gamma import GammaClient


def raw_market(market_id, condition_id, market_type, volume):
    return {
        "id": market_id,
        "conditionId": condition_id,
        "slug": f"market-{market_id}",
        "question": f"Market {market_id}",
        "version": "v1",
        "sportsMarketType": market_type,
        "outcomes": '["A", "B"]',
        "clobTokenIds": f'["{market_id}1", "{market_id}2"]',
        "active": True,
        "closed": False,
        "acceptingOrders": True,
        "enableOrderBook": True,
        "feesEnabled": False,
        "volume24hr": volume,
    }


class FakeHttp:
    def __init__(self):
        self.calls = []

    async def request_json(self, method, path, *, params=None, body=None):
        self.calls.append(params.copy())
        market_type = params["sports_market_types"]
        if market_type == "moneyline":
            rows = [raw_market("10", "condition-a", market_type, 5)]
        else:
            rows = [raw_market("20", "condition-b", market_type, 10)]
        return {"markets": rows, "next_cursor": None}


def test_market_types_are_queried_separately_then_volume_ranked():
    import asyncio

    http = FakeHttp()
    client = GammaClient(http)
    result = asyncio.run(
        client.discover_sports_markets(market_types=("moneyline", "spreads"), max_markets=10)
    )
    assert [call["sports_market_types"] for call in http.calls] == [
        "moneyline",
        "spreads",
    ]
    assert [item.market_id for item in result] == ["20", "10"]

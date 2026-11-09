import pytest

from worldbrief import markets


@pytest.fixture(autouse=True)
def no_live_market_prices(monkeypatch):
    """Tests never reach Yahoo: every price request answers with nothing."""
    monkeypatch.setattr(markets, "_get", lambda symbols, interval: [])

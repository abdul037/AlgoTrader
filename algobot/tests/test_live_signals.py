from __future__ import annotations

from fastapi.testclient import TestClient

from app.main import create_app
from app.models.live_signal import MarketQuote
from tests.conftest import MockBroker, make_settings


class FakeMarketDataClient:
    def __init__(self, frames: dict[str, object], quotes: dict[str, MarketQuote]):
        self.frames = frames
        self.quotes = quotes

    def get_daily_candles(self, symbol: str, *, candles_count: int = 250, direction: str = "desc", interval: str = "OneDay"):
        return self.frames[symbol.upper()].copy()

    def get_rates(self, symbols: list[str]):
        return {symbol.upper(): self.quotes[symbol.upper()] for symbol in symbols}


class FakeTelegramNotifier:
    def __init__(self) -> None:
        self.messages: list[tuple[str, str | None]] = []

    def send_signal_change(self, snapshot, *, previous_state=None):
        self.messages.append((snapshot.symbol, previous_state))
        return True

    def send_text(self, message: str):
        self.messages.append((message, "text"))
        return True


def _equity_frame(closes: list[float]):
    import pandas as pd

    rows = []
    base = pd.Timestamp("2025-01-01T00:00:00Z")
    for index, close in enumerate(closes):
        rows.append(
            {
                "timestamp": base + pd.Timedelta(days=index),
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1_000_000 + index,
            }
        )
    return pd.DataFrame(rows)


def test_latest_signal_endpoint_returns_live_snapshot(tmp_path) -> None:
    closes = [100 + (index * 0.4) for index in range(100)]
    closes += [136.0, 134.0, 132.0, 131.0, 133.5]
    frame = _equity_frame(closes)
    market_data = FakeMarketDataClient(
        {"NVDA": frame},
        {
            "NVDA": MarketQuote(
                symbol="NVDA",
                instrument_id=1137,
                bid=133.4,
                ask=133.6,
                last_execution=133.5,
                timestamp="2026-04-10T12:00:00Z",
            )
        },
    )
    notifier = FakeTelegramNotifier()
    settings = make_settings(
        tmp_path,
        signal_scan_universe=["NVDA"],
        allowed_instruments=["NVDA", "GOOG", "GOOGL", "AMD", "MU", "GOLD"],
    )
    app = create_app(settings, broker=MockBroker(), market_data_client=market_data, telegram_notifier=notifier)
    client = TestClient(app)

    response = client.get("/signals/latest", params={"symbol": "NVDA"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["symbol"] == "NVDA"
    assert payload["strategy_name"] == "pullback_trend_100_10"
    assert payload["state"] in {"buy", "none", "sell"}
    assert payload["current_price"] == 133.5
    assert "trend_ma" in payload["indicators"]


def test_signal_scan_limits_and_notifies_only_on_change(tmp_path) -> None:
    bullish = _equity_frame([100 + (index * 0.5) for index in range(100)] + [150.0, 147.0, 145.0, 144.0, 146.5])
    neutral = _equity_frame([50 + (index * 0.1) for index in range(105)])
    market_data = FakeMarketDataClient(
        {"NVDA": bullish, "AAPL": neutral},
        {
            "NVDA": MarketQuote(symbol="NVDA", instrument_id=1137, bid=146.4, ask=146.6, last_execution=146.5, timestamp="2026-04-10T12:00:00Z"),
            "AAPL": MarketQuote(symbol="AAPL", instrument_id=1001, bid=60.4, ask=60.6, last_execution=60.5, timestamp="2026-04-10T12:00:00Z"),
        },
    )
    notifier = FakeTelegramNotifier()
    settings = make_settings(
        tmp_path,
        signal_scan_universe=["NVDA", "AAPL"],
        allowed_instruments=["NVDA", "GOOG", "GOOGL", "AMD", "MU", "GOLD"],
    )
    app = create_app(settings, broker=MockBroker(), market_data_client=market_data, telegram_notifier=notifier)
    client = TestClient(app)

    first = client.get("/signals/scan", params={"limit": 1, "notify": "true"})
    assert first.status_code == 200
    payload = first.json()
    assert payload["limit"] == 1
    assert len(payload["candidates"]) == 1
    assert payload["alerts_sent"] >= 1
    assert len(notifier.messages) >= 1

    second = client.get("/signals/scan", params={"limit": 2, "notify": "true"})
    assert second.status_code == 200
    assert second.json()["alerts_sent"] == 0


def test_manual_telegram_endpoints(tmp_path) -> None:
    frame = _equity_frame([100 + (index * 0.4) for index in range(100)] + [136.0, 134.0, 132.0, 131.0, 133.5])
    market_data = FakeMarketDataClient(
        {"NVDA": frame},
        {
            "NVDA": MarketQuote(
                symbol="NVDA",
                instrument_id=1137,
                bid=133.4,
                ask=133.6,
                last_execution=133.5,
                timestamp="2026-04-10T12:00:00Z",
            )
        },
    )
    notifier = FakeTelegramNotifier()
    settings = make_settings(
        tmp_path,
        signal_scan_universe=["NVDA"],
        allowed_instruments=["NVDA", "GOOG", "GOOGL", "AMD", "MU", "GOLD"],
    )
    app = create_app(settings, broker=MockBroker(), market_data_client=market_data, telegram_notifier=notifier)
    client = TestClient(app)

    test_response = client.post("/signals/test-telegram", json={"message": "manual test"})
    assert test_response.status_code == 200
    assert test_response.json()["sent"] is True

    notify_response = client.post("/signals/notify", json={"symbol": "NVDA"})
    assert notify_response.status_code == 200
    assert notify_response.json()["sent"] is True
    assert len(notifier.messages) >= 2

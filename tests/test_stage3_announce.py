"""One-time Stage 3-ready alert: fires once when the gates are first met."""

from __future__ import annotations

from types import SimpleNamespace

from app.workflow.operations import STAGE3_ANNOUNCED_KEY, _maybe_announce_stage3_ready


def _trade(pnl: float, day: str):
    return SimpleNamespace(realized_pnl_usd=pnl, closed_at=f"{day}T15:00:00Z", payload={})


class _Runtime:
    def __init__(self):
        self.store: dict[str, str] = {}
    def get(self, key):
        return self.store.get(key)
    def set(self, key, value):
        self.store[key] = value


def _service(trades, *, ready_gates=True):
    sent = []
    runtime = _Runtime()
    # Loosen gates via settings so a modest synthetic record is "ready".
    settings = SimpleNamespace(paper_account_balance_usd=100_000.0, daily_profit_target_usd=1000.0)
    svc = SimpleNamespace(
        runtime_state=runtime,
        settings=settings,
        paper_trading=SimpleNamespace(trades=SimpleNamespace(list=lambda limit=5000: trades)),
        notifier=SimpleNamespace(send_text=lambda msg: sent.append(msg) or True),
    )
    return svc, sent, runtime


def _ready_record():
    # 120 winning days -> clears 60-day / 100-trade / Sharpe / DD / expectancy gates.
    return [_trade(150.0, f"2026-{(i//28)+1:02d}-{(i%28)+1:02d}") for i in range(120)]


def test_fires_once_when_ready() -> None:
    svc, sent, runtime = _service(_ready_record())
    assert _maybe_announce_stage3_ready(svc) is True
    assert len(sent) == 1 and "Stage 3 reached" in sent[0]
    assert runtime.get(STAGE3_ANNOUNCED_KEY)
    # Second call is a no-op (already announced).
    assert _maybe_announce_stage3_ready(svc) is False
    assert len(sent) == 1


def test_silent_when_not_ready() -> None:
    svc, sent, _ = _service([_trade(10.0, "2026-01-01")])  # far too few trades/days
    assert _maybe_announce_stage3_ready(svc) is False
    assert sent == []


def test_silent_when_no_trades() -> None:
    svc, sent, _ = _service([])
    assert _maybe_announce_stage3_ready(svc) is False
    assert sent == []

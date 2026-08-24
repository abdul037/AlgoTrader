from __future__ import annotations

from types import SimpleNamespace

from app.automation.unattended import AUTO_TRADING_ANNOUNCED_KEY, PaperAutoTradingService


class _State:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.values.get(key)

    def set(self, key: str, value: str) -> None:
        self.values[key] = value


class _Notifier:
    def __init__(self) -> None:
        self.msgs: list[str] = []

    def send_text(self, message: str) -> bool:
        self.msgs.append(message)
        return True


class _Logs:
    def log(self, *args, **kwargs) -> None:
        pass


def _service(mode: str, state) -> PaperAutoTradingService:
    return PaperAutoTradingService(
        settings=SimpleNamespace(paper_auto_operation_mode=mode),
        proposal_service=None,
        execution_coordinator=None,
        automation=None,
        reconciliation=None,
        safety_state=None,
        executions=None,
        run_logs=_Logs(),
        notifier=_Notifier(),
        alpaca_client=None,
        runtime_state=state,
    )


_PROCESSED = SimpleNamespace(symbol="NVDA", id="q1", status="submitted")


def test_announces_once_in_unattended() -> None:
    state = _State()
    svc = _service("unattended", state)

    assert svc._announce_auto_trading_started(_PROCESSED) is True
    assert any("ACTIVE" in m for m in svc.notifier.msgs)
    assert state.get(AUTO_TRADING_ANNOUNCED_KEY)

    svc.notifier.msgs.clear()
    assert svc._announce_auto_trading_started(_PROCESSED) is False  # already announced
    assert svc.notifier.msgs == []


def test_no_announce_in_shadow_mode() -> None:
    svc = _service("shadow", _State())
    assert svc._announce_auto_trading_started(_PROCESSED) is False
    assert svc.notifier.msgs == []


def test_no_announce_without_runtime_state() -> None:
    svc = _service("unattended", None)
    assert svc._announce_auto_trading_started(_PROCESSED) is False
    assert svc.notifier.msgs == []

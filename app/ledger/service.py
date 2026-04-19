"""Ledger orchestration — snapshot, match alerts to positions, track closures."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from app.broker.etoro_client import EToroClient
from app.broker.instrument_resolver import InstrumentResolver
from app.ledger.repository import LedgerRepository
from app.runtime_settings import AppSettings
from app.storage.db import Database
from app.utils.time import utc_now

logger = logging.getLogger(__name__)


# When a position disappears from the live portfolio, we assume it closed
# somewhere between the prior snapshot and the current one. This is an
# approximation: for exact fills we'd need eToro's closed-positions history
# (not probed yet; see scripts/verify_etoro_demo.py).
DEFAULT_MATCH_WINDOW_MINUTES = 120
DEFAULT_PENDING_EXPIRY_HOURS = 48


class LedgerService:
    """Snapshot the real eToro portfolio and link it to bot alerts."""

    def __init__(
        self,
        *,
        settings: AppSettings,
        broker: EToroClient,
        repository: LedgerRepository,
        database: Database | None = None,
    ):
        self.settings = settings
        self.broker = broker
        self.repository = repository
        self.db = database
        # Forward map: symbol -> instrument_id. Lazily populated.
        self._symbol_to_instrument_id: dict[str, int] = {}
        self._resolver = InstrumentResolver(settings)

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run_cycle(self) -> dict[str, Any]:
        """One full pass: snapshot -> match -> refresh closed -> expire stale."""
        expired = self.expire_stale_pending(older_than_hours=int(self.settings.ledger_pending_expiry_hours))
        pending = self.repository.list_pending_match()
        open_outcomes = self.repository.list_open_outcomes()
        manual_tracking = bool(getattr(self.settings, "ledger_track_manual_positions_enabled", False))

        if not pending and not open_outcomes and not manual_tracking:
            summary = {
                "snapshot_ts": utc_now().isoformat(),
                "positions_seen": 0,
                "matched_new": 0,
                "manual_imported": 0,
                "closed_new": 0,
                "expired_pending": expired,
                "skipped": True,
                "skip_reason": "no_active_ledger_work",
            }
            logger.info("Ledger cycle skipped: %s", summary)
            return summary

        pending_symbols = {
            str(row.get("symbol") or "").upper()
            for row in pending
            if row.get("symbol")
        }
        snapshot = self.snapshot_portfolio(
            candidate_symbols=pending_symbols,
            resolve_all_symbols=manual_tracking,
        )
        matched = self.match_open_positions(match_window_minutes=int(self.settings.ledger_match_window_minutes))
        manual_imported = (
            self.import_new_manual_positions(snapshot)
            if manual_tracking
            else 0
        )
        closed = self.refresh_closed_outcomes()
        summary = {
            "snapshot_ts": snapshot["snapshot_ts"],
            "positions_seen": snapshot["position_count"],
            "matched_new": matched,
            "manual_imported": manual_imported,
            "closed_new": closed,
            "expired_pending": expired,
        }
        logger.info("Ledger cycle complete: %s", summary)
        return summary

    # ------------------------------------------------------------------
    # 1. Snapshot
    # ------------------------------------------------------------------

    def snapshot_portfolio(
        self,
        *,
        candidate_symbols: set[str] | None = None,
        resolve_all_symbols: bool = False,
    ) -> dict[str, Any]:
        """Poll eToro, persist one snapshot row, return the snapshot dict."""
        raw = self.broker.fetch_raw_portfolio()
        client_portfolio = raw.get("clientPortfolio", {}) or {}
        raw_positions = client_portfolio.get("positions", []) or []
        credit = _safe_float(client_portfolio.get("credit"))

        # Normalize each position into a compact dict the ledger understands.
        positions: list[dict[str, Any]] = []
        for item in raw_positions:
            instrument_id = _safe_int(item.get("instrumentID"))
            position_id = _safe_int(item.get("positionID"))
            if instrument_id is None or position_id is None:
                continue
            positions.append(
                {
                    "position_id": position_id,
                    "instrument_id": instrument_id,
                    "symbol": self._resolve_symbol_for_id(
                        instrument_id,
                        candidate_symbols=candidate_symbols,
                        resolve_all=resolve_all_symbols,
                    ),
                    "is_buy": bool(item.get("isBuy", True)),
                    "leverage": _safe_int(item.get("leverage")) or 1,
                    "open_rate": _safe_float(item.get("openRate")) or 0.0,
                    "units": _safe_float(item.get("units")) or 0.0,
                    "amount_usd": _safe_float(item.get("amount")) or _safe_float(
                        item.get("initialAmountInDollars")
                    ),
                    "open_datetime": item.get("openDateTime"),
                    "stop_loss_rate": _safe_float(item.get("stopLossRate")),
                    "take_profit_rate": _safe_float(item.get("takeProfitRate")),
                    "total_fees": _safe_float(item.get("totalFees")),
                }
            )

        snapshot_ts = utc_now().isoformat()
        row_id = self.repository.insert_snapshot(
            snapshot_ts=snapshot_ts,
            positions=positions,
            credit=credit,
            unrealized_pnl_usd=None,
            raw_payload=raw if len(raw_positions) <= 50 else None,
        )

        return {
            "id": row_id,
            "snapshot_ts": snapshot_ts,
            "position_count": len(positions),
            "positions": positions,
            "credit": credit,
        }

    # ------------------------------------------------------------------
    # 2. Record an alert (called from the Telegram/screener code path)
    # ------------------------------------------------------------------

    def record_alert(
        self,
        *,
        symbol: str,
        strategy_name: str | None,
        timeframe: str | None,
        alert_created_at: str | datetime,
        alert_entry_price: float | None = None,
        alert_stop: float | None = None,
        alert_target: float | None = None,
        alert_score: float | None = None,
        alert_source: str = "screener",
        alert_id: str | None = None,
        alert_payload: dict[str, Any] | None = None,
    ) -> int:
        """Record a new alert. Returns the outcome id (pending_match)."""
        if isinstance(alert_created_at, datetime):
            alert_created_at = alert_created_at.astimezone(timezone.utc).isoformat()
        if alert_id:
            existing = self.repository.get_by_alert_id(alert_id)
            if existing is not None:
                return int(existing["id"])
        outcome_id = self.repository.insert_outcome(
            alert_source=alert_source,
            alert_id=alert_id,
            symbol=symbol,
            strategy_name=strategy_name,
            timeframe=timeframe,
            alert_created_at=alert_created_at,
            alert_entry_price=alert_entry_price,
            alert_stop=alert_stop,
            alert_target=alert_target,
            alert_score=alert_score,
            alert_payload=alert_payload,
        )
        return outcome_id

    # ------------------------------------------------------------------
    # 3. Match pending_match outcomes to open positions
    # ------------------------------------------------------------------

    def match_open_positions(
        self, *, match_window_minutes: int = DEFAULT_MATCH_WINDOW_MINUTES
    ) -> int:
        """Link pending_match outcomes to open eToro positions.

        An outcome matches a position when:
        - the position's symbol equals the alert's symbol
        - the position was opened within ``match_window_minutes`` of the alert
        - the position is not yet linked to another outcome

        Returns number of new matches recorded.
        """
        snapshot = self.repository.latest_snapshot()
        if snapshot is None:
            return 0
        positions: list[dict[str, Any]] = snapshot.get("positions", []) or []
        if not positions:
            return 0

        pending = self.repository.list_pending_match()
        if not pending:
            return 0

        matches_made = 0
        for outcome in pending:
            alert_ts = _parse_iso(outcome.get("alert_created_at"))
            if alert_ts is None:
                continue
            symbol = (outcome.get("symbol") or "").upper()
            window_start = alert_ts - timedelta(minutes=5)  # a little forgiveness for clock drift
            window_end = alert_ts + timedelta(minutes=match_window_minutes)

            best: dict[str, Any] | None = None
            best_delta: timedelta | None = None
            for pos in positions:
                if (pos.get("symbol") or "").upper() != symbol:
                    continue
                position_id = pos.get("position_id")
                if position_id is None:
                    continue
                if self.repository.position_already_matched(int(position_id)):
                    continue
                open_ts = _parse_iso(pos.get("open_datetime"))
                if open_ts is None:
                    continue
                if open_ts < window_start or open_ts > window_end:
                    continue
                delta = abs(open_ts - alert_ts)
                if best_delta is None or delta < best_delta:
                    best = pos
                    best_delta = delta

            if best is None:
                continue

            self.repository.mark_matched(
                outcome_id=int(outcome["id"]),
                position_id=int(best["position_id"]),
                position_open_at=best.get("open_datetime"),
                position_open_rate=float(best.get("open_rate") or 0.0),
                position_amount_usd=_safe_float(best.get("amount_usd")),
                position_units=float(best.get("units") or 0.0),
                position_is_buy=bool(best.get("is_buy", True)),
                position_leverage=int(best.get("leverage") or 1),
                position_stop_loss_rate=_safe_float(best.get("stop_loss_rate")),
                position_take_profit_rate=_safe_float(best.get("take_profit_rate")),
            )
            matches_made += 1

        return matches_made

    def import_new_manual_positions(self, snapshot: dict[str, Any] | None = None) -> int:
        """Import newly-opened manual eToro positions as ledger outcomes.

        This is read-only account observation. It deliberately runs after
        alert-to-position matching so bot-generated alerts remain measurable as
        ``alert_source='screener'`` while manual trades are separated under
        ``alert_source='manual_etoro'``.
        """
        latest = snapshot or self.repository.latest_snapshot()
        if latest is None:
            return 0

        previous = self.repository.snapshot_before(str(latest["snapshot_ts"]))
        if previous is None:
            # First observed snapshot becomes the baseline. Do not backfill the
            # whole existing account unless the operator explicitly asks.
            return 0

        previous_ids = {
            int(position["position_id"])
            for position in (previous.get("positions") or [])
            if position.get("position_id") is not None
        }
        imported = 0
        for position in latest.get("positions") or []:
            position_id = position.get("position_id")
            if position_id is None:
                continue
            position_id = int(position_id)
            if position_id in previous_ids:
                continue
            if self.repository.position_already_matched(position_id):
                continue

            symbol = str(position.get("symbol") or position.get("instrument_id") or "").upper()
            if not symbol:
                continue
            alert_id = f"manual_etoro:{position_id}"
            outcome_id = self.record_alert(
                symbol=symbol,
                strategy_name="manual_etoro",
                timeframe=None,
                alert_created_at=position.get("open_datetime") or latest["snapshot_ts"],
                alert_entry_price=_safe_float(position.get("open_rate")),
                alert_stop=_safe_float(position.get("stop_loss_rate")),
                alert_target=_safe_float(position.get("take_profit_rate")),
                alert_score=None,
                alert_source="manual_etoro",
                alert_id=alert_id,
                alert_payload={
                    "auto_imported": True,
                    "position_id": position_id,
                    "instrument_id": position.get("instrument_id"),
                    "snapshot_ts": latest["snapshot_ts"],
                    "note": "New eToro position imported by ledger polling. No trade action was taken.",
                },
            )
            self.repository.mark_matched(
                outcome_id=outcome_id,
                position_id=position_id,
                position_open_at=position.get("open_datetime"),
                position_open_rate=float(position.get("open_rate") or 0.0),
                position_amount_usd=_safe_float(position.get("amount_usd")),
                position_units=float(position.get("units") or 0.0),
                position_is_buy=bool(position.get("is_buy", True)),
                position_leverage=int(position.get("leverage") or 1),
                position_stop_loss_rate=_safe_float(position.get("stop_loss_rate")),
                position_take_profit_rate=_safe_float(position.get("take_profit_rate")),
            )
            imported += 1

        return imported

    # ------------------------------------------------------------------
    # 4. Detect closures and score outcome
    # ------------------------------------------------------------------

    def refresh_closed_outcomes(self) -> int:
        """For every open outcome whose position has disappeared, mark it closed.

        Close price is approximated from the last snapshot that still contained
        the position (good enough for expectancy/win-rate; exact fill requires
        the /positions/history endpoint which isn't probed yet).
        """
        latest = self.repository.latest_snapshot()
        if latest is None:
            return 0
        latest_ids = {
            int(p["position_id"])
            for p in (latest.get("positions") or [])
            if p.get("position_id") is not None
        }
        open_outcomes = self.repository.list_open_outcomes()
        closed = 0

        for outcome in open_outcomes:
            pid = outcome.get("matched_position_id")
            if pid is None or int(pid) in latest_ids:
                continue  # still live

            # The position vanished — find the most recent snapshot that still
            # had it and use that rate as our close price.
            close_snapshot, last_position = self._find_last_sighting(int(pid))
            close_rate = (
                float(last_position.get("open_rate") or 0.0)
                if last_position
                else float(outcome.get("position_open_rate") or 0.0)
            )
            # NOTE: in future we can enrich close_rate with a final market-data
            # rate at the disappearance timestamp. For now we use the last
            # seen position's open rate as a safe fallback.

            closed_at = (
                close_snapshot.get("snapshot_ts")
                if close_snapshot
                else latest.get("snapshot_ts")
            )
            entry = float(outcome.get("position_open_rate") or 0.0)
            units = float(outcome.get("position_units") or 0.0)
            is_buy = bool(outcome.get("position_is_buy"))

            # Realized PnL (gross, excludes fees — fees available via totalFees)
            direction = 1 if is_buy else -1
            realized_pnl = (close_rate - entry) * units * direction

            # R-multiple vs the *alert's* declared stop (this is the thing we
            # care about for signal quality, not the user's manual SL).
            alert_entry = _safe_float(outcome.get("alert_entry_price")) or entry
            alert_stop = _safe_float(outcome.get("alert_stop"))
            r_mult: float | None = None
            if alert_stop is not None and alert_entry is not None:
                risk_per_unit = abs(alert_entry - alert_stop)
                if risk_per_unit > 0:
                    move_per_unit = (close_rate - alert_entry) * direction
                    r_mult = move_per_unit / risk_per_unit

            outcome_status = self._classify_outcome(
                entry=alert_entry,
                close=close_rate,
                stop=alert_stop,
                target=_safe_float(outcome.get("alert_target")),
                is_buy=is_buy,
            )

            self.repository.mark_closed(
                outcome_id=int(outcome["id"]),
                closed_at=closed_at,
                close_rate=close_rate,
                realized_pnl_usd=realized_pnl,
                realized_r_multiple=r_mult,
                outcome_status=outcome_status,
            )
            closed += 1

        return closed

    # ------------------------------------------------------------------
    # 5. Housekeeping
    # ------------------------------------------------------------------

    def expire_stale_pending(
        self, *, older_than_hours: int = DEFAULT_PENDING_EXPIRY_HOURS
    ) -> int:
        """Pending_match outcomes that never got a matching position become
        'expired_unmatched' after this many hours."""
        cutoff = (utc_now() - timedelta(hours=older_than_hours)).isoformat()
        return self.repository.expire_stale_pending(cutoff)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _resolve_symbol_for_id(
        self,
        instrument_id: int,
        *,
        candidate_symbols: set[str] | None = None,
        resolve_all: bool = False,
    ) -> str:
        """Best-effort reverse lookup of instrument_id -> symbol."""
        # Quick path: cache
        for symbol, iid in self._symbol_to_instrument_id.items():
            if iid == instrument_id:
                return symbol

        if not resolve_all and not candidate_symbols:
            return str(instrument_id)

        supported = self._resolver.list_supported()
        candidates = (
            {symbol.upper() for symbol in candidate_symbols}
            if candidate_symbols is not None
            else {instrument.symbol.upper() for instrument in supported}
        )

        # Ask the broker to resolve only the symbols needed for active pending
        # alerts unless manual-position importing explicitly needs full symbols.
        for instrument in supported:
            sym = instrument.symbol.upper()
            if sym not in candidates:
                continue
            if sym in self._symbol_to_instrument_id:
                continue
            try:
                resolved = self.broker._search_instrument(sym)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001
                continue
            self._symbol_to_instrument_id[sym] = int(resolved["instrument_id"])
            if int(resolved["instrument_id"]) == instrument_id:
                return sym

        for symbol, iid in self._symbol_to_instrument_id.items():
            if iid == instrument_id:
                return symbol
        return str(instrument_id)

    def _find_last_sighting(
        self, position_id: int
    ) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        """Find the most recent snapshot that still contained position_id."""
        # Cheap linear scan: walk backwards from latest until we find the position
        # or exhaust snapshots. For high-volume use, index by position_id.
        if self.db is None:
            return None, None
        with self.db.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM portfolio_snapshots ORDER BY snapshot_ts DESC LIMIT 500"
            ).fetchall()
        import json as _json

        for row in rows:
            try:
                positions = _json.loads(row["positions_json"] or "[]")
            except Exception:
                continue
            for p in positions:
                if int(p.get("position_id") or 0) == position_id:
                    return dict(row), p
        return None, None

    @staticmethod
    def _classify_outcome(
        *,
        entry: float,
        close: float,
        stop: float | None,
        target: float | None,
        is_buy: bool,
    ) -> str:
        """Tag the outcome as target_hit / stop_hit / closed_manual."""
        if stop is not None and target is not None:
            if is_buy:
                if close <= stop:
                    return "stop_hit"
                if close >= target:
                    return "target_hit"
            else:
                if close >= stop:
                    return "stop_hit"
                if close <= target:
                    return "target_hit"
        return "closed_manual"


# ----------------------------------------------------------------------
# Small parsing helpers
# ----------------------------------------------------------------------


def _safe_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return None


def _parse_iso(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    except Exception:  # noqa: BLE001
        return None

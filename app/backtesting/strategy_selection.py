"""Strategy-selection helpers shared by the screener and the batch backtester.

Historically these helpers lived inside ``app/screener/service.py`` as private
functions. Pulling them up to the backtesting package lets
``BatchBacktestService`` live where it belongs (``app.backtesting.batch``)
without creating a circular import.

No behaviour change: the functions are moved verbatim and given public names.
The screener still imports them from here; everything else stays the same.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.strategies import get_strategy_specs

if TYPE_CHECKING:
    from app.screener.regime_router import RegimeSignal


def active_strategy_names(settings: Any, *, requested: set[str] | None = None) -> set[str] | None:
    """Return the set of strategy names the caller should run.

    ``None`` means "all specs for this timeframe"; a set means filter to these
    names only. Kept backward-compatible with the private version from the
    screener module.
    """

    if requested:
        return {item.strip().lower() for item in requested if item.strip()}
    configured = {
        item.strip().lower()
        for item in getattr(settings, "screener_active_strategy_names", []) or []
        if item.strip()
    }
    if not configured or "all" in configured:
        return None
    return configured


def strategy_specs_for(
    settings: Any,
    *,
    timeframe: str,
    requested: set[str] | None = None,
    regime: RegimeSignal | None = None,
) -> list[Any]:
    """Return the list of strategy specs active for ``timeframe``.

    When ``settings.regime_router_enabled`` is set and a ``regime`` is supplied,
    the result is further narrowed to the strategy families the regime router
    permits (momentum on healthy trends, mean-reversion in chop, defensive in
    downtrends). With the flag off or no regime — the default — selection is
    unchanged, so this is a safe no-op until explicitly enabled.
    """

    active = active_strategy_names(settings, requested=requested)
    specs = get_strategy_specs(timeframe=timeframe)
    if active is not None:
        specs = [spec for spec in specs if spec.name.lower() in active]
    if regime is not None and bool(getattr(settings, "regime_router_enabled", False)):
        # Imported lazily to avoid a screener<->backtesting import cycle.
        from app.screener.regime_router import route_specs

        specs = route_specs(specs, regime)
    return specs


def strategy_kwargs_for(settings: Any, spec: Any) -> dict[str, object]:
    """Return the constructor kwargs for a strategy spec, honouring settings overrides."""

    kwargs = dict(spec.default_kwargs)
    if spec.name != getattr(settings, "screener_primary_strategy_name", "rsi_vwap_ema_confluence"):
        return kwargs
    kwargs.update(
        {
            "minimum_confluence_score": float(settings.confluence_minimum_score),
            "minimum_relative_volume": max(
                float(kwargs.get("minimum_relative_volume") or 0.0),
                float(settings.confluence_minimum_relative_volume),
            ),
            "minimum_adx": float(settings.confluence_minimum_adx),
            "rsi_long_min": float(settings.confluence_rsi_long_min),
            "rsi_long_max": float(settings.confluence_rsi_long_max),
            "rsi_short_min": float(settings.confluence_rsi_short_min),
            "rsi_short_max": float(settings.confluence_rsi_short_max),
            "max_extension_atr": float(settings.confluence_max_extension_atr),
            "minimum_body_to_range": float(settings.confluence_min_body_to_range),
            "minimum_close_location": float(settings.confluence_min_close_location),
        }
    )
    return kwargs

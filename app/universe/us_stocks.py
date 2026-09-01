"""Default liquid US stock universe for screening."""

from __future__ import annotations

from app.runtime_settings import AppSettings


# Liquid large-cap default watchlist. It is configurable through settings and can be
# replaced entirely with another universe without changing screener code.
DEFAULT_TOP_100_US = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "GOOG", "META", "BRK.B", "AVGO", "TSLA",
    "LLY", "JPM", "WMT", "V", "MA", "XOM", "UNH", "ORCL", "NFLX", "COST",
    "JNJ", "PG", "HD", "BAC", "ABBV", "KO", "MRK", "CVX", "AMD", "PEP",
    "ADBE", "CRM", "TMO", "CSCO", "LIN", "MCD", "ACN", "ABT", "WFC", "IBM",
    "GE", "PM", "DIS", "QCOM", "DHR", "NOW", "INTU", "CAT", "TXN", "GS",
    "RTX", "AMGN", "UBER", "SPGI", "ISRG", "PFE", "BLK", "T", "AMAT", "BX",
    "PLTR", "INTC", "MU", "BKNG", "CMCSA", "MS", "AXP", "SCHW", "LOW", "SYK",
    "UNP", "TJX", "MDT", "GILD", "PANW", "DE", "ADI", "LRCX", "MMC", "C",
    "VRTX", "ANET", "KLAC", "COP", "CRWD", "MDLZ", "SBUX", "BA", "SHOP", "ADP",
    "SNOW", "MELI", "REGN", "PYPL", "ETN", "HON", "APH", "CB", "SO", "NKE",
]

# Second 100 liquid US large/mid-caps (not already in DEFAULT_TOP_100_US), used
# to widen the scan universe so more genuine near-miss setups surface each day.
# All are high-dollar-volume names; the IEX-calibrated liquidity floor still
# filters anything that isn't actually tradeable, so widening here adds
# opportunities without lowering quality.
ADDITIONAL_LIQUID_US_100 = [
    "ELV", "VZ", "TMUS", "CI", "CVS", "MCK", "ZTS", "BSX", "PGR", "CME",
    "ICE", "MMM", "EQIX", "AON", "ITW", "GD", "NOC", "LMT", "WM", "SHW",
    "CL", "EOG", "SLB", "APD", "FCX", "EMR", "PSX", "MPC", "VLO", "F",
    "GM", "PSA", "O", "CCI", "AMT", "SPG", "DLR", "WELL", "PLD", "NEM",
    "KMB", "GIS", "KHC", "HSY", "STZ", "MNST", "KDP", "DOW", "DD", "NUE",
    "CTAS", "PH", "ROK", "CARR", "OTIS", "JCI", "CMI", "PCAR", "FDX", "NSC",
    "CSX", "ODFL", "URI", "PWR", "FTNT", "ADSK", "WDAY", "TEAM", "DDOG", "NET",
    "ZS", "ABNB", "DASH", "RBLX", "COIN", "HOOD", "MRVL", "ON", "MCHP", "NXPI",
    "TER", "ENPH", "FSLR", "CEG", "VST", "D", "EXC", "AEP", "PEG", "XEL",
    "SRE", "PCG", "DUK", "MO", "HCA", "GEHC", "TGT", "DELL", "HPQ", "GLW",
]

# Broad ~200-name universe: the top 100 plus the next 100 liquid names.
DEFAULT_TOP_200_US = DEFAULT_TOP_100_US + ADDITIONAL_LIQUID_US_100

UNIVERSE_TIERS: dict[str, list[str]] = {
    "broad_top200": DEFAULT_TOP_200_US,
    "broad_top100": DEFAULT_TOP_100_US,
    "large_cap_leaders": [
        "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "AVGO", "TSLA", "LLY", "JPM",
        "WMT", "V", "MA", "XOM", "UNH", "ORCL", "NFLX", "COST", "AMD", "CRM",
        "CSCO", "LIN", "PM", "QCOM", "GE", "INTU", "UBER", "AMAT", "PLTR", "MU",
    ],
    "momentum_leaders": [
        "NVDA", "AVGO", "PLTR", "ANET", "CRWD", "PANW", "NFLX", "META", "UBER", "SHOP",
        "MELI", "AMD", "TSLA", "AMAT", "LRCX", "KLAC", "SMCI", "ARM", "QCOM", "ASML",
    ],
    "institutional_quality": [
        "AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META", "JPM", "V", "MA", "LLY",
        "UNH", "COST", "ORCL", "AMD", "CRM", "LIN", "ADBE", "INTU", "QCOM", "AMAT",
        "UBER", "SPGI", "ISRG", "BLK", "DE", "ANET", "LRCX", "VRTX", "MELI", "ETN",
    ],
}


def resolve_universe(settings: AppSettings, *, limit: int | None = None) -> list[str]:
    """Return the active screening universe from config or the default top-100 list."""

    symbols = settings.market_universe_symbols
    if not symbols:
        symbols = UNIVERSE_TIERS.get(settings.market_universe_tier.lower(), DEFAULT_TOP_100_US)
    normalized = []
    seen: set[str] = set()
    for symbol in symbols:
        cleaned = str(symbol).strip().upper()
        if not cleaned or cleaned in seen:
            continue
        normalized.append(cleaned)
        seen.add(cleaned)
    max_items = max(1, min(limit or settings.market_universe_limit, len(normalized)))
    return normalized[:max_items]

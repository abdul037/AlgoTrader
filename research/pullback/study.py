"""The pre-registered pullback study: 9 variants, trade-level and portfolio results.

Evaluation window: signals from 2011-01-03 (2010 is indicator and 200-day warm-up) to the last
bar with a following bar. All numbers are written to ``OUT/results.json``; ``report.py`` renders
the tables.
"""

from __future__ import annotations

import json
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from research.pullback import data as data_mod
from research.pullback import portfolio as pf
from research.pullback import signals as sig_mod
from research.pullback import stats as st
from research.pullback import trades as tr

EVAL_START = pd.Timestamp("2011-01-03")
RULES = ("bracket", "breakeven", "trend", "engine")
VARIANTS = {
    "V1": ("bracket", None, "baseline: the bot's rules"),
    "V2": ("bracket", "spy_above_200d", "SPY closed above its 200-day SMA"),
    "V3": ("bracket", "vix_below_25", "^VIX closed below 25"),
    "V4": ("breakeven", None, "stop to entry once the high reaches +1R"),
    "V5": ("trend", None, "no target; exit next open after a close below the 20 EMA"),
    "V6": ("bracket", None, "portfolio: at most 2 open per GICS sector"),
    "V7": ("bracket", "vix_below_vix3m", "^VIX closed below ^VIX3M"),
    "V8": ("bracket", "breadth_50", ">= 50% of members closed above their 50-day SMA"),
    "V9": ("bracket", "credit_risk_on", "HYG/IEF closed above its 50-day SMA"),
}
FILTER_VARIANTS = ("V2", "V3", "V7", "V8", "V9")
# The live scan universe (Railway MARKET_UNIVERSE_SYMBOLS is not in the repository; this is
# the 24-name list docs/research/intraday_edge_study.md records as live24).
LIVE24 = ["AAPL", "ADBE", "AMD", "AMZN", "AVGO", "BAC", "COST", "CRM", "CSCO", "DIS", "GOOGL",
          "INTC", "IWM", "JPM", "META", "MSFT", "NFLX", "NVDA", "PYPL", "QQQ", "SPY", "TSLA",
          "WMT", "XOM"]
LIVE_WINDOW = (pd.Timestamp("2024-11-01"), pd.Timestamp("2026-10-30"))


def _symbol_job(args: tuple[str, str, str]) -> tuple[str, dict | None]:
    data_dir, symbol, sector = args
    cleaned = data_mod.clean_symbol(Path(data_dir), symbol)
    if cleaned is None:
        return symbol, None
    frame, breaks = cleaned
    if len(frame) < sig_mod.REQUIRED_BARS + 2:
        return symbol, {"bars": None, "trades": {}, "breaks": [str(b.date()) for b in breaks], "n": len(frame)}
    signal_frame = sig_mod.compute_signals(frame)
    bars = tr.from_signal_frame(symbol, sector, signal_frame, breaks)
    trades = {rule: tr.signal_trades(bars, rule, eval_start=EVAL_START.to_datetime64()) for rule in RULES}
    above50 = pd.Series(frame["close"].to_numpy() > frame["close"].rolling(50).mean().to_numpy(),
                        index=pd.to_datetime(frame["timestamp"]))
    valid50 = pd.Series(frame["close"].rolling(50).mean().notna().to_numpy(), index=above50.index)
    closes = pd.Series(frame["close"].to_numpy(), index=pd.to_datetime(frame["timestamp"]))
    return symbol, {"bars": bars, "trades": trades, "breaks": [str(b.date()) for b in breaks],
                    "above50": above50[valid50], "closes": closes, "n": len(frame)}


def load_universe(data_dir: Path, symbols: list[tuple[str, str]]) -> dict[str, dict]:
    jobs = [(str(data_dir), symbol, sector) for symbol, sector in symbols]
    with ProcessPoolExecutor(max_workers=4) as pool:
        return {symbol: payload for symbol, payload in pool.map(_symbol_job, jobs, chunksize=8) if payload}


def market_series(data_dir: Path, members: dict[str, dict]) -> tuple[pd.DatetimeIndex, pd.DataFrame]:
    def close_of(symbol: str) -> pd.Series:
        frame, _ = data_mod.clean_symbol(data_dir, symbol)
        return pd.Series(frame["close"].to_numpy(), index=pd.to_datetime(frame["timestamp"]))

    spy = close_of("SPY")
    calendar = spy.index
    market = pd.DataFrame(index=calendar)
    market["spy"] = spy
    market["spy_sma200"] = spy.rolling(200).mean()
    rising = market["spy_sma200"] > market["spy_sma200"].shift(20)
    market["regime"] = np.where(
        market["spy_sma200"].isna(), "unknown",
        np.where(spy < market["spy_sma200"], "falling",
                 np.where((spy > market["spy_sma200"]) & rising, "rising", "sideways")),
    )
    market["vix"] = close_of("^VIX").reindex(calendar).ffill()
    market["vix3m"] = close_of("^VIX3M").reindex(calendar).ffill()
    ratio = (close_of("HYG") / close_of("IEF")).reindex(calendar).ffill()
    market["hyg_ief"] = ratio
    market["hyg_ief_sma50"] = ratio.rolling(50).mean()
    above = pd.DataFrame({s: p["above50"] for s, p in members.items() if p.get("bars") is not None})
    above = above.astype(float).reindex(calendar)
    counted = above.notna().sum(axis=1)
    market["breadth"] = above.sum(axis=1) / counted.replace(0, np.nan)
    market["breadth_count"] = counted
    market["spy_above_200d"] = spy > market["spy_sma200"]
    market["vix_below_25"] = market["vix"] < 25
    market["vix_below_vix3m"] = market["vix"] < market["vix3m"]
    market["breadth_50"] = market["breadth"] >= 0.5
    market["credit_risk_on"] = ratio > market["hyg_ief_sma50"]
    return calendar, market


def _allowed(cands: pd.DataFrame, market: pd.DataFrame, filter_name: str | None) -> pd.Series | None:
    if filter_name is None:
        return None
    flags = market[filter_name].astype(bool)
    return pd.Series(pd.to_datetime(cands["signal_date"]).map(flags).fillna(False).to_numpy(bool), index=cands.index)


def _portfolio_summary(result: pf.PortfolioResult, spy_equity: pd.Series) -> dict[str, object]:
    m = pf.metrics(result.equity, result.invested)
    spy = pf.metrics(spy_equity)
    weight, mix = pf.mix_matching_drawdown(spy_equity, m["max_drawdown"])
    half = pf.constant_mix(spy_equity, 0.5)
    m.update({
        "trades": int(len(result.trades)),
        "skipped_no_slot": int(result.skipped_no_slot),
        "skipped_no_cash": int(result.skipped_no_cash),
        "spy": spy,
        "spy_matched_dd_weight": weight,
        "spy_matched_dd": pf.metrics(mix),
        "spy_50_50": pf.metrics(half),
    })
    yearly = result.equity.resample("YE").last()
    first = result.equity.iloc[0]
    prev = yearly.shift(1).fillna(first)
    m["yearly_return"] = {int(ix.year): float(v / p - 1) for ix, v, p in zip(yearly.index, yearly, prev, strict=True)}
    spy_yearly = spy_equity.resample("YE").last()
    spy_prev = spy_yearly.shift(1).fillna(spy_equity.iloc[0])
    m["spy_yearly_return"] = {int(ix.year): float(v / p - 1) for ix, v, p in zip(spy_yearly.index, spy_yearly, spy_prev, strict=True)}
    return m


def run(data_dir: Path, out: Path | None) -> dict[str, object]:
    out = out or (data_dir / "out")
    out.mkdir(parents=True, exist_ok=True)
    members_frame = data_mod.load_members(data_dir)
    member_list = [(row.yf_symbol, row.sector) for row in members_frame.itertuples()]
    universe = load_universe(data_dir, member_list)
    calendar_full, market = market_series(data_dir, universe)
    calendar = calendar_full[calendar_full >= EVAL_START]
    usable = {s: p for s, p in universe.items() if p.get("bars") is not None}

    cands = {}
    for rule in RULES:
        frames = [p["trades"][rule] for p in usable.values() if not p["trades"][rule].empty]
        cands[rule] = pd.concat(frames, ignore_index=True)
    closes = {s: p["closes"].reindex(calendar).ffill().to_numpy() for s, p in usable.items()}
    spy_equity = market["spy"].reindex(calendar) / market["spy"].reindex(calendar).iloc[0] * 100_000.0
    regime = market["regime"]

    results: dict[str, object] = {"variants": {}, "filters": {}, "data": {}, "context": {}}
    trade_sets: dict[str, pd.DataFrame] = {}
    config = pf.PortfolioConfig()
    for name, (rule, filter_name, label) in VARIANTS.items():
        pool = cands[rule]
        allowed = _allowed(pool, market, filter_name)
        if name == "V6":
            sim = pf.simulate(pool, calendar, closes, pf.PortfolioConfig(sector_cap=2))
            judged = sim.trades.merge(pool[["symbol", "signal_date", "risk_pct", "cost_r", "bars_held"]],
                                      on=["symbol", "signal_date"], how="left")
        else:
            judged = tr.chain(pool, allowed)
            portfolio_pool = pool if allowed is None else pool[allowed.to_numpy(bool)]
            sim = pf.simulate(portfolio_pool, calendar, closes, config)
        trade_sets[name] = judged
        summary = st.summarize(judged, regime)
        port_trades = sim.trades.merge(pool[["symbol", "signal_date", "risk_pct", "cost_r", "bars_held"]],
                                       on=["symbol", "signal_date"], how="left")
        results["variants"][name] = {
            "rule": rule, "filter": filter_name, "label": label,
            "trade_level": summary,
            "portfolio_trades": st.summarize(port_trades, regime) if not port_trades.empty else {"trades": 0},
            "portfolio": _portfolio_summary(sim, spy_equity),
            "invalid_bracket_signals": int((~pool["valid"]).sum()) if allowed is None
            else int((~pool.loc[allowed.to_numpy(bool), "valid"]).sum()),
        }
        judged.to_parquet(out / f"trades_{name}.parquet", index=False)
        sim.equity.to_frame("equity").assign(invested=sim.invested).to_parquet(out / f"equity_{name}.parquet")

    # Sensitivity (not in the rule): remove the rough survivorship drift from every held position.
    surv = _survivorship(data_dir, usable, calendar, trade_sets["V1"])
    haircut = float(np.log1p(surv["members_ew_cagr"]) - np.log1p(surv["rsp_cagr"])) / 252.0
    for name, (rule, filter_name, _label) in VARIANTS.items():
        pool = cands[rule]
        allowed = _allowed(pool, market, filter_name)
        portfolio_pool = pool if allowed is None else pool[allowed.to_numpy(bool)]
        adj_config = pf.PortfolioConfig(sector_cap=2 if name == "V6" else None, haircut_per_day=haircut)
        adj = pf.simulate(portfolio_pool, calendar, closes, adj_config)
        judged = trade_sets[name]
        held = judged["bars_held"].astype(float)
        r_adj = judged["r_net"] - judged["entry"] * (1 - np.exp(-haircut * held)) / (judged["entry"] - judged["stop"])
        results["variants"][name]["survivorship_adjusted"] = {
            "haircut_per_year": haircut * 252, "r_net": float(r_adj.mean()),
            "t_day": st.clustered_t(r_adj, pd.to_datetime(judged["entry_date"]))[0],
            "portfolio": pf.metrics(adj.equity, adj.invested),
        }

    # Holm across all nine variants, on the day-clustered one-sided p of after-cost R.
    pvalues = {n: float(v["trade_level"].get("p_day", float("nan"))) for n, v in results["variants"].items()}
    adjusted = st.holm(pvalues)
    spy_metrics = pf.metrics(spy_equity)
    for name, payload in results["variants"].items():
        payload["holm_p"] = adjusted[name]
        payload["pass"] = _pass_rule(payload, spy_metrics)

    # Removed-vs-kept: V1's own trades split by each market filter on the signal day.
    v1 = trade_sets["V1"]
    for name in FILTER_VARIANTS:
        flag = _allowed(v1, market, VARIANTS[name][1]).to_numpy(bool)
        kept, removed = v1[flag], v1[~flag]
        results["filters"][name] = {
            "filter": VARIANTS[name][1],
            "v1_trades": int(len(v1)),
            "kept": _brief(kept), "removed": _brief(removed),
            "variant_trades": int(results["variants"][name]["trade_level"]["trades"]),
            "days_off_share": float((~market.loc[calendar, VARIANTS[name][1]].astype(bool)).mean()),
        }

    results["data"] = _data_summary(universe, members_frame, market, calendar, cands)
    results["context"]["survivorship"] = surv
    results["context"]["quote_rr_check"] = _quote_rr(v1)
    results["context"]["regime_days"] = {str(k): int(v) for k, v in market.loc[calendar, "regime"].value_counts().items()}
    results["spy"] = spy_metrics
    (out / "results.json").write_text(json.dumps(results, indent=2, default=_json_default))
    market.to_parquet(out / "market.parquet")
    print(json.dumps({n: {"r_net": v["trade_level"]["r_net"], "t": v["trade_level"]["t_day"],
                          "holm": v["holm_p"], "pass": v["pass"]} for n, v in results["variants"].items()},
                     indent=1, default=_json_default))
    return results


def _brief(frame: pd.DataFrame) -> dict[str, float]:
    if frame.empty:
        return {"n": 0}
    entry = pd.to_datetime(frame["entry_date"])
    t, p, _ = st.clustered_t(frame["r_net"], entry)
    t_m, p_m, months = st.clustered_t(frame["r_net"], entry.dt.to_period("M").astype(str))
    return {"n": int(len(frame)), "r_net": float(frame["r_net"].mean()), "r_gross": float(frame["r_gross"].mean()),
            "win_rate": float((frame["r_net"] > 0).mean()), "sum_r_net": float(frame["r_net"].sum()),
            "t_day": t, "p_day": p, "t_month": t_m, "p_month": p_m, "months": int(months)}


def _pass_rule(payload: dict, spy: dict[str, float]) -> dict[str, object]:
    tl = payload["trade_level"]
    port = payload["portfolio"]
    a = bool(tl["r_net"] > 0 and payload["holm_p"] < 0.10)
    years_share = tl["years_positive"] / tl["years_total"] if tl["years_total"] else 0.0
    falling = tl["conditions"].get("falling")
    falling_ok = falling is None or falling["r"] >= -0.10
    b = bool(years_share >= 0.60 and falling_ok)
    sharpe_ok = port["sharpe"] > spy["sharpe"]
    dd_ok = abs(port["max_drawdown"]) <= 0.5 * abs(spy["max_drawdown"]) and port["cagr"] >= 0.7 * spy["cagr"]
    c = bool(sharpe_ok or dd_ok)
    return {"a": a, "b": b, "c": c, "all": a and b and c, "years_share": years_share,
            "falling_r": None if falling is None else falling["r"], "falling_empty": falling is None,
            "c_sharpe": bool(sharpe_ok), "c_drawdown": bool(dd_ok)}


def _data_summary(universe: dict, members: pd.DataFrame, market: pd.DataFrame, calendar: pd.DatetimeIndex,
                  cands: dict[str, pd.DataFrame]) -> dict[str, object]:
    usable = [s for s, p in universe.items() if p.get("bars") is not None]
    breaks = {s: p["breaks"] for s, p in universe.items() if p.get("breaks")}
    starts = {s: str(pd.Timestamp(p["bars"].dates[0]).date()) for s, p in universe.items() if p.get("bars") is not None}
    late = sum(1 for d in starts.values() if pd.Timestamp(d) > EVAL_START)
    date_added = pd.to_datetime(members["date_added"], errors="coerce")
    bracket = cands["bracket"]
    return {
        "members": int(len(members)),
        "usable_symbols": len(usable),
        "unusable": sorted(set(members["yf_symbol"]) - set(usable)),
        "history_starts_after_eval_start": late,
        "added_to_index_after_eval_start": int((date_added > EVAL_START).sum()),
        "breaks": breaks,
        "calendar_first": str(calendar[0].date()), "calendar_last": str(calendar[-1].date()),
        "trading_days": int(len(calendar)),
        "buy_signals_in_window": int(len(bracket)),
        "invalid_bracket_signals": int((~bracket["valid"]).sum()),
        "data_break_exits": int((bracket["reason"] == "data_break").sum()),
        "breadth_min_count": int(market.loc[calendar, "breadth_count"].min()),
        "sectors": members["sector"].value_counts().to_dict(),
    }


def _survivorship(data_dir: Path, usable: dict, calendar: pd.DatetimeIndex, v1: pd.DataFrame) -> dict[str, float]:
    """Equal-weight buy-and-hold of today's members vs RSP (the equal-weight S&P 500 as it was)."""

    closes = pd.DataFrame({s: p["closes"] for s, p in usable.items()}).reindex(calendar)
    daily = closes.pct_change(fill_method=None)
    ew = (1.0 + daily.mean(axis=1, skipna=True).fillna(0.0)).cumprod()
    frame, _ = data_mod.clean_symbol(data_dir, "RSP")
    rsp = pd.Series(frame["close"].to_numpy(), index=pd.to_datetime(frame["timestamp"])).reindex(calendar).ffill()
    years = (calendar[-1] - calendar[0]).days / 365.25
    ew_cagr = float(ew.iloc[-1] ** (1 / years) - 1)
    rsp_cagr = float((rsp.iloc[-1] / rsp.iloc[0]) ** (1 / years) - 1)
    gap_per_day = (np.log1p(ew_cagr) - np.log1p(rsp_cagr)) / 252
    hold = float(v1["bars_held"].mean())
    risk = float(v1["risk_pct"].mean())
    return {"members_ew_cagr": ew_cagr, "rsp_cagr": rsp_cagr, "gap_per_year": ew_cagr - rsp_cagr,
            "v1_mean_bars_held": hold, "v1_mean_risk_pct": risk,
            "implied_r_per_trade": float(gap_per_day * hold / risk)}


def _quote_rr(v1: pd.DataFrame) -> dict[str, float]:
    """Live re-checks reward:risk at the quote (>= 1.0) before submitting; the study enters at the
    open regardless (as the method specifies). How many V1 trades that check would have refused."""

    rr = (v1["target"] - v1["entry"]) / (v1["entry"] - v1["stop"])
    low = v1[rr < 1.0]
    return {"trades_below_1": int(len(low)), "share": float(len(low) / len(v1)),
            "their_r_net": float(low["r_net"].mean()) if len(low) else float("nan"),
            "rest_r_net": float(v1.loc[rr >= 1.0, "r_net"].mean())}


def _json_default(value: object) -> object:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return float(value)
    if isinstance(value, (np.bool_,)):
        return bool(value)
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return str(pd.Timestamp(value).date())
    return str(value)

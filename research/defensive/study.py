"""Run the pre-registered defensive trend study and write ``results.json`` and ``tables.md``."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.defensive import backtest, data, metrics, report
from research.defensive import strategies as st

RISK_FUNDS = st.FIVE
ALT_MONTHS = [8, 9, 11, 12]
MAIN = "$10,000"
SCENARIOS = {
    "$10,000": backtest.Costs(capital=10_000.0),
    "$100,000": backtest.Costs(capital=100_000.0),
    "$500": backtest.Costs(capital=500.0),
    "zero cost": backtest.Costs(fixed_fee=0.0, bps_fee=0.0, capital=10_000.0),
    "$10,000, cash 0%": backtest.Costs(capital=10_000.0, cash_earns=False),
}
VARIANTS = ["D1", "D2", "D3", "D4"]
BENCHMARKS = ["SPY buy-and-hold", "60/40 SPY/IEF", "EW5 rebalanced", "EW5 never rebalanced"]
EPISODES = {
    "2008": ("2007-01-01", "2009-12-31"),
    "2020 crash": ("2020-01-01", "2020-12-31"),
    "2022": ("2022-01-01", "2022-12-31"),
    "2025 drop": ("2025-01-01", "2025-12-31"),
}
CURRENT_FROM = "2024-12-31"


def load_market(data_dir: Path) -> tuple[backtest.Market, pd.Series, str]:
    spy = data.load_adjusted(data_dir, "SPY")
    cal = spy.index
    opens = pd.DataFrame(
        {f: data.load_adjusted(data_dir, f)["Open"].reindex(cal) for f in RISK_FUNDS}
    )
    closes = pd.DataFrame(
        {f: data.load_adjusted(data_dir, f)["Close"].reindex(cal) for f in RISK_FUNDS}
    )
    cash, note = data.cash_daily_returns(data_dir, cal)
    cash_index = (1.0 + cash).cumprod()
    return backtest.Market(opens=opens, closes=closes, cash=cash), cash_index, note


def schedules(
    market: backtest.Market, cash_index: pd.Series, first_signal: pd.Timestamp
) -> dict[str, dict]:
    c = market.closes
    out = {
        "D1": st.d1(c, first_signal, 10),
        "D2": st.d2(c, first_signal),
        "D3": st.d3(c, first_signal, 10),
        "D4": st.d4(c, cash_index, first_signal),
        "SPY buy-and-hold": st.fixed_mix(c, {"SPY": 1.0}, first_signal, rebalance=False),
        "60/40 SPY/IEF": st.fixed_mix(c, {"SPY": 0.6, "IEF": 0.4}, first_signal, rebalance=True),
        "EW5 rebalanced": st.fixed_mix(c, {f: 0.2 for f in RISK_FUNDS}, first_signal, True),
        "EW5 never rebalanced": st.fixed_mix(c, {f: 0.2 for f in RISK_FUNDS}, first_signal, False),
    }
    for m in ALT_MONTHS:
        out[f"D1 ({m}-month)"] = st.d1(c, first_signal, m)
        out[f"D3 ({m}-month)"] = st.d3(c, first_signal, m)
    return out


def summarise(res: backtest.Result, cash_daily: pd.Series, spy_close: pd.Series) -> dict:
    eq = res.equity
    if res.meta["ruined"]:  # fees used up the account: no ratios
        return {
            "ruined": res.meta["ruined"],
            "cagr": -1.0,
            "max_dd": -1.0,
            "fees_paid": res.fees_paid,
            "orders": int(len(res.orders)),
            "monthly_excess": [],
        }
    base = eq.index[0]
    monthly = metrics.monthly_returns(eq)
    cash_m = metrics.monthly_cash(cash_daily, monthly.index, base)
    excess = (monthly - cash_m).to_numpy()
    dd, dd_peak, dd_trough = metrics.max_drawdown(eq)
    years = metrics.calendar_years(eq)
    full = years.loc[:2025]
    yrs = metrics.years_between(base, eq.index[-1])
    orders = res.orders
    out = {
        "cagr": metrics.cagr(eq),
        "max_dd": dd,
        "max_dd_peak": str(dd_peak.date()),
        "max_dd_trough": str(dd_trough.date()),
        "sharpe": metrics.sharpe(excess),
        "sortino": metrics.sortino(excess),
        "ann_vol": float(monthly.std(ddof=1) * np.sqrt(12)),
        "worst_year": float(full.min()),
        "worst_year_label": int(full.idxmin()),
        "years": {int(k): float(v) for k, v in years.items()},
        "orders": int(len(orders)),
        "trades_per_year": len(orders) / yrs,
        "signal_trades_per_year": float((orders["kind"] == "signal").sum() / yrs),
        "rebalance_trades_per_year": float((orders["kind"] == "rebalance").sum() / yrs),
        "time_in_market": float(res.exposure.mean()),
        "fees_paid": res.fees_paid,
        "final_equity": float(eq.iloc[-1]),
        "episodes": {},
        "current": metrics.period(eq, pd.Timestamp(CURRENT_FROM)),
        "monthly_excess": excess.tolist(),
        "ruined": None,
    }
    for label, (lo, hi) in EPISODES.items():
        peak, trough = metrics.find_episode(spy_close, lo, hi)
        out["episodes"][label] = metrics.episode(eq, peak, trough)
    return out


def engine_checks(
    market: backtest.Market,
    scheds: dict,
    base: pd.Timestamp,
    start: pd.Timestamp,
    end: pd.Timestamp,
) -> dict:
    """Independent checks of the engine on zero cost, and of the fee arithmetic."""
    zero = backtest.Costs(fixed_fee=0.0, bps_fee=0.0, capital=1.0)
    cal = market.calendar[(market.calendar >= start) & (market.calendar <= end)]
    o, c = market.opens["SPY"], market.closes["SPY"]
    checks = {}
    bh = backtest.run(market, scheds["SPY buy-and-hold"], start, end, zero, "bh", base)
    checks["spy_bh_vs_close_over_open"] = float(bh.equity.iloc[-1] - c.loc[end] / o.loc[start])
    for name in ("D1", "D2"):
        res = backtest.run(market, scheds[name], start, end, zero, name, base)
        state, level, worst = False, 1.0, 0.0
        prev_c = c.shift(1)
        for day in cal:
            new = scheds[name].get(day)
            if new is None:
                r = c[day] / prev_c[day] if state else 1.0 + market.cash[day]
            else:
                overnight = o[day] / prev_c[day] if state else 1.0
                state = "SPY" in new
                r = overnight * (c[day] / o[day] if state else 1.0 + market.cash[day])
            level *= r
            worst = max(worst, abs(level / res.equity.loc[day] - 1.0))
        checks[f"{name}_vectorised_max_rel_diff"] = worst
    costs = backtest.Costs(capital=10_000.0)
    res = backtest.run(market, scheds["D3"], start, end, costs, "D3", base)
    implied = len(res.orders) * costs.fixed_fee + costs.bps_fee * res.orders["notional"].sum()
    checks["d3_fee_identity_abs_diff"] = float(abs(implied - res.fees_paid))
    return checks


def fund_table(data_dir: Path, base: pd.Timestamp, end: pd.Timestamp) -> list[dict]:
    rows = []
    for fund in data.FUNDS:
        close = data.load_adjusted(data_dir, fund)["Close"]
        first = close.index[0]
        start = max(base, first)
        eq = close.loc[start:end]
        dd, peak, trough = metrics.max_drawdown(eq)
        years = metrics.calendar_years(eq).loc[:2025]
        rows.append(
            {
                "fund": fund,
                "from": str(start.date()),
                "cagr": metrics.cagr(eq),
                "max_dd": dd,
                "dd_peak": str(peak.date()),
                "dd_trough": str(trough.date()),
                "worst_year": float(years.min()),
                "worst_year_label": int(years.idxmin()),
            }
        )
    return rows


def unadjusted_diagnostic(
    data_dir: Path, market: backtest.Market, first_signal: pd.Timestamp
) -> dict:
    cal = market.calendar
    raw = pd.DataFrame({f: data.load_raw(data_dir, f)["Close"].reindex(cal) for f in RISK_FUNDS})
    _, on_adj = st.sma_trend(market.closes, RISK_FUNDS, 10, first_signal, 0.2)
    _, on_raw = st.sma_trend(raw, RISK_FUNDS, 10, first_signal, 0.2)
    differ = on_adj != on_raw
    s_adj = st.d2_states(market.closes["SPY"])
    s_raw = st.d2_states(raw["SPY"])
    test = s_adj.index[(s_adj.index >= first_signal) & (s_adj.index < cal[-1])]
    return {
        "d1_months_differ": int(differ["SPY"].sum()),
        "d1_months": int(len(differ)),
        "d3_sleeve_months_differ": int(differ.to_numpy().sum()),
        "d3_sleeve_months": int(differ.size),
        "d3_by_fund": {f: int(differ[f].sum()) for f in RISK_FUNDS},
        "d2_days_differ": int((s_adj.loc[test] != s_raw.loc[test]).sum()),
        "d2_days": int(len(test)),
    }


def same_dd_mix(
    market: backtest.Market, base: pd.Timestamp, end: pd.Timestamp, target_dd: float
) -> dict:
    """Diagnostic added after the results: a constant SPY/cash mix, rebalanced daily at zero
    cost (close to close), whose max drawdown equals ``target_dd``."""
    spy = market.closes["SPY"].loc[base:end].pct_change().dropna()
    cash = market.cash.loc[spy.index]

    def curve(w: float) -> pd.Series:
        level = (1.0 + w * spy + (1.0 - w) * cash).cumprod()
        return pd.concat([pd.Series([1.0], index=[base]), level])

    lo, hi = 0.0, 1.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if metrics.max_drawdown(curve(mid))[0] > target_dd:
            lo = mid
        else:
            hi = mid
    eq = curve(lo)
    monthly = metrics.monthly_returns(eq)
    excess = (monthly - metrics.monthly_cash(market.cash, monthly.index, base)).to_numpy()
    return {
        "weight": lo,
        "cagr": metrics.cagr(eq),
        "max_dd": metrics.max_drawdown(eq)[0],
        "sharpe": metrics.sharpe(excess),
    }


def verdicts(summary: dict[str, dict]) -> dict:
    spy = summary["SPY buy-and-hold"]
    dd_limit = abs(spy["max_dd"]) / 2
    cagr_floor = 0.75 * spy["cagr"]

    def basic(s: dict) -> dict:
        return {"i": abs(s["max_dd"]) <= dd_limit, "ii": s["cagr"] >= cagr_floor}

    out = {"dd_limit": dd_limit, "cagr_floor": cagr_floor, "variants": {}}
    for name in VARIANTS:
        b = basic(summary[name])
        row = {"i": b["i"], "ii": b["ii"]}
        if name in ("D1", "D3"):
            alts = {m: basic(summary[f"{name} ({m}-month)"]) for m in ALT_MONTHS}
            n_ok = sum(v["i"] and v["ii"] for v in alts.values())
            row["alts"] = {m: v for m, v in alts.items()}
            row["alts_passing"] = n_ok
            row["iii"] = n_ok >= 3
        row["pass"] = all(v for k, v in row.items() if k in ("i", "ii", "iii"))
        out["variants"][name] = row
    for m in ALT_MONTHS:
        for name in ("D1", "D3"):
            out.setdefault("alt_rows", {})[f"{name} ({m}-month)"] = basic(
                summary[f"{name} ({m}-month)"]
            )
    return out


def run(data_dir: Path, out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    market, cash_index, cash_note = load_market(data_dir)
    cal = market.calendar
    base = cal[cal < pd.Timestamp("2007-01-01")][-1]
    start = cal[cal >= pd.Timestamp("2007-01-01")][0]
    end = cal[-1]
    scheds = schedules(market, cash_index, base)
    checks = engine_checks(market, scheds, base, start, end)
    print("engine checks:", checks)
    spy_close = market.closes["SPY"]

    results: dict = {
        "base": str(base.date()),
        "start": str(start.date()),
        "end": str(end.date()),
        "cash_note": cash_note,
        "engine_checks": checks,
        "scenarios": {},
    }
    for scen, costs in SCENARIOS.items():
        summary = {}
        for name, sched in scheds.items():
            res = backtest.run(market, sched, start, end, costs, name, base)
            summary[name] = summarise(res, market.cash, spy_close)
        results["scenarios"][scen] = {"summary": summary, "verdicts": verdicts(summary)}
        print(f"done {scen}")

    main = results["scenarios"][MAIN]["summary"]
    spy_ex = np.asarray(main["SPY buy-and-hold"]["monthly_excess"])
    results["bootstrap"] = {
        name: metrics.block_bootstrap_sharpe_diff(np.asarray(s["monthly_excess"]), spy_ex)
        for name, s in main.items()
        if name != "SPY buy-and-hold"
    }
    results["episode_dates"] = {
        label: [str(d.date()) for d in metrics.find_episode(spy_close, lo, hi)]
        for label, (lo, hi) in EPISODES.items()
    }
    results["same_dd_mix"] = {
        name: same_dd_mix(market, base, end, main[name]["max_dd"])
        for name in VARIANTS + ["60/40 SPY/IEF", "EW5 rebalanced"]
    }
    picks = st.d4_choices(market.closes, cash_index, base)
    results["d4_months"] = {k: int(v) for k, v in picks.value_counts().items()}
    results["d4_switches"] = int((picks != picks.shift(1)).sum() - 1)
    results["funds"] = fund_table(data_dir, base, end)
    results["unadjusted"] = unadjusted_diagnostic(data_dir, market, base)
    cash_level = (1 + market.cash.loc[start:end]).prod()
    results["cash_cagr"] = float(cash_level ** (1 / metrics.years_between(base, end)) - 1)

    for scen in results["scenarios"].values():
        for s in scen["summary"].values():
            s.pop("monthly_excess")
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, default=str))
    (out_dir / "tables.md").write_text(report.render(results))
    print(f"wrote {out_dir / 'results.json'} and {out_dir / 'tables.md'}")

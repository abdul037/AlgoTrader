"""The pre-registered momentum study: 4 variants, controls, statistics, pass rule.

Writes ``results.json`` (every number the report quotes) and ``monthly.csv`` (monthly series)
into the output directory.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from research.momentum import stats
from research.momentum.data import Panel
from research.momentum.engine import (
    TOP_N,
    ZERO,
    Costs,
    Result,
    Universe,
    account,
    build_universe,
    buy_and_hold,
    simulate,
    top_n,
)

SEED = 20261101
REPS = 500
VARIANTS = {
    "M1": "top 20 by 12-1 month return",
    "M2": "M1, cash when SPY < its 10-month SMA",
    "M3": "top 20 by 6-1 month return",
    "M4": "top 20 by 12-1 return / 12-month daily vol",
}
ACCOUNTS = {
    "zero": ZERO,
    "100k": account(100_000),
    "10k": account(10_000),
    "10k_resize_fee": account(10_000, resize_fee=True),
    "500": account(500),
}


def _scores(universe: Universe, eligible: np.ndarray) -> dict[str, np.ndarray]:
    mask = lambda values: np.where(eligible, values, np.nan)  # noqa: E731
    with np.errstate(invalid="ignore", divide="ignore"):
        return {"M1": mask(universe.mom12), "M2": mask(universe.mom12), "M3": mask(universe.mom6),
                "M4": mask(universe.mom12 / universe.vol12)}


def _random_targets(eligible: np.ndarray, reps: int, seed: int) -> list[np.ndarray]:
    rng = np.random.default_rng(seed)
    targets = []
    for row in eligible:
        pool = np.flatnonzero(row)
        keys = rng.random((reps, len(pool)))
        targets.append(pool[np.argsort(keys, axis=1)[:, :TOP_N]])
    return targets


def metrics(universe: Universe, result: Result, years: float) -> dict:
    """Metrics per repetition (arrays) for one simulated result."""

    monthly = result.monthly
    final = result.equity_daily[:, -1]
    return {
        "cagr": stats.cagr(final, years),
        "max_dd": stats.max_drawdown(result.equity_daily),
        "sharpe": stats.sharpe(monthly, universe.cash_ret[None, :]),
        "vol": monthly.std(axis=1, ddof=1) * np.sqrt(12.0),
        "worst_month": monthly.min(axis=1),
        "turnover_month": result.turnover[:, 1:].mean(axis=1),
        "entries_month": result.entries[:, 1:].mean(axis=1),
        "fees_month": result.fees[:, 1:].mean(axis=1),
        "cost_year": result.cost[:, 1:].mean(axis=1) * 12.0,
    }


def _first(values: dict) -> dict:
    return {key: float(np.asarray(value).ravel()[0]) for key, value in values.items()}


def summarise(universe: Universe, result: Result, years: float) -> dict:
    out = _first(metrics(universe, result, years))
    by_year = stats.calendar_years(universe.months.labels, result.monthly[0])
    out["years"] = by_year
    worst = min(by_year, key=by_year.get)
    out["worst_year"] = {"year": worst, "return": by_year[worst]}
    out["worst_month_label"] = universe.months.labels[int(np.argmin(result.monthly[0]))]
    equity = result.equity_daily[0]
    peak_curve = np.maximum.accumulate(equity)
    trough = int(np.argmin(equity / peak_curve))
    peak = int(np.argmax(equity[: trough + 1]))
    labels = universe.daily_labels()
    out["max_dd_peak"], out["max_dd_trough"] = labels[peak], labels[trough]
    return out


def distribution(values: np.ndarray) -> dict:
    return {f"p{q}": float(np.percentile(values, q)) for q in (5, 25, 50, 75, 95)} | {
        "mean": float(np.mean(values))}


def percentile_of(value: float, values: np.ndarray) -> float:
    return float(np.mean(values < value) * 100.0)


def holdings_diagnostics(universe: Universe, picks: list[np.ndarray], cash: np.ndarray | None,
                         sectors: list[str]) -> dict:
    not_in, not_in_ret, in_ret, max_sector, kept = 0, [], [], [], []
    previous: set[int] = set()
    held_months = 0
    for j, idx in enumerate(picks):
        if cash is not None and cash[j]:
            previous = set()
            continue
        held_months += 1
        flags = universe.in_index[j, idx]
        rets = universe.ret[j, idx]
        not_in += int((~flags).sum())
        not_in_ret.extend(rets[~flags].tolist())
        in_ret.extend(rets[flags].tolist())
        counts = pd.Series([sectors[i] for i in idx]).value_counts()
        max_sector.append(int(counts.iloc[0]))
        if j > 0:
            kept.append(len(previous & set(idx.tolist())))
        previous = set(idx.tolist())
    total = held_months * TOP_N
    return {
        "position_months": total,
        "not_yet_in_index_share": not_in / total if total else float("nan"),
        "not_yet_in_index_mean_month_return": float(np.mean(not_in_ret)) if not_in_ret else float("nan"),
        "in_index_mean_month_return": float(np.mean(in_ret)) if in_ret else float("nan"),
        "mean_largest_sector_count": float(np.mean(max_sector)),
        "mean_names_kept": float(np.mean(kept)) if kept else float("nan"),
    }


def run_universe(universe: Universe, eligible: np.ndarray, years: float, *, full: bool) -> dict:
    """Variants and the random control on one eligibility mask."""

    scores = _scores(universe, eligible)
    picks = {name: top_n(score, universe.symbols) for name, score in scores.items()}
    cash = {"M2": universe.spy_below_sma10}
    accounts = ACCOUNTS if full else {"zero": ZERO, "100k": ACCOUNTS["100k"], "10k": ACCOUNTS["10k"]}
    variants: dict = {}
    sims: dict = {}
    for name in VARIANTS:
        variants[name] = {}
        for key, costs in accounts.items():
            sim = simulate(universe, picks[name], costs, cash.get(name))
            sims[(name, key)] = sim
            variants[name][key] = summarise(universe, sim, years)
    rand_targets = _random_targets(eligible, REPS, SEED)
    random_sims = {key: simulate(universe, rand_targets, costs)
                   for key, costs in accounts.items() if key in ("zero", "100k", "10k")}
    random_metrics = {key: metrics(universe, sim, years) for key, sim in random_sims.items()}
    return {"picks": picks, "variants": variants, "sims": sims, "random_sims": random_sims,
            "random_metrics": random_metrics, "rand_targets": rand_targets}


def tests(universe: Universe, sims: dict, random_zero: Result, rsp: dict[str, np.ndarray]) -> dict:
    out: dict = {}
    random_mean = random_zero.monthly.mean(axis=0)
    families = {
        "vs_random_mean_zero": lambda name: sims[(name, "zero")].monthly[0] - random_mean,
        "vs_rsp_100k": lambda name: sims[(name, "100k")].monthly[0] - rsp["100k"],
        "vs_rsp_zero": lambda name: sims[(name, "zero")].monthly[0] - rsp["zero"],
        "vs_rsp_10k": lambda name: sims[(name, "10k")].monthly[0] - rsp["10k"],
    }
    for family, series_of in families.items():
        rows = {name: stats.t_test(series_of(name)) for name in VARIANTS}
        adjusted = stats.holm([rows[name]["p_one_sided"] for name in VARIANTS])
        for name, p_adj in zip(VARIANTS, adjusted, strict=True):
            rows[name]["holm_p"] = p_adj
            rows[name]["mean_annual"] = rows[name]["mean"] * 12.0
        out[family] = rows
    return out


def crashes(universe: Universe, sims: dict, random_zero: Result) -> dict:
    labels = universe.months.labels
    random_mean = random_zero.monthly.mean(axis=0)
    out = {}
    for name in VARIANTS:
        monthly = sims[(name, "zero")].monthly[0]
        excess = monthly - random_mean
        worst = np.argsort(excess)[:5]
        out[name] = {
            "worst_relative_months": [
                {"month": labels[i], "variant": float(monthly[i]), "random_mean": float(random_mean[i]),
                 "excess": float(excess[i])} for i in worst],
            "relative_drawdown": stats.relative_drawdown(labels, monthly, random_mean),
        }
    return out


def run(price_dir: Path, extra_dir: Path, out_dir: Path) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    panel = Panel(price_dir, extra_dir)
    universe = build_universe(panel)
    labels = universe.months.labels
    first = pd.Timestamp(universe.meta["first_trade_day"])
    last = pd.Timestamp(universe.meta["last_day"])
    years = (last - first).days / 365.25
    sectors = [panel.sector.get(symbol, "?") for symbol in universe.symbols]

    main = run_universe(universe, universe.eligible, years, full=True)
    sims = main["sims"]
    random_zero = main["random_sims"]["zero"]

    benchmarks: dict = {}
    bench_monthly: dict = {}
    for symbol in ("SPY", "RSP"):
        benchmarks[symbol] = {}
        bench_monthly[symbol] = {}
        for key, costs in ACCOUNTS.items():
            sim = buy_and_hold(universe, symbol, costs)
            benchmarks[symbol][key] = summarise(universe, sim, years)
            bench_monthly[symbol][key] = sim.monthly[0]
    all_members = simulate(universe, [np.flatnonzero(row) for row in universe.eligible], ZERO)
    controls = {"all_members_zero": summarise(universe, all_members, years)}
    controls["all_members_zero"]["mean_names"] = float(universe.eligible.sum(axis=1).mean())
    random_mean_series = random_zero.monthly.mean(axis=0)
    controls["random_mean_years"] = stats.calendar_years(labels, random_mean_series)

    random_dist = {key: {metric: distribution(values) for metric, values in m.items()}
                   for key, m in main["random_metrics"].items()}
    rz = main["random_metrics"]["zero"]
    percentiles = {name: {
        "sharpe_pct": percentile_of(main["variants"][name]["zero"]["sharpe"], rz["sharpe"]),
        "cagr_pct": percentile_of(main["variants"][name]["zero"]["cagr"], rz["cagr"]),
        "sharpe_pct_100k": percentile_of(main["variants"][name]["100k"]["sharpe"], main["random_metrics"]["100k"]["sharpe"]),
        "sharpe_pct_10k": percentile_of(main["variants"][name]["10k"]["sharpe"], main["random_metrics"]["10k"]["sharpe"]),
    } for name in VARIANTS}

    # Informational: the random control with M2's cash months.
    random_m2 = simulate(universe, main["rand_targets"], ZERO, universe.spy_below_sma10)
    random_m2_metrics = metrics(universe, random_m2, years)
    random_m2_out = {metric: distribution(values) for metric, values in random_m2_metrics.items()}
    random_m2_out["m2_sharpe_pct"] = percentile_of(main["variants"]["M2"]["zero"]["sharpe"], random_m2_metrics["sharpe"])
    random_m2_out["m2_cagr_pct"] = percentile_of(main["variants"]["M2"]["zero"]["cagr"], random_m2_metrics["cagr"])

    rsp_series = {key: bench_monthly["RSP"][key] for key in ("zero", "100k", "10k")}
    test_rows = tests(universe, sims, random_zero, rsp_series)
    spy_vs = {name: stats.t_test(sims[(name, "100k")].monthly[0] - bench_monthly["SPY"]["100k"]) for name in VARIANTS}
    random_vs_rsp = stats.t_test(random_mean_series - rsp_series["zero"])
    all_vs_rsp = stats.t_test(all_members.monthly[0] - rsp_series["zero"])

    # Informational: the already-in-index universe.
    in_index = universe.eligible & universe.in_index
    alt = run_universe(universe, in_index, years, full=False)
    alt_rz = alt["random_metrics"]["zero"]
    alt_tests = tests(universe, alt["sims"], alt["random_sims"]["zero"], rsp_series)
    alt_out = {name: {
        "zero": {k: alt["variants"][name]["zero"][k] for k in ("cagr", "max_dd", "sharpe")},
        "100k": {k: alt["variants"][name]["100k"][k] for k in ("cagr", "max_dd", "sharpe")},
        "10k": {k: alt["variants"][name]["10k"][k] for k in ("cagr", "max_dd", "sharpe")},
        "sharpe_pct": percentile_of(alt["variants"][name]["zero"]["sharpe"], alt_rz["sharpe"]),
        "vs_rsp_100k": alt_tests["vs_rsp_100k"][name],
        "vs_random_mean_zero": alt_tests["vs_random_mean_zero"][name],
    } for name in VARIANTS}
    alt_all = simulate(universe, [np.flatnonzero(row) for row in in_index], ZERO)
    alt_controls = {
        "random_zero": {metric: distribution(alt_rz[metric]) for metric in ("cagr", "sharpe", "max_dd")},
        "all_members_zero": {k: summarise(universe, alt_all, years)[k] for k in ("cagr", "max_dd", "sharpe")},
        "eligible_first": int(in_index[0].sum()), "eligible_last": int(in_index[-1].sum()),
    }

    diagnostics = {name: holdings_diagnostics(universe, main["picks"][name],
                                              universe.spy_below_sma10 if name == "M2" else None, sectors)
                   for name in VARIANTS}
    random_not_in = float(np.mean(~universe.in_index[np.arange(len(labels))[:, None, None],
                                                      np.stack(main["rand_targets"])]))
    diagnostics["random_not_yet_in_index_share"] = random_not_in
    diagnostics["eligible_not_yet_in_index_share"] = float(
        (universe.eligible & ~universe.in_index).sum() / universe.eligible.sum())

    spy_sharpe_10k = benchmarks["SPY"]["10k"]["sharpe"]
    verdicts = {}
    for name in VARIANTS:
        c1 = main["variants"][name]["zero"]["sharpe"] > random_dist["zero"]["sharpe"]["p95"]
        c2 = test_rows["vs_rsp_100k"][name]["holm_p"] < 0.10
        c3 = main["variants"][name]["10k"]["sharpe"] >= spy_sharpe_10k
        c3_conservative = main["variants"][name]["10k_resize_fee"]["sharpe"] >= benchmarks["SPY"]["10k_resize_fee"]["sharpe"]
        c1_alt = alt["variants"][name]["zero"]["sharpe"] > alt_controls["random_zero"]["sharpe"]["p95"]
        c2_alt = alt_tests["vs_rsp_100k"][name]["holm_p"] < 0.10
        c3_alt = alt["variants"][name]["10k"]["sharpe"] >= spy_sharpe_10k
        verdicts[name] = {"c1": bool(c1), "c2": bool(c2), "c3": bool(c3), "pass": bool(c1 and c2 and c3),
                          "c3_conservative_costs": bool(c3_conservative),
                          "in_index_universe": {"c1": bool(c1_alt), "c2": bool(c2_alt), "c3": bool(c3_alt)}}

    top_examples = {name: {labels[j]: [universe.symbols[i] for i in main["picks"][name][j]]
                           for j in (0, len(labels) // 2, len(labels) - 1)} for name in VARIANTS}

    results = {
        "meta": universe.meta | {"years": years, "reps": REPS, "seed": SEED,
                                 "cash_months": [m for m, b in zip(labels, universe.spy_below_sma10, strict=True) if b]},
        "variants": main["variants"], "benchmarks": benchmarks, "controls": controls,
        "random": random_dist, "percentiles": percentiles, "random_with_m2_filter": random_m2_out,
        "tests": test_rows, "vs_spy_100k": spy_vs, "random_mean_vs_rsp": random_vs_rsp,
        "all_members_vs_rsp": all_vs_rsp, "crashes": crashes(universe, sims, random_zero),
        "in_index_universe": {"variants": alt_out, "controls": alt_controls},
        "diagnostics": diagnostics, "verdicts": verdicts, "picks_examples": top_examples,
    }
    (out_dir / "results.json").write_text(json.dumps(results, indent=1, default=float))
    monthly = pd.DataFrame(index=labels)
    for (name, key), sim in sims.items():
        monthly[f"{name}_{key}"] = sim.monthly[0]
    monthly["random_mean_zero"] = random_mean_series
    monthly["all_members_zero"] = all_members.monthly[0]
    for symbol in ("SPY", "RSP"):
        monthly[f"{symbol}"] = bench_monthly[symbol]["zero"]
    monthly["tbill"] = universe.cash_ret
    monthly["spy_below_sma10"] = universe.spy_below_sma10
    monthly.to_csv(out_dir / "monthly.csv")
    return results

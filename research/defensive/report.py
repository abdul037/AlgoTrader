"""Markdown tables from the study's results dictionary (``results.json``)."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ORDER = [
    "D1",
    "D2",
    "D3",
    "D4",
    "SPY buy-and-hold",
    "60/40 SPY/IEF",
    "EW5 rebalanced",
    "EW5 never rebalanced",
]
ROBUST = [f"{v} ({m}-month)" if m != 10 else v for v in ("D1", "D3") for m in (8, 9, 10, 11, 12)]
MAIN = "$10,000"


def pct(x: float, digits: int = 1, sign: bool = False) -> str:
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def yes(flag: bool) -> str:
    return "pass" if flag else "**fail**"


def _table(header: list[str], rows: list[list[str]], align: str | None = None) -> str:
    align = align or "|" + "|".join(["---"] + ["--:"] * (len(header) - 1)) + "|"
    lines = ["| " + " | ".join(header) + " |", align]
    lines += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(lines)


def main_table(res: dict, scen: str = MAIN) -> str:
    s = res["scenarios"][scen]["summary"]
    rows = []
    for name in ORDER:
        r = s[name]
        rows.append(
            [
                name,
                pct(r["cagr"]),
                f"{pct(r['max_dd'])} ({r['max_dd_peak']} .. {r['max_dd_trough']})",
                f"{r['sharpe']:.2f}",
                f"{r['sortino']:.2f}",
                pct(r["ann_vol"]),
                f"{pct(r['worst_year'], sign=True)} ({r['worst_year_label']})",
                f"{r['trades_per_year']:.1f} ({r['signal_trades_per_year']:.1f} / "
                f"{r['rebalance_trades_per_year']:.1f})",
                pct(r["time_in_market"], 0),
                f"${r['final_equity']:,.0f}",
                f"${r['fees_paid']:,.0f}",
            ]
        )
    return _table(
        [
            "strategy",
            "CAGR",
            "max DD (peak .. trough)",
            "Sharpe",
            "Sortino",
            "ann. vol",
            "worst full year",
            "trades/yr (signal / rebalance)",
            "time in market",
            "final equity",
            "fees paid",
        ],
        rows,
    )


def episode_table(res: dict) -> str:
    s = res["scenarios"][MAIN]["summary"]
    dates = res["episode_dates"]
    header = ["strategy"]
    for label in dates:
        header.append(f"{label}: peak..trough / max DD to trough+12m / to trough+12m")
    header.append("current (from 2024-12-31): return / max DD")
    rows = []
    for name in ORDER:
        r = s[name]
        row = [name]
        for label in dates:
            e = r["episodes"][label]
            row.append(
                f"{pct(e['peak_to_trough'], sign=True)} / {pct(e['max_dd_to_trough_plus_12m'])}"
                f" / {pct(e['peak_to_trough_plus_12m'], sign=True)}"
            )
        row.append(f"{pct(r['current']['return'], sign=True)} / {pct(r['current']['max_dd'])}")
        rows.append(row)
    lines = [
        _table(header, rows),
        "",
        "SPY peak and trough dates (adjusted closes): "
        + "; ".join(f"{k} {v[0]} .. {v[1]}" for k, v in dates.items())
        + ".",
    ]
    return "\n".join(lines)


def _year(years: dict, y: int) -> float:
    return years[y] if y in years else years[str(y)]


def year_table(res: dict) -> str:
    s = res["scenarios"][MAIN]["summary"]
    years = sorted(int(y) for y in s["SPY buy-and-hold"]["years"])
    header = ["strategy"] + [str(y) if y < 2026 else "2026 (to Oct)" for y in years]
    rows = [
        [name] + [pct(_year(s[name]["years"], y), 0, sign=True) for y in years] for name in ORDER
    ]
    return _table(header, rows)


def cost_table(res: dict) -> str:
    rows = []
    for name in ORDER:
        row = [name]
        for scen in ("zero cost", "$100,000", "$10,000", "$500", "$10,000, cash 0%"):
            r = res["scenarios"][scen]["summary"][name]
            row.append(
                f"wiped out by fees on {r['ruined']}" if r.get("ruined") else pct(r["cagr"], 2)
            )
        r10 = res["scenarios"]["$10,000"]["summary"][name]
        r5 = res["scenarios"]["$500"]["summary"][name]
        row += [
            f"{r10['trades_per_year']:.1f}",
            f"${r10['fees_paid']:,.0f}",
            f"${r5['fees_paid']:,.0f}",
        ]
        rows.append(row)
    return _table(
        [
            "strategy",
            "CAGR zero cost",
            "CAGR $100,000",
            "CAGR $10,000",
            "CAGR $500",
            "CAGR $10,000, cash 0%",
            "trades/yr",
            "fees paid $10,000",
            "fees paid $500",
        ],
        rows,
    )


def bootstrap_table(res: dict) -> str:
    s = res["scenarios"][MAIN]["summary"]
    rows = [["SPY buy-and-hold", f"{s['SPY buy-and-hold']['sharpe']:.2f}", "", "", ""]]
    names = [n for n in ORDER if n != "SPY buy-and-hold"] + [n for n in ROBUST if n not in ORDER]
    for name in names:
        b = res["bootstrap"][name]
        rows.append(
            [
                name,
                f"{s[name]['sharpe']:.2f}",
                f"{b['diff']:+.2f}",
                f"{b['ci95'][0]:+.2f} .. {b['ci95'][1]:+.2f}",
                f"{b['share_le_0']:.3f}",
            ]
        )
    return _table(
        [
            "strategy",
            "Sharpe",
            "minus SPY's",
            "95% interval (block bootstrap)",
            "share of resamples <= 0",
        ],
        rows,
    )


def robustness_table(res: dict) -> str:
    s = res["scenarios"][MAIN]["summary"]
    v = res["scenarios"][MAIN]["verdicts"]
    rows = []
    for name in ROBUST:
        r = s[name]
        crit = v["alt_rows"].get(name) or {
            "i": v["variants"][name]["i"],
            "ii": v["variants"][name]["ii"],
        }
        rows.append(
            [
                name,
                pct(r["cagr"]),
                pct(r["max_dd"]),
                f"{r['sharpe']:.2f}",
                f"{pct(r['worst_year'], sign=True)} ({r['worst_year_label']})",
                f"{r['trades_per_year']:.1f}",
                pct(r["time_in_market"], 0),
                pct(r["episodes"]["2008"]["max_dd_to_trough_plus_12m"]),
                pct(r["episodes"]["2022"]["max_dd_to_trough_plus_12m"]),
                yes(crit["i"]),
                yes(crit["ii"]),
            ]
        )
    return _table(
        [
            "setting",
            "CAGR",
            "max DD",
            "Sharpe",
            "worst full year",
            "trades/yr",
            "time in market",
            "2008 max DD",
            "2022 max DD",
            "(i) DD <= 1/2 SPY",
            "(ii) CAGR >= 75% SPY",
        ],
        rows,
    )


def verdict_table(res: dict) -> str:
    out = []
    for scen in ("$10,000", "$100,000", "zero cost", "$10,000, cash 0%", "$500"):
        v = res["scenarios"][scen]["verdicts"]
        s = res["scenarios"][scen]["summary"]
        rows = []
        for name in ("D1", "D2", "D3", "D4"):
            r, sv = v["variants"][name], s[name]
            if sv.get("ruined"):
                rows.append(
                    [
                        name,
                        f"**fail** (wiped out by fees on {sv['ruined']})",
                        "**fail**",
                        "n/a",
                        "fail",
                    ]
                )
                continue
            iii = f"{yes(r['iii'])} ({r['alts_passing']} of 4)" if "iii" in r else "n/a"
            rows.append(
                [
                    name,
                    f"{yes(r['i'])} ({pct(sv['max_dd'])} vs limit -{pct(v['dd_limit'])})",
                    f"{yes(r['ii'])} ({pct(sv['cagr'], 2)} vs floor {pct(v['cagr_floor'], 2)})",
                    iii,
                    "**PASS**" if r["pass"] else "fail",
                ]
            )
        title = "verdict (the rule)" if scen == MAIN else f"information only: {scen}"
        out.append(
            f"**{title}.** SPY: CAGR {pct(s['SPY buy-and-hold']['cagr'], 2)}, max DD "
            f"{pct(s['SPY buy-and-hold']['max_dd'])}.\n"
        )
        out.append(
            _table(
                [
                    "variant",
                    "(i) max DD <= half of SPY's",
                    "(ii) CAGR >= 75% of SPY's",
                    "(iii) holds at >= 3 of 4 other lengths",
                    "verdict",
                ],
                rows,
            )
        )
        out.append("")
    return "\n".join(out)


def mix_table(res: dict) -> str:
    s = res["scenarios"][MAIN]["summary"]
    rows = []
    for name, m in res["same_dd_mix"].items():
        r = s[name]
        rows.append(
            [
                name,
                pct(r["max_dd"]),
                pct(r["cagr"]),
                f"{r['sharpe']:.2f}",
                f"{m['weight']:.2f}",
                pct(m["cagr"]),
                f"{m['sharpe']:.2f}",
            ]
        )
    return _table(
        [
            "strategy",
            "max DD",
            "CAGR",
            "Sharpe",
            "SPY weight of the same-DD mix",
            "mix CAGR",
            "mix Sharpe",
        ],
        rows,
    )


def fund_table(res: dict) -> str:
    rows = [
        [
            f["fund"],
            f["from"],
            pct(f["cagr"]),
            f"{pct(f['max_dd'])} ({f['dd_peak']} .. " f"{f['dd_trough']})",
            f"{pct(f['worst_year'], sign=True)} ({f['worst_year_label']})",
        ]
        for f in res["funds"]
    ]
    return _table(["fund", "from", "CAGR", "max DD (peak .. trough)", "worst full year"], rows)


def render(res: dict) -> str:
    u = res["unadjusted"]
    parts = [
        f"Test period {res['start']} .. {res['end']} (capital at the {res['base']} close). "
        f"Cash: {res['cash_note']}; cash CAGR {pct(res['cash_cagr'], 2)}.",
        "",
        "## Main table ($10,000, after costs)",
        "",
        main_table(res),
        "",
        "## Main table ($100,000, after costs)",
        "",
        main_table(res, "$100,000"),
        "",
        "## Episodes ($10,000)",
        "",
        episode_table(res),
        "",
        "## Calendar years ($10,000)",
        "",
        year_table(res),
        "",
        "## Costs and account size",
        "",
        cost_table(res),
        "",
        "## Sharpe difference against SPY ($10,000)",
        "",
        bootstrap_table(res),
        "",
        "## Robustness ($10,000)",
        "",
        robustness_table(res),
        "",
        "## Verdicts",
        "",
        verdict_table(res),
        "## Same-max-DD SPY/cash mix (diagnostic added after results)",
        "",
        mix_table(res),
        "",
        "## D4 months",
        "",
        f"{res['d4_months']}; switches {res['d4_switches']}",
        "",
        "## Funds, buy-and-hold before costs",
        "",
        fund_table(res),
        "",
        "## Unadjusted-close signals",
        "",
        f"D1: {u['d1_months_differ']} of {u['d1_months']} months differ. D3: "
        f"{u['d3_sleeve_months_differ']} of {u['d3_sleeve_months']} sleeve-months differ "
        f"({u['d3_by_fund']}). D2: {u['d2_days_differ']} of {u['d2_days']} days differ.",
        "",
        "## Engine checks",
        "",
        json.dumps(res["engine_checks"]),
        "",
    ]
    return "\n".join(parts)


if __name__ == "__main__":  # python -I research/defensive/report.py OUT
    out = Path(sys.argv[1])
    print(render(json.loads((out / "results.json").read_text())))

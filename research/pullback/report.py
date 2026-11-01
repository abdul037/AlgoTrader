"""Render the study's markdown tables from ``results.json``, ``calibration.json`` and ``parity.json``.

    python -I research/pullback/report.py OUT > OUT/tables.md
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ORDER = ["V1", "V2", "V3", "V4", "V5", "V6", "V7", "V8", "V9"]
NAMED_YEARS = (2018, 2020, 2022)


def _r(x: float | None, digits: int = 3) -> str:
    return "n/a" if x is None else f"{x:+.{digits}f}"


def _pct(x: float, digits: int = 1) -> str:
    return f"{100 * x:.{digits}f}%"


def _p(x: float) -> str:
    return "<1e-10" if x < 1e-10 else (f"{x:.2e}" if x < 1e-3 else f"{x:.3f}")


def _yn(flag: bool) -> str:
    return "pass" if flag else "**fail**"


def main_table(res: dict) -> str:
    spy = res["spy"]
    lines = [
        f"SPY buy-and-hold, same dates: CAGR {_pct(spy['cagr'])}, max DD {_pct(spy['max_drawdown'])}, "
        f"Sharpe {spy['sharpe']:.2f}.",
        "",
        "| variant | trades | win % | zero-cost R | after-cost R | day-t | Holm p | years + | falling R (n) | rising R (n) "
        "| sideways R (n) | CAGR | max DD | Sharpe | (a) | (b) | (c) | verdict |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|--:|---|---|---|---|",
    ]
    for name in ORDER:
        v = res["variants"][name]
        t, port, rule = v["trade_level"], v["portfolio"], v["pass"]
        cond = t["conditions"]

        def bucket(key: str, conditions: dict = cond) -> str:
            c = conditions.get(key)
            return "none" if c is None else f"{c['r']:+.3f} ({c['n']})"

        lines.append(
            f"| {name} {v['label']} | {t['trades']} | {100 * t['win_rate']:.1f} | {_r(t['r_gross'])} | {_r(t['r_net'])} "
            f"| {t['t_day']:+.1f} | {_p(v['holm_p'])} | {t['years_positive']}/{t['years_total']} | {bucket('falling')} "
            f"| {bucket('rising')} | {bucket('sideways')} | {_pct(port['cagr'])} | {_pct(port['max_drawdown'])} "
            f"| {port['sharpe']:.2f} | {_yn(rule['a'])} | {_yn(rule['b'])}{' (vacuous)' if rule['falling_empty'] else ''} "
            f"| {_yn(rule['c'])} | {'**PASS**' if rule['all'] else 'fail'} |"
        )
    return "\n".join(lines)


def portfolio_table(res: dict) -> str:
    lines = [
        "| variant | trades taken | their after-cost R | CAGR | max DD | Sharpe | ann. vol | exposure | SPY mix with the same max DD: weight / CAGR / Sharpe | signals skipped (no slot / no cash) |",
        "|---|--:|--:|--:|--:|--:|--:|--:|---|--:|",
    ]
    for name in ORDER:
        v = res["variants"][name]
        port, pt = v["portfolio"], v["portfolio_trades"]
        mix = port["spy_matched_dd"]
        lines.append(
            f"| {name} | {port['trades']} | {_r(pt.get('r_net'))} | {_pct(port['cagr'])} | {_pct(port['max_drawdown'])} "
            f"| {port['sharpe']:.2f} | {_pct(port['ann_vol'])} | {_pct(port['exposure'], 0)} "
            f"| {port['spy_matched_dd_weight']:.2f} / {_pct(mix['cagr'])} / {mix['sharpe']:.2f} "
            f"| {port['skipped_no_slot']} / {port['skipped_no_cash']} |"
        )
    spy = res["spy"]
    half = res["variants"]["V1"]["portfolio"]["spy_50_50"]
    lines.append(f"| SPY buy-and-hold | | | {_pct(spy['cagr'])} | {_pct(spy['max_drawdown'])} | {spy['sharpe']:.2f} "
                 f"| {_pct(spy['ann_vol'])} | 100% | | |")
    lines.append(f"| SPY 50% / cash 50% (daily rebalanced) | | | {_pct(half['cagr'])} | {_pct(half['max_drawdown'])} "
                 f"| {half['sharpe']:.2f} | {_pct(half['ann_vol'])} | 50% | | |")
    return "\n".join(lines)


def years_table(res: dict) -> str:
    years = sorted({int(y) for name in ORDER for y in res["variants"][name]["trade_level"]["years"]})
    head = "| variant | " + " | ".join(f"**{y}**" if y in NAMED_YEARS else str(y) for y in years) + " |"
    lines = [head, "|---|" + "--:|" * len(years)]
    for name in ORDER:
        ys = res["variants"][name]["trade_level"]["years"]
        cells = []
        for y in years:
            item = ys.get(str(y)) or ys.get(y)
            cells.append("" if item is None else f"{item['r']:+.2f}")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    port = res["variants"]["V1"]["portfolio"]
    lines.append("| SPY total return | " + " | ".join(f"{100 * port['spy_yearly_return'].get(str(y), 0):+.0f}%" for y in years) + " |")
    return "\n".join(lines)


def named_years_table(res: dict) -> str:
    lines = ["| variant | " + " | ".join(f"{y}: trades / after-cost R / portfolio return" for y in NAMED_YEARS) + " |",
             "|---|" + "---|" * len(NAMED_YEARS)]
    for name in ORDER:
        v = res["variants"][name]
        ys, port = v["trade_level"]["years"], v["portfolio"]["yearly_return"]
        cells = []
        for y in NAMED_YEARS:
            item = ys.get(str(y))
            cells.append(f"{item['n']} / {item['r']:+.3f} / {100 * port[str(y)]:+.1f}%" if item else "none")
        lines.append(f"| {name} | " + " | ".join(cells) + " |")
    spy = res["variants"]["V1"]["portfolio"]["spy_yearly_return"]
    lines.append("| SPY | " + " | ".join(f"{100 * spy[str(y)]:+.1f}%" for y in NAMED_YEARS) + " |")
    return "\n".join(lines)


def filters_table(res: dict) -> str:
    lines = [
        "| filter | days it is off | V1 trades removed | removed: after-cost R (win %) | removed: day-t / month-t | V1 trades kept | kept: after-cost R (win %) | kept: day-t / month-t | R removed in total | variant's own trades |",
        "|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|",
    ]
    for name in ("V2", "V3", "V7", "V8", "V9"):
        f = res["filters"][name]
        k, rm = f["kept"], f["removed"]
        lines.append(
            f"| {name} {res['variants'][name]['label']} | {_pct(f['days_off_share'], 0)} | {rm['n']} "
            f"| {rm['r_net']:+.3f} ({100 * rm['win_rate']:.0f}%) | {rm['t_day']:+.1f} / {rm['t_month']:+.1f} | {k['n']} "
            f"| {k['r_net']:+.3f} ({100 * k['win_rate']:.0f}%) | {k['t_day']:+.1f} / {k['t_month']:+.1f} "
            f"| {rm['sum_r_net']:+.0f} R | {f['variant_trades']} |"
        )
    return "\n".join(lines)


def survivorship_table(res: dict) -> str:
    spy = res["spy"]
    lines = ["| variant | after-cost R | adjusted R (day-t) | CAGR | adjusted CAGR | Sharpe | adjusted Sharpe | adjusted max DD | (c) on adjusted figures |",
             "|---|--:|--:|--:|--:|--:|--:|--:|---|"]
    for name in ORDER:
        v = res["variants"][name]
        adj = v["survivorship_adjusted"]
        ap = adj["portfolio"]
        c_ok = ap["sharpe"] > spy["sharpe"] or (abs(ap["max_drawdown"]) <= 0.5 * abs(spy["max_drawdown"])
                                                  and ap["cagr"] >= 0.7 * spy["cagr"])
        lines.append(f"| {name} | {_r(v['trade_level']['r_net'])} | {_r(adj['r_net'])} ({adj['t_day']:+.1f}) "
                     f"| {_pct(v['portfolio']['cagr'])} | {_pct(ap['cagr'])} | {v['portfolio']['sharpe']:.2f} "
                     f"| {ap['sharpe']:.2f} | {_pct(ap['max_drawdown'])} | {'pass' if c_ok else 'fail'} |")
    return "\n".join(lines)


def parity_table(parity: dict, calib: dict) -> str:
    lines = ["| symbol | bars checked | first bar | BUY (real = vectorised) | SELL (real = vectorised) | mismatches | max rel. diff stop / target | engine trades | identical trades |",
             "|---|--:|---|--:|--:|--:|---|--:|--:|"]
    exec_by = {x["symbol"]: x for x in calib["execution_parity"]}
    for item in parity["symbols"]:
        ex = exec_by.get(item["symbol"], {})
        lines.append(f"| {item['symbol']} | {item['bars']} | {item['first_bar']} | {item['buy_real']} = {item['buy_vec']} "
                     f"| {item['sell_real']} = {item['sell_vec']} | {len(item['mismatches'])} "
                     f"| {item['max_rel_diff_stop']:.0e} / {item['max_rel_diff_target']:.0e} "
                     f"| {ex.get('engine_trades', '')} | {ex.get('matched_exact', '')} |")
    return "\n".join(lines)


def control_table(calib: dict, res: dict) -> str:
    c = calib["random_entry_control"]
    years = sorted(c["by_year"], key=int)
    lines = ["| | " + " | ".join(years) + " | all |", "|---|" + "--:|" * (len(years) + 1)]
    lines.append("| V1 after-cost R | " + " | ".join(f"{c['by_year'][y]['v1']:+.2f}" for y in years) + f" | {c['v1_r_net']:+.3f} |")
    lines.append("| random-entry control | " + " | ".join(f"{c['by_year'][y]['control']:+.2f}" for y in years) + f" | {c['control_r_net']:+.3f} |")
    return "\n".join(lines)


def main(out: Path) -> None:
    res = json.loads((out / "results.json").read_text())
    calib = json.loads((out / "calibration.json").read_text())
    parity = json.loads((out / "parity.json").read_text())
    for title, body in (
        ("main", main_table(res)), ("portfolio", portfolio_table(res)), ("years", years_table(res)),
        ("named years", named_years_table(res)), ("filters", filters_table(res)),
        ("survivorship", survivorship_table(res)), ("parity", parity_table(parity, calib)),
        ("control", control_table(calib, res)),
    ):
        print(f"<!-- {title} -->\n{body}\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

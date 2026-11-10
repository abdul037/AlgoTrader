"""Markdown tables for docs/research/bigger_moves_study.md from the JSON outputs.

    python -I research/bigger_moves/report.py OUT > OUT/tables.md

Reads ``parity.json``, ``results_main.json`` and, when present, ``results_holdout.json`` and
``calibration.json``.
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from research.bigger_moves import variants as var_mod  # noqa: E402


def r(value: object, digits: int = 3) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{float(value):+.{digits}f}"


def num(value: object, digits: int = 1) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    return f"{float(value):.{digits}f}"


def p(value: object) -> str:
    if value is None or (isinstance(value, float) and not math.isfinite(value)):
        return "n/a"
    value = float(value)
    return "<1e-4" if value < 1e-4 else f"{value:.4f}" if value < 0.01 else f"{value:.3f}"


def mark(ok: bool) -> str:
    return "pass" if ok else "**fail**"


def table(header: list[str], rows: list[list[str]], align: str | None = None) -> str:
    align = align or "|".join(["---"] + ["--:"] * (len(header) - 1))
    lines = ["| " + " | ".join(header) + " |", "|" + align + "|"]
    lines += ["| " + " | ".join(row) + " |" for row in rows]
    return "\n".join(lines)


def name(key: str) -> str:
    strategy, variant = key.split(":")
    return f"{var_mod.SHORT[strategy]} {variant}"


def parity_section(parity: dict) -> str:
    rows = []
    for item in parity["signal"]["results"]:
        rows.append([item["symbol"], item["strategy"], item["timeframe"], str(item["bars"]),
                     f"{item['buy_real']} = {item['buy_vec']}" if item["buy_real"] == item["buy_vec"] else f"{item['buy_real']} / {item['buy_vec']}",
                     f"{item['sell_real']} = {item['sell_vec']}", str(len(item["mismatches"])),
                     f"{item['max_rel_stop']:.0e} / {item['max_rel_target']:.0e}"])
    out = [table(["symbol", "strategy", "tf", "bars", "BUY real = vectorised", "SELL", "mismatches",
                  "max rel. diff stop / target"], rows, "---|---|---|--:|--:|--:|--:|---")]
    exec_rows: dict[str, list[int]] = {}
    for item in parity["execution"]:
        if "skipped" in item:
            continue
        acc = exec_rows.setdefault(item["variant"], [0, 0, 0])
        acc[0] += item.get("engine_trades", 0)
        acc[1] += item.get("our_trades", 0)
        acc[2] += item.get("identical", 0)
    rows = [[name(key), str(a), str(b), str(c)] for key, (a, b, c) in exec_rows.items()]
    out.append("")
    out.append(table(["variant", "engine trades", "study trades", "identical"], rows))
    return "\n".join(out)


def main_section(main: dict, holdout: dict | None) -> str:
    rows = []
    for key, item in main["variants"].items():
        pit = item["pit"]
        if not pit.get("trades"):
            rows.append([name(key), item["change"], "0"] + ["n/a"] * 11)
            continue
        hold = holdout["variants"][key]["pit"] if holdout else {}
        rows.append([
            name(key), item["change"], str(pit["trades"]), num(100 * pit["win_rate"]), r(pit["r_gross"]),
            r(pit["r_net"]), r(pit["r_nofin"]),
            f"{num(pit['spread_usd'], 2)} / {num(pit['fee_usd'], 2)} / {num(pit['fin_usd'], 2)}",
            num(pit["bars_held_median"], 0), r(pit["ctrl_net"]), r(pit["excess"]), num(pit["excess_t_month"], 2),
            p(pit["holm_p"]),
            f"{hold.get('trades', 'n/a')} / {r(hold.get('r_net'))}" if holdout else "sealed",
        ])
    return table(["variant", "change", "trades", "win %", "before costs R", "after costs R", "no-financing R",
                  "cost $ spread / fee / financing", "median bars", "random control R", "excess", "month-t",
                  "Holm p", "holdout trades / R"], rows, "---|---|" + "|".join(["--:"] * 12))


def stability_section(main: dict) -> str:
    rows = []
    for key, item in main["variants"].items():
        pit = item["pit"]
        if not pit.get("trades"):
            continue
        subs = " / ".join(f"{row['n']}: {r(row['r_net'], 2)} ({r(row['excess'], 2)})" for row in pit["sub_periods"])
        drop = pit.get("drop_best_year", {})
        rows.append([name(key), subs, str(sum(row["holds"] for row in pit["sub_periods"])),
                     f"{drop.get('year')}: {r(drop.get('r_net'))} ({r(drop.get('excess'))})"])
    return table(["variant", "trades: after-cost R (excess) in 2011-15 / 2016-20 / 2021-25", "sub-periods holding",
                  "without the best year: R (excess)"], rows, "---|---|--:|---")


def universe_section(main: dict) -> str:
    rows = []
    for key, item in main["variants"].items():
        pit, alls, live = item["pit"], item["all"], item["live24"]
        rows.append([name(key),
                     f"{pit.get('trades', 0)} / {r(pit.get('r_net'))} / {r(pit.get('excess'))}",
                     f"{alls.get('trades', 0)} / {r(alls.get('r_net'))} / {r(alls.get('excess'))}",
                     f"{live.get('trades', 0)} / {r(live.get('r_net'))} / {r(live.get('ctrl_net'))} / "
                     f"{r(live.get('excess'))} ({num(live.get('excess_t_month'), 2)})"])
    return table(["variant", "point-in-time: trades / R / excess", "today's members, no restriction: trades / R / excess",
                  "live 24, 2011-2025: trades / R / control R / excess (month-t)"], rows, "---|---|---|---")


def verdict_section(main: dict, holdout: dict | None) -> str:
    rows = []
    for key, item in main["variants"].items():
        checks = holdout["variants"][key]["checks"] if holdout else item["checks_main"]
        rows.append([name(key), mark(checks["rule1_main"]),
                     mark(checks["rule1_holdout"]) if holdout else "sealed",
                     mark(checks["rule2"]), "yes" if checks["rule2_at_0.10"] else "no",
                     mark(checks["rule3a"]), mark(checks["rule3b"]), "by construction",
                     ("**PASS**" if checks["pass"] else "fail") if holdout else "pending"])
    return table(["variant", "1 main >= +0.03R, >= 40", "1 holdout > 0, >= 10", "2 beats control (Holm p < 0.05)",
                  "2 at 0.10?", "3a 2 of 3 sub-periods", "3b not one year", "4 point-in-time", "verdict"], rows,
                 "---|---|---|---|---|---|---|---|---")


def holdout_live_section(holdout: dict) -> str:
    rows = []
    for key, item in holdout["variants"].items():
        pit, live = item["pit"], item["live24"]
        rows.append([name(key), f"{pit.get('trades') or 0} / {r(pit.get('r_net'))} / {r(pit.get('r_nofin'))}",
                     f"{live.get('trades') or 0} / {r(live.get('r_net'))}"])
    return table(["variant", "point-in-time holdout: trades / R / no-financing R", "live 24 holdout: trades / R"], rows,
                 "---|---|---")


def calibration_section(cal: dict) -> str:
    rows = []
    for strategy in var_mod.STRATEGIES:
        prod = cal["production_brief"][strategy]
        rep = cal["replication"][strategy]["oos"]
        hold = cal["replication"][strategy]["holdout"]
        ours = cal["study"][strategy]
        rows.append([
            var_mod.SHORT[strategy], f"{prod[0]} / {r(prod[1])} / {r(prod[2])}",
            f"{rep.get('trades', 0)} / {r(rep.get('r_gross'))} / {r(rep.get('r_net'))}",
            f"{num(rep.get('spread_usd'), 2)} / {num(rep.get('fee_usd'), 2)} / {num(rep.get('fin_usd'), 2)}",
            num(100 * rep.get("end_of_data_share", float("nan")), 0), num(rep.get("median_days_held"), 0),
            f"{hold.get('trades', 0)} / {r(hold.get('r_net'))}",
            f"{ours.get('trades', 0)} / {r(ours.get('r_gross'))} / {r(ours.get('r_net'))}",
            num(ours.get("median_bars_held"), 0), str(cal["replication"][strategy].get("entries_also_in_study")),
        ])
    return table(["strategy", "production (brief): trades / before / after", "replication: trades / before / after",
                  "replication cost $ spread / fee / financing", "closed at fold end %", "median days held",
                  "replication holdout: trades / R", "study V0, no folds: trades / before / after",
                  "study median bars", "replicated entries also in study"], rows, "---|---|---|---|--:|--:|---|---|--:|--:")


def main(out: Path) -> None:
    parity = json.loads((out / "parity.json").read_text()) if (out / "parity.json").exists() else None
    main_results = json.loads((out / "results_main.json").read_text())
    holdout_path = out / "results_holdout.json"
    holdout = json.loads(holdout_path.read_text()) if holdout_path.exists() else None
    cal_path = out / "calibration.json"
    print("<!-- generated by research/bigger_moves/report.py -->\n")
    if parity:
        print("## parity\n")
        print(parity_section(parity) + "\n")
    print("## main\n")
    print(main_section(main_results, holdout) + "\n")
    print("## stability\n")
    print(stability_section(main_results) + "\n")
    print("## universes\n")
    print(universe_section(main_results) + "\n")
    print("## verdict\n")
    print(verdict_section(main_results, holdout) + "\n")
    if holdout:
        print("## holdout\n")
        print(holdout_live_section(holdout) + "\n")
        print("passing:", holdout["passing"], "\n")
    if cal_path.exists():
        print("## calibration\n")
        print(calibration_section(json.loads(cal_path.read_text())) + "\n")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

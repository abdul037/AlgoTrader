"""Compare two strategy audit JSON reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, help="path to baseline audit JSON")
    parser.add_argument("--candidate", required=True, help="path to candidate audit JSON")
    parser.add_argument("--output", required=True, help="output markdown path")
    args = parser.parse_args()

    baseline = json.loads(Path(args.baseline).read_text())
    candidate = json.loads(Path(args.candidate).read_text())

    baseline_by_strategy = {r["strategy"]: r for r in baseline["results"]}
    candidate_by_strategy = {r["strategy"]: r for r in candidate["results"]}

    rows = []
    for name in sorted(set(baseline_by_strategy) | set(candidate_by_strategy)):
        br = baseline_by_strategy.get(name, {})
        cr = candidate_by_strategy.get(name, {})
        rows.append(
            {
                "strategy": name,
                "baseline_sharpe": br.get("sharpe", 0.0),
                "candidate_sharpe": cr.get("sharpe", 0.0),
                "sharpe_delta": cr.get("sharpe", 0.0) - br.get("sharpe", 0.0),
                "baseline_deflated": br.get("deflated_sharpe", 0.0),
                "candidate_deflated": cr.get("deflated_sharpe", 0.0),
                "deflated_delta": cr.get("deflated_sharpe", 0.0) - br.get("deflated_sharpe", 0.0),
                "baseline_verdict": br.get("verdict", "missing"),
                "candidate_verdict": cr.get("verdict", "missing"),
                "verdict_changed": br.get("verdict") != cr.get("verdict"),
            }
        )

    rows.sort(key=lambda r: r["sharpe_delta"], reverse=True)
    md = ["# Audit Comparison: Baseline vs Candidate Cost Model\n"]
    md.append(f"- Baseline: {args.baseline}")
    md.append(f"- Candidate: {args.candidate}\n")
    md.append("Sorted by sharpe_delta descending. Positive delta means strategy improved under candidate cost model.\n")
    md.append("| strategy | base_sharpe | cand_sharpe | delta_sharpe | base_deflated | cand_deflated | delta_deflated | base_verdict | cand_verdict | changed |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---|---|:-:|")
    for row in rows:
        md.append(
            "| "
            f"{row['strategy']} | "
            f"{row['baseline_sharpe']:.4f} | "
            f"{row['candidate_sharpe']:.4f} | "
            f"{row['sharpe_delta']:+.4f} | "
            f"{row['baseline_deflated']:.4f} | "
            f"{row['candidate_deflated']:.4f} | "
            f"{row['deflated_delta']:+.4f} | "
            f"{row['baseline_verdict']} | "
            f"{row['candidate_verdict']} | "
            f"{'yes' if row['verdict_changed'] else ''} |"
        )
    md.append("\n## Summary\n")
    md.append(f"- Verdict changes: {sum(row['verdict_changed'] for row in rows)} of {len(rows)} strategies")
    md.append(
        "- Newly production candidate: "
        f"{sum(1 for row in rows if row['candidate_verdict'] == 'production candidate' and row['baseline_verdict'] != 'production candidate')}"
    )
    md.append(
        "- Newly no edge at this confidence: "
        f"{sum(1 for row in rows if row['candidate_verdict'] == 'no edge at this confidence' and row['baseline_verdict'] != 'no edge at this confidence')}"
    )
    if rows:
        md.append(f"- Largest sharpe improvement: {rows[0]['strategy']} ({rows[0]['sharpe_delta']:+.4f})")
        md.append(f"- Largest sharpe degradation: {rows[-1]['strategy']} ({rows[-1]['sharpe_delta']:+.4f})")

    Path(args.output).write_text("\n".join(md), encoding="utf-8")
    print(f"Wrote {args.output}")


if __name__ == "__main__":
    main()

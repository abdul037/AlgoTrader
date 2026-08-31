# Monday validation runbook

Read-only checks to run before the week starts. Nothing here enables a feature
or places a trade — each step only measures, so you can decide what (if
anything) to flip on. Everything stays paper-only (`ENABLE_REAL_TRADING=false`).

## 0. Confirm the bot is alive

```
curl -s https://<railway-app>/health/ready
```

Expect `ready: true`. If not, check the Railway logs before anything else — the
scheduler self-heals, but a cold deploy needs a minute.

## 0.5 Pre-open readiness — will today accrue data, safely?

```
python -m scripts.preopen_check          # human-readable
python -m scripts.preopen_check --json    # machine-readable (exit 2 on NO-GO)
```

One GO / WARN / NO-GO readout across four groups — Safety (paper-only, not
paused, kill-switch off), Deploy (running commit), Trading mode, and the gated
baseline. The group that catches the common silent failure is **Trading mode**:
if `paper_auto_operation_mode` is `shadow`, the bot proposes but places nothing,
so the day accrues **zero** closed trades and Stage 1 never advances. For the
day to produce measurement data the mode must be `supervised`/`unattended` with
propose + approve + execute all open. Fix it in the Railway env (not the repo),
redeploy, and re-run this check until Trading mode reads GO.

## 1. Gated-feature validation

Run on the deployed bot (it has market data + the paper DB):

```
python -m scripts.validate_gated_features          # human-readable readout
python -m scripts.validate_gated_features --json    # machine-readable JSON
```

The `--json` form emits `{"features": [...], "overall": "..."}` (each feature
carries its flag, current value, verdict, detail, and metrics) so the dashboard
or notification layer can surface the verdicts automatically. `overall` is the
most-blocking verdict across the three.

You get three sections, each ending in a verdict:

| Verdict | Meaning |
|---|---|
| `GO` | Safe/expected to enable if it matches intent |
| `NO-GO` | The data says leave it off |
| `REVIEW` | A trade-off only you can judge — read the numbers |
| `NEED-DATA` | Can't decide yet (no benchmark history / no closed trades) |

- **Regime router** (`regime_router_enabled`) — shows the current market regime
  and which whole strategy families it would switch off *right now*, per
  timeframe. `GO` when the regime is broad enough that it drops nothing;
  `REVIEW` when it would drop families — enable only if those should be off.
- **Drawdown governor** (`drawdown_governor_enabled`) — replays the closed
  paper-trade history with vs without the governor. `GO` when it cuts max
  drawdown for an acceptable P&L give-up; `NO-GO` when it doesn't reduce
  drawdown. `NEED-DATA` until Stage 1 accrues closed trades.
- **Cross-sectional momentum** (`cross_sectional_momentum_enabled`) — a
  scan-time filter, so its effect shows in the *next* scan's kept count. The
  section prints the configured concentration (`top_pct`); enable when you want
  leader-only exposure, then watch the kept count on the following scan.

## 2. Stage readiness

The Stage 3 readiness assessment and track-record report gate the
paper→capital decision (days live, Sharpe, drawdown, hit rate). Pull them from
the dashboard at `/dashboard` or the Stage 3 endpoint. This stays `NEED-DATA`
until the paper track record is long enough — that's expected early in Stage 1.

## 3. Enabling a feature (only if a verdict says so)

Flip the single flag in the Railway environment (not in the repo), redeploy,
and confirm the next scan/report reflects it. Change **one** flag at a time so
the measurement stays attributable. Never enable a feature on a `NEED-DATA`
verdict — there's no evidence yet.

> The validation script and this runbook change nothing on their own. They exist
> so every enable decision is grounded in the bot's own data, not a hunch.

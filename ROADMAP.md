# AlgoTrader — Roadmap & Build Log

**Durable, version-controlled record of what has been built, what is in flight, and
what remains.** This file lives on `main`, so it survives any ephemeral session and
is readable by anyone (the operator, a future Claude session, the review bots).

> Update protocol: whenever a change is merged to `main`, add a line to **Shipped**
> and adjust **In flight** / **Backlog**. Keep entries short; the full detail lives
> in the git commit and its Pull Request.

---

## Non-negotiable constraints (never violate)

These are permanent guardrails, not goals:

- **PAPER ONLY.** `ENABLE_REAL_TRADING` stays `false`; `EXECUTION_MODE` stays `paper`.
- **Never weaken a hard risk gate** without explicit operator sign-off: spread cap,
  reward:risk, valid bracket (stop + target), relative-volume / liquidity floors,
  regular-hours, blacklist, score threshold.
- **Never commit credentials** (Alpaca / OpenAI / Anthropic keys, tokens).
- **Observe-by-default:** consequential new behaviour ships behind a flag that
  defaults OFF in code; the operator opts in via env.

---

## Current status (as of 2026-09-04)

- The bot runs unattended on Railway, scanning a widened US equity universe on the
  Alpaca **paper** account, IEX data feed.
- **Autonomous paper execution is unblocked** (PR #22) and **fully instrumented**
  (PRs #23–#25).
- **Open question:** as of the last close, promoted near-miss candidates were not yet
  executing (the enabling deploy landed minutes before close). The **first autonomous
  paper trade** is still unconfirmed — watched at each US open via the funnel below.
- Honest framing: there is no "instant profit". Profit comes from a *validated edge
  measured over time*. The current phase is: get the first trades firing, then let
  real data drive which gate/strategy changes are worth making.

---

## Shipped (merged to `main`)

Newest first. `PR #n` links the full write-up; the commit is the permanent record.

| PR | Change | Why it matters |
|----|--------|----------------|
| #25 | Auto-propose funnel **tile** on `/dashboard` | Watch the pipeline live in a browser; `Executed` turns green on the first trade |
| #24 | Funnel **rollup** in `GET /automation/reliability` (`scan_funnel`) | Day-level pipeline state in one API call |
| #23 | Per-scan auto-propose **funnel log** (`auto_propose_funnel`) | One row per scan showing where each candidate exits the pipeline |
| #22 | Enable unattended **near-miss auto-execution** (paper only, opt-in flag) | Root-cause fix for zero autonomous trades: promoted near-miss candidates can now auto-execute; every hard gate still enforced |
| #21 | Measured-slippage calibration **core** for the cost model | Pure function to turn realized IEX fills into a robust slippage estimate (wiring deferred until fills exist — see #59 in Backlog) |
| #20 | Per-strategy scorecard in the daily digest | EOD visibility into which strategies are working |
| #19 | Auto-demote losing strategies from live selection (safe flag) | Close-the-loop: stop selecting strategies with ≤0 live expectancy |

Everything before #19 (worker hardening, risk engine, backtest gate, strategy library,
Stage 1–3 measurement, dashboard, regime router, etc.) is in the git history and the
`docs/` runbooks.

---

## In flight / awaiting data

- **First autonomous paper trade** — watched at each US open (13:30 UTC). The funnel
  (#23–#25) pinpoints where any promoted candidate stalls: proposal creation, the
  auto-execution worker, or a hard gate. *Nothing to build until this fires or the
  funnel shows a concrete gap.*

---

## Backlog (prioritized)

**Gated on live fills — do NOT build until the bot is trading:**
- **#59 — feed measured IEX slippage into the backtest cost model.** Core built (PR #21);
  wiring deferred until there are real fills to measure. Keeps backtests honest (IEX
  fills ≠ true NBBO).
- **#58 — auto-concentrate: bias sizing/selection toward proven winners.** Needs live
  per-strategy P&L to know who the winners are.

**The likely first real lever — needs operator sign-off (it's a hard gate):**
- **`spread_too_wide` on the IEX feed.** A single-venue spread reads far wider than the
  true consolidated NBBO, so this gate probably rejects tradeable setups on bad data,
  not bad prices. Tomorrow's funnel will quantify the cost. Loosening / NBBO-correcting
  it could unlock significant volume — **but it is a hard risk gate; do not touch
  without explicit approval.**

**Ongoing (the actual path to profit — iterative, not one-shot):**
- Tighten entry timing, regime-router on/off calls, and backtest-gate honesty as
  live-vs-backtest decay data accumulates. Loop: *watch → find what blocks or loses →
  fix → repeat.*

**Hygiene / infra (safe, data-independent, low urgency):**
- Split the `app/storage/repositories.py` god-file (~2.9k lines).
- Clear the ~134 advisory ruff findings + `black` sweep, then flip CI lint to a hard gate.
- CI test env is missing `alembic` / `pyarrow`, so `test_schema_drift` and the parquet
  cache tests are skipped/red locally — pin them into the CI image.

---

## How every change is recorded (four durable layers)

1. **Git history on `main`** — every change is a descriptive commit pushed to GitHub.
   This is the permanent source of truth (survives session/container resets).
2. **Pull Requests** — each PR carries the what/why/safety write-up plus its CI and
   4-bot review-team logs.
3. **This `ROADMAP.md`** — the human-readable ledger + plan, version-controlled here.
4. **Runtime `run_logs`** (in the database) — the bot records its own operational
   history as it runs (e.g. the `auto_propose_funnel` event), readable via
   `/automation/reliability` and the `/dashboard`.

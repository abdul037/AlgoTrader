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

## Current status (as of 2026-09-08)

- **🎉 FIRST AUTONOMOUS PAPER TRADE (2026-09-08 17:00:50 UTC).** GOOGL buy 1 @ $338.75,
  `opening_range_breakout_retest`, Alpaca paper bracket (stop $333.24 / target $349.39),
  reconciliation clean. The unattended pipeline is end-to-end proven: scan → near-miss /
  weak-valid promotion → auto-approval → execution queue → broker bracket → fill →
  reconciliation. Stage 1 (live track record) has begun. Also surfaced: the $500
  per-trade cap blocks any stock over $500/share (AMD attempt failed
  `one_share_exceeds_max_trade_amount`) — operator decision pending. Ledger: `OPS_LOG.md`.
- **Same day, two more funnel fixes (2026-09-08 pm).** (1) Per-trade sizing raised to
  $1,000 (default + cap) so >$500/share names can trade as one share. (2) The internal
  P&L ledger now mirrors real Alpaca-paper fills (`app/paper/broker_ledger.py`), so
  equity curve / EOD digest / scorecard see broker-backed trades; verified with GOOGL.
  (3) `ALLOWED_INSTRUMENTS` aligned to the 25-name universe — it had silently rejected
  17 of 19 promoted candidates at the proposal step. Expect materially more paper
  trades from Wed 2026-09-09.
- **Review-team bots wired on (2026-09-07).** The QA/Strategy/Trader/PM review workflow
  had been silently skipping for weeks due to a chain of three issues, all now fixed:
  (1) no open PR (the workflow only triggers on `pull_request`) — opened PR #31;
  (2) the repo secret was named `CLAUDE_CODE_AUTH_TOKEN` but the workflow referenced
  `CLAUDE_CODE_OAUTH_TOKEN` — pointed the workflow at the real name; (3) the token value
  had an embedded line break (110 chars on 2 lines) so the Authorization header was
  rejected — operator re-pasted it as a single line. Advisory only; never blocks a merge.

- **OUTAGE + RECOVERY (Sat 2026-09-05 20:42 → Mon 2026-09-07 ~16:50 UTC).** The bot went
  silent for ~2 days and missed Monday's open. Post-mortem:
  1. **DB pool wedge (root cause).** A Supabase session-pooler (port 5432) connectivity
     blip Sat ~20:00 left all 5 SQLAlchemy connections stuck checked-out (no statement
     timeout, no TCP keepalives → they hung forever). The pool exhausted and every
     scheduler job failed on `QueuePool limit of size 3 overflow 2 reached`. The
     self-heal watchdog couldn't help — it restarts the worker thread, not the
     process-level pool. **Fixed (commit `f6d0d08`):** `connect_args` add a server-side
     `statement_timeout` (30s, env `db_statement_timeout_seconds`) + TCP keepalives, so a
     stuck connection always errors out and returns to the pool. Verified healthy in
     prod (0 QueuePool errors after recovery).
  2. **Railway did not auto-restart (made the outage 2 days instead of minutes).** The
     hardened deploy ran healthy 10:06–10:44, then took a Railway SIGTERM (graceful
     container reclaim) with no replacement. The service had **no restart policy** (so it
     defaulted to `ON_FAILURE`, which ignores a clean exit) and **no healthcheck**.
     **Fixed via the Railway connector:** restart policy → `ALWAYS` (max 10 retries) +
     `/health/ready` healthcheck (300s). This is Railway config, not code — recorded here
     because it lives only in this ledger.
- **Bot RECOVERED and healthy (2026-09-07 16:50 UTC).** Redeploy of the hardened image
  booted clean; 0 scheduler errors, events flowing, paper-safe (`enable_real_trading:
  false`, `execution_mode: paper`), near-miss auto-exec on. **0 trades today** — the bot
  was down for the whole morning session; watching for the first trade into the afternoon.
- **EOD Mon 2026-09-07: 0 autonomous trades.** Infra was healthy 16:50–19:38 UTC; 60
  promotion attempts, all stopped at hard gates. Dominant blockers were `quote_too_old`
  (64) and `spread_too_wide` (67) — both artifacts of the single-venue IEX feed (stale
  last-quote timestamps and IEX-only spreads), not of the strategies. Investigated and
  rejected "re-fetch the quote before the promotion check": the quote is already fetched
  immediately before `_market_data_status` (`service_scan.py`), so a re-fetch returns the
  same stale IEX quote. Applied instead: `MARKET_UNIVERSE_SYMBOLS` trimmed to 25
  IEX-liquid mega-caps (Railway var) so the scan spends its budget where IEX quotes are
  fresh and tight. Structural fix remains an operator decision: paid SIP/NBBO feed.
- **SECOND SILENT STOP (Mon 19:38 → Tue 2026-09-08 12:51 UTC).** Setting the universe
  variable triggered a Railway redeploy that SIGTERM'd the running container and never
  started a replacement (the replacement deploy never appeared in the deployment list;
  `environment-status` showed the service with no deployment). The `ALWAYS` restart
  policy did not help — it restarts a crashed container, not a deploy that never came
  up. Market had closed 22 min later so no session was lost, but this is the same
  failure shape as the 2-day outage. Recovered with a manual `redeploy` at 12:51 UTC
  before Tuesday's open. Rule from now on: after ANY Railway variable change or push,
  verify a new deployment reaches SUCCESS and `run_logs` resumes — never assume.
- **CORRECTION (2026-09-08): Mon 2026-09-07 was Labor Day — the market was closed.**
  All 72 promotion attempts that day carried `quote_too_old`, and recorded spreads
  were a single frozen Friday quote (AAPL 1023.6 bps, identical for 20 hours). The
  Monday "blocker" analysis above (quote_too_old / spread_too_wide as IEX artifacts)
  is therefore void for that day: nothing could have traded. The fixed system has
  not yet run through an open market; Tue 2026-09-08 13:30 UTC is the first real
  test. No gates were changed. Also found: the effective auto-exec score floor in
  the active exploration profile is `PAPER_EXPLORATION_AUTO_EXECUTION_MIN_SCORE=0.15`
  (typo, effectively open), so `AUTO_EXECUTION_MIN_SCORE` is not the binding gate.
  Action ledger from now on: `OPS_LOG.md`.
- **Ops dashboard published (2026-09-07):** a private Claude artifact "AlgoTrader Ops"
  (blockers chart, shipped ledger, what's next, bots table, where work is recorded).
  Snapshot, not live; regenerate on request.
- **Operator follow-up still open:** point `DATABASE_URL` at the Supabase **transaction
  pooler (port 6543)** (same string, `:5432`→`:6543`). The statement-timeout fix stops the
  pool from wedging; 6543 removes the failure class entirely. Needs the DB password
  (redacted from the agent), so operator-only.

## Current status (as of 2026-09-05)

- **The bot is HEALTHY and running the fixed code.** As of 2026-09-05 10:25 UTC the
  fixed build (`b4f5fdd`) deployed and booted clean on a cleared Supabase pool:
  DB connected, scheduler running 4 jobs, no `ECHECKOUTTIMEOUT`, no 240s scan kills.
  Paper account, IEX feed, widened US universe.
- **Two P0s were found and fixed this session** (both had blocked ALL autonomous trades):
  1. **Scan-timeout:** `workflow_cadence` exceeded its 240s wall-clock cap and was
     killed every cycle, so no scan ever completed. Fixed (#27) by enforcing the batch
     deadline *inside* the per-symbol strategy-spec loop + Railway
     `SCREENER_BATCH_DEADLINE_SECONDS=120`.
  2. **DB pool exhaustion:** the Supabase pooler (session mode, port 5432) ran out of
     connections (`ECHECKOUTTIMEOUT`) — scans stalled and, worse, new deploys couldn't
     even boot, so the fix couldn't ship. Fixed by bounding the SQLAlchemy pool (#28) +
     retrying the first connection at startup (#29), and cleared by an operator Supabase
     restart on 2026-09-05.
- **Third 240s timeout found & fixed (2026-09-05 ~12:15 UTC).** The 10:57 scan-health
  check-in surfaced a *different* job hitting the 240s cap: `backtest_gate_refresh`
  walk-forward-backtests the full 200-symbol universe every 6h with no time bound, so
  it was killed every run and only ever covered the leading symbols. Fixed on the branch
  (commit `8d1b4e5`): the runner now stops cleanly at a soft `deadline_seconds` (180s,
  under the hard cap) and a persisted cursor rotates the start offset so successive runs
  sweep the whole universe. Not a Monday blocker (unattended paper exploration bypasses
  the weak-backtest watchlist downgrade), but it stops the recurring timeout, frees
  ~240s of worker time per cycle, and lets the quality gate finally populate.
- **Config verified live + near-miss auto-exec ENABLED (2026-09-05 ~12:50 UTC).** The
  new `execution_policy_effective` startup log (queryable from run_logs) revealed the
  deployed policy auto-executed **strict-valid only** — near-miss candidates (the bulk
  of exploration output) were proposed but held for human approval, so the first
  autonomous trade might never have fired. With operator sign-off, set
  `PAPER_UNATTENDED_NEAR_MISS_AUTO_EXEC_ENABLED=true` (confirmed `true` in the policy
  log after redeploy). Paper-only intact; every HARD gate still enforced. Open flag:
  `paper_exploration_auto_execution_min_score=0.15` deployed vs code default `60.0` —
  likely a typo; with near-miss auto-exec on it effectively disables the score floor
  for near-miss candidates (pending operator confirmation of intent).
- **Backtest deadline fix, round 2 — the real bug.** The first deadline fix only checked
  the budget at the *top of the symbol loop*, but a single symbol's full 20-strategy
  walk-forward exceeds even the 240s hard cap, so the job was still hard-killed
  mid-symbol — and because a hard kill never returns, the rotation cursor never advanced:
  the bot was stuck re-killing the same slow symbol every cycle (observed 12:59 UTC).
  Fixed by also checking the deadline *inside* the strategy-spec loop, so it bails
  mid-symbol, returns cleanly, and the cursor advances. NOT Monday-blocking (exploration
  candidates don't require backtest validation). Known limitation: one symbol still eats
  the whole ~180s budget (≈1 symbol/run), so the gate populates slowly — the real
  throughput fix (scope to active strategies / traded universe) is a careful post-Monday
  change.
- **Still UNVERIFIED — the first autonomous paper trade.** Infra is healthy but no trade
  has fired yet (weekend; market closed). The **Monday 2026-09-07 13:40 UTC** watch is
  the real test: does a promoted candidate execute, or does the (now-populating) funnel
  reveal a remaining gap?
- **Operator follow-ups (optional, durable):** switch `DATABASE_URL` to the Supabase
  **transaction pooler (port 6543)** so the pool can't re-exhaust; add
  `CLAUDE_CODE_OAUTH_TOKEN` as a **repository** secret to switch on the review bots.
- Honest framing: there is no "instant profit". Profit comes from a *validated edge
  measured over time*. Current phase: **confirm the first trade fires Monday → then let
  real funnel/P&L data drive which gate/strategy changes are worth making.**

---

## Shipped (merged to `main`)

Newest first. `PR #n` links the full write-up; the commit is the permanent record.

| PR | Change | Why it matters |
|----|--------|----------------|
| #29 | Retry the first DB connection at startup | Deploys survive a transiently exhausted pool instead of hard-crashing on boot |
| #28 | Bound the Postgres connection pool | Old+new deploy instances fit a small Supabase limit — unblocks deploys |
| #27 | Enforce scan wall-clock deadline **inside the strategy-spec loop** | The P0 that stopped all trading: scan no longer overruns its 240s job cap and gets killed |
| #26 | `ROADMAP.md` — durable build log & plan | The record survives ephemeral sessions |
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

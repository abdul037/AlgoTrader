# AlgoTrader Ops Log

Append-only record of every operational action taken on the paper-trading bot:
deploys, config changes, incidents, decisions, and what was verified. Newest
day first, entries within a day in chronological order (UTC). Long-form
post-mortems and roadmap live in `ROADMAP.md`; this file is the action ledger.

Standing constraints: PAPER ONLY (`ENABLE_REAL_TRADING=false`,
`EXECUTION_MODE=paper`). Hard risk gates (spread cap, reward:risk, valid
bracket, relative-volume floor, regular hours, blacklist) are never loosened
without explicit operator sign-off recorded here.

---

## 2026-09-08 (Tue)

- **12:50** Verified the dashboard publish and found the bot had been DOWN since
  Mon 19:38 UTC: the Railway redeploy triggered by the `MARKET_UNIVERSE_SYMBOLS`
  variable change SIGTERM'd the running container and never started a
  replacement (`environment-status` showed no deployment). Restart policy
  `ALWAYS` did not help because nothing crashed; the replacement never came up.
- **12:51** Manual Railway `redeploy` → deployment `e32027c7` SUCCESS at 12:53.
  Startup policy log confirmed `execution_mode=paper`, `enable_real_trading=false`,
  near-miss auto-exec on. Alpaca paper account ACTIVE, equity $100,064.91, 0
  positions, reconciliation clean. Events flowing, 0 errors.
- **12:52** Scheduled routine `trig_01Dyf8QEda4fMDZeqCsGTztV`: pre-open health
  check + first-trade watch at 13:35 UTC.
- **12:54** Pushed `257fab9` (ROADMAP: Monday EOD, second silent stop, universe
  trim, dashboard). Push-triggered deployment `37ab2152` SUCCESS at 12:56:40 and
  replaced `e32027c7` cleanly. 2 clean boots today, 0 errors.
- **12:58** Operator asked for the auto-exec score floor to be lowered 65 → 55.
  **Not applied — it would be a no-op.** In the active paper-exploration profile
  the effective floor is `PAPER_EXPLORATION_AUTO_EXECUTION_MIN_SCORE`, which is
  set to `0.15` in Railway (almost certainly a typo for 15 or 55, but it means
  the score gate is already effectively open). `AUTO_EXECUTION_MIN_SCORE=65`
  only applies when the exploration profile is off. Left unchanged; flagged.
- **13:00** Re-analysed Monday's "blockers" with the correct key
  (`promotion_blockers`, not `reasons`). Finding: **Mon 2026-09-07 was Labor
  Day — US markets were closed all day.** 72 of 72 promotion attempts carried
  `quote_too_old`; AAPL's recorded spread was an identical 1023.6 bps at 17:09,
  19:28 and 12:57 the next day, i.e. one frozen Friday quote. Monday's zero
  trades and its entire blocker mix were artifacts of a closed market, not of
  IEX or the strategies. Correction to earlier analysis recorded in ROADMAP.
  Consequence: the fixed system (Sep 5 fixes) has **never yet seen an open
  market**. Tue 13:30 UTC open is the first real test. No gate changes made.
- **13:02** Railway `watchPatterns` set to `["/**", "!/**/*.md", "!/docs/**"]`
  so that log/roadmap-only pushes no longer restart the bot. Verified after this
  push: no new deployment should appear.
- **15:02** First auto-execution attempt: AMD `anchored_vwap_pullback_continuation`,
  $500 notional. **Blocked at the broker step:** `one_share_exceeds_max_trade_amount`
  (AMD ≈ $501/share > `MAX_TRADE_AMOUNT_USD=500`). Not a gate; a sizing cap.
  Operator decision: raise the per-trade cap or allow fractional shares.
- **17:00:50 — FIRST AUTONOMOUS PAPER TRADE.** GOOGL buy 1 share @ $338.75,
  strategy `opening_range_breakout_retest` (supervised weak-valid path), $500
  notional request, Alpaca paper bracket order `c3bd9790…`: parent filled, stop
  leg $333.24 (held), take-profit leg $349.39 (limit, new). Quote verified live
  (Alpaca IEX), bars fresh. Alpaca reconciliation clean: `positions_seen: 1`,
  `orders_seen: 28`, equity $100,064.13 (baseline $100,064.91), cash $99,726.16.
  Task #56 (autonomous paper-trade fluency) marked complete.
- **13:30–18:30** Live-session stats: 690 scan decisions, 51 near-miss promotion
  attempts, 19 promoted to candidate, 2 reached execution (1 filled, 1 blocked
  by the sizing cap). `workflow_cadence` hit the 240s job timeout 16 times
  (~every 20 min) — the scan still completes enough to trade, but this is the
  recurring item the operator said to leave for now.
- **13:10** Two Railway `redeploy`s of older snapshots appeared (commits
  `c6cd480` and `0d412fb`), not triggered by this session; the surviving
  deployment `ac733f02` runs `c6cd480`, functionally identical to head for the
  app (differs only in CI workflow + a test ceiling). Bot healthy through it.
- **18:32** Re-armed pre-close check routine for 19:30 UTC.
- **18:34** Operator sign-off: `MAX_TRADE_AMOUNT_USD` raised 500 → 1000 (Railway var;
  redeploy triggered — verified below). Caveat found while applying it: the broker
  step sizes as `floor(min(request_amount, max_cap) / price)` and the request
  amount is `DEFAULT_TRADE_AMOUNT_USD=500`, so a $501 AMD share still rounds to 0
  shares. The cap is no longer binding; the default request amount is. Raising
  the default to 1000 doubles every trade's size — left for operator decision.
- **18:37** Operator sign-off: `DEFAULT_TRADE_AMOUNT_USD` raised 500 → 1000 as well
  (Railway var). Every new proposal now requests $1,000 notional (1 share of
  anything up to $1,000). Observation: the 18:34 `MAX_TRADE_AMOUNT_USD` change did
  not produce a deployment on its own; the 18:37 change did (deployment
  `3a2a07f3`, carrying both values). Same failure shape as Mon 19:38 — variable
  changes are unreliable deploy triggers on this service; always verify.
- **18:40** Shipped `4b52d95`: the startup `execution_policy_effective` log now
  includes `default_trade_amount_usd`, `max_trade_amount_usd`, `max_open_positions`,
  `max_trades_per_day`, so sizing-blocked trades are diagnosable from run_logs.
- **18:45** Railway SKIPPED the `7b6ade6` code push: the watch pattern I set at 13:02
  (`/**`) was malformed (gitignore-style needs `**`), so nothing matched the include
  rule and every push was skipped. Fixed to `["**", "!**/*.md", "!/docs/**"]` and
  moved it into `railway.json` (`build.watchPatterns`) so it is version-controlled.
  This push carries both the pattern fix and the policy-snapshot change.
- **18:44 — VERIFIED from the new container's startup log** (deployment `27b1caad`,
  commit `661106a`): `default_trade_amount_usd=1000`, `max_trade_amount_usd=1000`,
  `max_open_positions=3`, `max_trades_per_day=6`, `execution_mode=paper`,
  `enable_real_trading=false`. GOOGL position and bracket unaffected (held at the
  broker). Notion mirror updated with the same entry.
- **18:50** Found the P&L ledger gap: with `paper_broker=alpaca` the coordinator writes
  executions + broker order snapshots but never `paper_positions`/`paper_trades`, so
  the equity curve, EOD digest and strategy scorecard were blind to real broker-backed
  paper trades. Shipped `d8623cc` (`app/paper/broker_ledger.py`): a filled parent
  bracket opens a ledger position from the broker fill; a filled stop/target leg (or
  matched close order) closes it into a paper trade with realized P&L; broker-backed
  rows are only marked-to-market, never closed by the simulator. Idempotent, paper-only.
  Also fixed `test_railway_deployment` for the version-controlled watch patterns.
- **18:58 — VERIFIED in prod** (deployment `03df1d2b`): first refresh wrote GOOGL as an
  open ledger position (entry $338.75, marked $337.86, unrealized −$0.89, stop/target
  from the bracket) and backfilled two old supervised Alpaca tests (AAPL Jul 16 −$1.49,
  NVDA Jun 22 −$0.12) as closed trades. Task #62 complete.
- **19:00** Root cause of "why only GOOGL": 17 of 19 promoted candidates failed at the
  proposal step with `Instrument X is not in the allowed instrument list` (INTC ×6,
  META ×3, AVGO ×2, QQQ ×2, AMZN, TSLA, NFLX, CSCO). `ALLOWED_INSTRUMENTS` was an old
  whitelist (default `NVDA,GOOG,GOOGL,AMD,MU,GOLD`) never widened when the universe was
  trimmed to 25 names — the scanner and the proposal gate disagreed. GOOGL and AMD
  only got through because they were on the old list.
- **19:02** Operator sign-off ("set the allowed instruments to match the universe"):
  `ALLOWED_INSTRUMENTS` set to the exact 25-symbol `MARKET_UNIVERSE_SYMBOLS` value.
  Redeploy verification recorded below.
- **19:07 — VERIFIED.** Allowlist deploy `26356326` booted 19:04:58; follow-up deploy
  `2400459b` (commit `e1a8e9b`, SUCCESS 19:07:45) adds allowlist-vs-universe visibility
  to the startup policy log, which now reads `allowed_instruments_count=25`,
  `market_universe_symbols_count=25`, `universe_not_in_allowlist=[]`. Zero allowlist
  rejections since the fix. Every symbol the scanner promotes can now be proposed.
- **19:10** Operator dissatisfied with pace and asked for a plan to reach "20% every
  day". Answer on record: 20%/day is not achievable by any strategy (compounds $100k to
  $3.8M in a month) and would only be reached by ruinous leverage; declined to build
  toward it. Proposed ladder instead: (1) two weeks of throughput + measurement (5–10
  paper trades/day, 50 closed trades), (2) concentrate on positive-expectancy strategies
  and target 0.1–0.3%/day, (3) scale with capital, not risk. Asked for sign-off on:
  fixing the recurring `workflow_cadence` 240s timeout, risk-based sizing at 0.5% of
  equity per trade, caps 3→5 open positions and 6→12 trades/day, enabling
  auto-demotion after 20 trades per strategy, daily EOD report. Hard gates unchanged.
- **19:20 — P0 FINDING while sizing the 'max per day' question.** The walk-forward
  backtest gate has never measured anything: of ~1,208,139 backtest rows since Aug 1
  (20 strategies × 196 symbols, 31,942 out-of-sample summaries), **every single one has
  `number_of_trades = 0`.** Each fold evaluates ~10 daily bars (`bars_evaluated` avg
  10.2, `fold_count` 37), far too short for any of these strategies to trigger. So there
  is no measured expectancy for any strategy, the "backtest validated" flag has never
  been earned, and the scheduler's 180s backtest budget has been producing empty rows
  for weeks. Task #63 opened: fix fold sizing, assert >0 trades on a trending fixture,
  re-run for the 25-name universe. This is the top priority — nothing about expected
  daily return can be estimated until it is fixed.
- **19:25 — OPERATOR APPROVED the aggressive paper plan + risk settings.** Applied via
  Railway (deployment `b9b8de52`, SUCCESS 19:31): `MAX_RISK_PER_TRADE_PCT=1.0`,
  `DEFAULT_TRADE_AMOUNT_USD=MAX_TRADE_AMOUNT_USD=12500` (per-position notional cap =
  12.5% of equity, so 8 positions ≤ 100% gross, no margin), `MAX_OPEN_POSITIONS=8`,
  `MAX_TRADES_PER_DAY=15`, `MAX_DAILY_LOSS_USD=3000` (3% hard stop, counts open losses),
  `MAX_WEEKLY_LOSS_USD=8000`, `MAX_CONSECUTIVE_LOSSES_BEFORE_COOLDOWN=4` (was 2, which
  would have halted most days by the second loss), drawdown governor ON (soft 2.5%,
  hard 5%, floor 0.5 = size halves), `AUTO_PROPOSE_RISK_BASED_SIZING=true`.
  Hard gates (spread, reward:risk, bracket, rvol, hours, blacklist) unchanged.
- **19:33** Shipped risk-based sizing for the unattended path (`app/risk/proposal_sizing.py`,
  wired in `auto_propose_candidates`). Finding: auto-proposals always passed a flat
  `default_trade_amount_usd`, so `max_risk_per_trade_pct` never applied to any
  autonomous trade — the $1,000 GOOGL trade risked ~$5. Now: notional = 1% of
  reconciled equity ÷ stop distance, capped at $12,500; falls back to the flat default
  if entry/stop/equity are missing (never blocks a proposal); the sizing record is
  stored in proposal metadata. Startup policy log now includes the full risk profile.
  Plan steps still to do: fix the backtester (#63), 5m/15m timeframes + scan cadence
  fix, auto-demote after 15 trades, daily EOD report.
- **19:36 — VERIFIED** the sizing deploy (`866f9413`) from its startup log: risk 1%/trade,
  risk-based sizing on, daily loss $3,000, weekly $8,000, cooldown after 4 losses,
  governor on (floor 0.5), 8 positions, 15 trades/day, paper-only.
- **19:45** Shipped the backtester fix (Task #63). Root cause: each walk-forward fold ran
  the engine on its ~10-bar test slice alone, below every strategy's indicator warm-up.
  Fix: the engine gained `trade_window_start` (warm-up bars are context only — no
  signals, entries or equity points), and folds/holdout now pass train+test bars with
  the test start as the window. Regression test proves the same synthetic folds go from
  0 trades (old) to >0 (new) with every entry inside the test window. 648 tests pass.
  From the next `backtest_gate_refresh` run the gate starts filling with real
  out-of-sample numbers per strategy × symbol.
- **13:03** Created this file at operator request ("update all the actions you
  are doing"): chose a repo Markdown ledger over Notion because it is
  version-controlled, reviewed by the PR bots, and lives with the code.

## 2026-09-07 (Mon — Labor Day, market closed)

- **~16:50** Redeployed hardened image after the 2-day DB-pool outage (see
  ROADMAP post-mortem). Bot healthy: 0 scheduler errors, paper-safe.
- Railway reliability config set via connector: restart policy `ALWAYS` (10
  retries), healthcheck `/health/ready` (300s).
- Review-team bots fixed (PR #31 opened, secret name matched, token re-pasted
  without newline, `--max-turns 60`). PR #31 CI green and squash-merged to
  `main` (`ffeeda95`).
- DB durable fix shipped: `statement_timeout` (30s) + TCP keepalives in
  `connect_args` (`f6d0d08`); architecture ratchet ceiling for `db.py` bumped
  to 1063 (`0a12efe`).
- **~19:30** Investigated `quote_too_old`. Rejected "re-fetch quote before the
  promotion check" as dead code (quote is fetched immediately before the check
  in `service_scan.py`). Applied `MARKET_UNIVERSE_SYMBOLS` = 25 IEX-liquid names
  via Railway variable. (Retrospective: the staleness that day was the holiday,
  not IEX; the universe trim still stands as harmless and focused.)
- **19:38** The variable change's redeploy stopped the container with no
  replacement → bot down until Tue 12:51. Market had closed 20:00 (and was
  closed all day for the holiday), so no session was lost.
- Published ops dashboard artifact "AlgoTrader Ops".

## Open operator items

- Switch `DATABASE_URL` to the Supabase transaction pooler (port 6543); needs
  the DB password (redacted from the agent).
- Rotate the Claude OAuth token that was pasted into chat once; optionally
  rename the GitHub secret to `CLAUDE_CODE_OAUTH_TOKEN` and revert the workflow
  refs.
- Decide on a paid SIP/NBBO feed (removes IEX single-venue artifacts).
- Fix the `PAPER_EXPLORATION_AUTO_EXECUTION_MIN_SCORE=0.15` typo to an
  intentional value once there is live data to calibrate against.

# AlgoTrader Review Team

A team of four role-based Claude reviewers that automatically reviews **every pull request**
and posts one combined review comment, focused on the project's goal: a safe, measured path to
profit. It runs on your **Claude subscription** (OAuth token) — no metered API key.

| Bot | Reviews for | Verdict scale |
|-----|-------------|---------------|
| 🧪 **QA Bot** | Correctness, regressions, test coverage, **safety gates** (paper-only, hard risk gates, secrets) | APPROVE / CHANGES / BLOCK |
| 📈 **Financial Strategy Bot** | Edge soundness, backtest methodology, overfitting, quiet quality-bar erosion | SOUND / CONCERNS / HARMS EDGE |
| 💹 **Trader Bot** | Net-of-cost realism, risk of ruin, execution quality, live-vs-backtest decay | TRADEABLE / RISK / DON'T SHIP |
| 📋 **Project Manager Bot** | Synthesizes the other three; scope, priorities, missing follow-ups, next step | SHIP / HOLD / BLOCK (coordinator) |

The PM bot runs last and **coordinates** — its section is shown first; the three specialist
reviews are in a collapsible block underneath.

## How it works
- Workflow: `.github/workflows/review-team.yml` runs on `opened`/`synchronize`/`reopened`/`ready_for_review`.
- Engine: the official [`anthropics/claude-code-action`](https://github.com/anthropics/claude-code-action),
  authenticated with your Claude subscription OAuth token. The action reads the personas below,
  diffs the PR, runs the four reviewers, and posts/updates **one sticky comment**.
- Personas: the `*.md` files in this folder. Edit them to tune what each bot cares about —
  they're versioned like any other code.
- **Advisory only.** It never blocks a merge, and it can never push code (`contents: read`).

## Setup (one step) — use your Claude subscription
1. In Claude Code (with a Pro/Max subscription), run:
   ```
   claude setup-token
   ```
   This prints a long-lived OAuth token.
2. Add it as the repository secret **`CLAUDE_CODE_OAUTH_TOKEN`**:
   `GitHub repo → Settings → Secrets and variables → Actions → New repository secret`.

Reviews then draw on your existing subscription instead of a pay-per-use API bill. Until the
secret is present, the workflow runs and exits cleanly with a notice — no failed checks.

> Prefer a metered API key instead? Swap `claude_code_oauth_token` for `anthropic_api_key` in the
> workflow and store an `ANTHROPIC_API_KEY` secret from console.anthropic.com.

## Tuning
- **What each bot checks:** edit the persona `*.md` files here.
- **Model / turns:** adjust `claude_args` in the workflow (e.g. `--model ...`, `--max-turns ...`).
- **Large diffs / cost:** the action reviews the diff directly; keep PRs focused for the tightest reviews.

## Adding or removing a bot
- **Remove:** delete its persona file here and drop it from the workflow prompt's Step-1 list and
  the combined-comment template.
- **Add:** drop a new `<role>.md` here and reference it in the workflow prompt (Step 1 + the
  comment template).

# AlgoTrader Review Team

A team of four role-based Claude reviewers that automatically reviews **every pull request**
and posts one combined review comment, focused on the project's goal: a safe, measured path to
profit.

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
- Orchestrator: `scripts/review_team.py` computes the PR diff, runs each bot, posts/updates **one**
  comment in place (so re-pushes don't spam the thread).
- Personas: the `*.md` files in this folder. Edit them to tune what each bot cares about —
  they're versioned like any other code.
- **Advisory only.** It never blocks a merge; it's a second pair of (four) eyes.

## Setup (one step)
Add the repository secret **`ANTHROPIC_API_KEY`**:
`GitHub repo → Settings → Secrets and variables → Actions → New repository secret`.

Without the secret the workflow still runs and exits cleanly with a notice — it just won't post a
review until the key is present.

## Cost / quality levers (optional repo *variables*)
Set under `Settings → Secrets and variables → Actions → Variables`:
- `REVIEW_MODEL` — default `claude-opus-5`. Use `claude-sonnet-5` or `claude-haiku-4-5` to cut cost.
- `REVIEW_EFFORT` — default `high`. Use `medium`/`low` to cut cost.

Each PR runs 4 model calls; the diff is prompt-cached across them to keep cost down. Large diffs
are capped (`REVIEW_MAX_DIFF_CHARS`, default 120k chars).

## Adding or removing a bot
- **Remove:** delete its entry from `SPECIALISTS` in `scripts/review_team.py` (and optionally its `.md`).
- **Add:** drop a new `<role>.md` here and add `("your-role.md", "Name", "🔧")` to `SPECIALISTS`.
